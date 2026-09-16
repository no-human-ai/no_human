"""GitHub Enterprise check-runs reader (IMPROVEMENT_PLAN D).

Reads check-run status from the GitHub / GHE Checks API for a given commit/ref.
This is NOT GitHub Actions (which *triggers* workflows) — it reads status checks
that external systems (Jenkins, GitLab, etc.) report to GHE.

Uses ``gh api`` for auth + GHE host routing (the CLI handles ``GH_ENTERPRISE_TOKEN``
and host selection transparently). Falls back cleanly when ``gh`` is missing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from typing import Any

from .base import CIBackend, CIResult, JobResult, PipelineStatus

log = logging.getLogger(__name__)

# Map GHE check-run conclusions to our PipelineStatus.
_CONCLUSION_MAP: dict[str, PipelineStatus] = {
    "success": PipelineStatus.SUCCESS,
    "failure": PipelineStatus.FAILED,
    "cancelled": PipelineStatus.CANCELED,
    "skipped": PipelineStatus.SKIPPED,
    "timed_out": PipelineStatus.FAILED,
    "action_required": PipelineStatus.MANUAL,
    "neutral": PipelineStatus.SUCCESS,  # neutral = advisory, not failure
    "stale": PipelineStatus.UNKNOWN,
}


# The env key a human must set to clear an access wall. GitHub's CLI reads this.
_ACCESS_ENV_KEY = "GH_TOKEN"

# Substrings in a `gh api` error that mean "auth wall" (not a transient blip):
# retrying will never help — only a human granting a valid token will. Matched
# as "http 401"/"http 403" (NOT bare "401"/"403", which appear inside byte
# counts / millisecond timers on unrelated transient errors).
_AUTH_SIGNALS: tuple[str, ...] = (
    "http 401", "http 403", "bad credentials", "requires authentication",
    "must authenticate", "gh auth login", "resource not accessible",
    "not accessible by",
)

# Transient conditions that HAPPEN to carry a 403/404 but are NOT auth walls —
# retrying (with backoff) is correct, so these must fall through to infra_failure.
_TRANSIENT_SIGNALS: tuple[str, ...] = (
    "rate limit",        # GitHub emits `HTTP 403: API rate limit exceeded` — clears itself
    "no commit found",   # a 404 for a not-yet-pushed SHA — a token won't fix it; re-check
    "no ref found",
)


def _is_auth_error(message: str) -> bool:
    m = message.lower()
    if any(sig in m for sig in _TRANSIENT_SIGNALS):
        return False
    return any(sig in m for sig in _AUTH_SIGNALS)


def _run_gh(args: list[str], *, hostname: str = "") -> dict[str, Any]:
    """Run ``gh api`` and parse the JSON response.

    ``hostname`` targets a GHE instance (e.g. ``code.example.com``); when empty,
    ``gh`` uses its default host.
    """
    cmd = ["gh", "api"]
    if hostname:
        cmd += ["--hostname", hostname]
    cmd += args
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api failed ({result.returncode}): {result.stderr.strip()}")
    return json.loads(result.stdout)


async def fetch_check_runs(
    repo: str,
    ref: str,
    *,
    hostname: str = "",
) -> list[dict[str, Any]]:
    """Fetch check runs for ``ref`` (branch or SHA) from ``repo``.

    Returns the raw check_runs list from the API. Runs in a thread to keep the
    event loop unblocked.
    """
    endpoint = f"/repos/{repo}/commits/{ref}/check-runs"
    data = await asyncio.to_thread(_run_gh, [endpoint], hostname=hostname)
    return data.get("check_runs", [])


# The Commit Status API (`/commits/{ref}/status`) is a DIFFERENT surface from
# check-runs: it's what external reporters (Jenkins, GitLab, a bespoke bot) POST
# via `POST /statuses/{sha}`. A repo can have check-runs, commit-statuses, both,
# or neither — so we read both and merge, never letting one green source hide
# the other's failure (`merge_ci_results`).
async def fetch_commit_statuses(
    repo: str,
    ref: str,
    *,
    hostname: str = "",
) -> dict[str, Any]:
    """Fetch the combined commit status for ``ref`` from ``repo``.

    Returns the raw ``/commits/{ref}/status`` payload
    (``{state, statuses: [...], total_count}``). Runs in a thread to keep the
    event loop unblocked.
    """
    endpoint = f"/repos/{repo}/commits/{ref}/status"
    return await asyncio.to_thread(_run_gh, [endpoint], hostname=hostname)


# Map a Commit-Status state to our PipelineStatus. The API's per-status and
# combined `state` field is one of success / pending / failure / error. Anything
# unexpected falls through to UNKNOWN (never green).
_STATUS_STATE_MAP: dict[str, PipelineStatus] = {
    "success": PipelineStatus.SUCCESS,
    "pending": PipelineStatus.RUNNING,
    "failure": PipelineStatus.FAILED,
    "error": PipelineStatus.FAILED,
}


def statuses_to_result(
    data: dict[str, Any],
    *,
    ref: str = "",
    url_hint: str = "",
) -> CIResult:
    """Convert a ``/commits/{ref}/status`` payload into a ``CIResult``.

    CRITICAL empty-vs-pending distinction: GitHub reports ``state == "pending"``
    for a commit that has ZERO statuses (no CI reported anything). Treating that
    as RUNNING would make the caller poll forever waiting on CI that will never
    report. So an empty status list -> UNKNOWN with NO jobs — the "no opinion"
    signal ``merge_ci_results`` uses to drop this source. A non-empty list whose
    combined state we can't map stays UNKNOWN but WITH jobs (indeterminate, not
    empty), so it is never silently dropped.
    """
    statuses = data.get("statuses") or []
    if not statuses:
        return CIResult(
            pipeline_id=ref,
            pipeline_url=url_hint,
            status=PipelineStatus.UNKNOWN,
        )

    combined = (data.get("state") or "").lower()
    overall = _STATUS_STATE_MAP.get(combined, PipelineStatus.UNKNOWN)
    jobs: list[JobResult] = []
    for s in statuses:
        st = _STATUS_STATE_MAP.get((s.get("state") or "").lower(),
                                   PipelineStatus.UNKNOWN)
        jobs.append(JobResult(
            name=s.get("context", "?"),
            status=st.value,
            failure_reason=(s.get("state") if st == PipelineStatus.FAILED else None),
            web_url=s.get("target_url", "") or "",
        ))
    return CIResult(
        pipeline_id=ref,
        pipeline_url=url_hint,
        status=overall,
        jobs=jobs,
    )


def merge_ci_results(
    a: CIResult,
    b: CIResult,
    *,
    ref: str = "",
    url_hint: str = "",
) -> CIResult:
    """Merge two CI reads (e.g. check-runs + commit-statuses) — NEVER false-green.

    A source with "no opinion" (UNKNOWN, no jobs, no error flag — the empty-CI
    signal) is dropped: green check-runs plus zero commit-statuses is green. But
    an UNKNOWN carrying jobs (indeterminate) or an error flag (access/infra) is
    NOT empty — it keeps the merged verdict at UNKNOWN so we never vouch for a
    result we couldn't read. Across the surviving sources: any failure wins, then
    any running, then any indeterminate/unreadable, else success. Access/infra
    flags propagate so the orchestrator still parks/retries correctly.
    """
    def is_empty(r: CIResult) -> bool:
        return (r.status == PipelineStatus.UNKNOWN and not r.jobs
                and not r.access_failure and not r.infra_failure)

    pid = ref or a.pipeline_id or b.pipeline_id
    purl = url_hint or a.pipeline_url or b.pipeline_url
    access = a.access_failure or b.access_failure
    infra = a.infra_failure or b.infra_failure
    env_key = a.access_env_key or b.access_env_key

    sources = [r for r in (a, b) if not is_empty(r)]
    if not sources:
        # Neither source reported anything — no CI at all. UNKNOWN, never green.
        return CIResult(
            pipeline_id=pid, pipeline_url=purl, status=PipelineStatus.UNKNOWN,
            access_failure=access, infra_failure=infra, access_env_key=env_key,
        )

    jobs = [j for r in sources for j in r.jobs]
    states = [r.status for r in sources]
    if any(s in (PipelineStatus.FAILED, PipelineStatus.CANCELED) for s in states):
        overall = PipelineStatus.FAILED
    elif any(s == PipelineStatus.RUNNING for s in states):
        overall = PipelineStatus.RUNNING
    elif all(s == PipelineStatus.SUCCESS for s in states) and not access and not infra:
        # SUCCESS only when EVERY surviving source is affirmatively SUCCESS and
        # nothing was unreadable. Requiring all-success (rather than falling
        # through from "not failed/running") means any other surviving state — an
        # UNKNOWN indeterminate, or a future reader emitting MANUAL/PENDING —
        # yields UNKNOWN, never a green we can't stand behind.
        overall = PipelineStatus.SUCCESS
    else:
        overall = PipelineStatus.UNKNOWN

    return CIResult(
        pipeline_id=pid, pipeline_url=purl, status=overall, jobs=jobs,
        access_failure=access, infra_failure=infra, access_env_key=env_key,
    )


def check_runs_to_result(
    check_runs: list[dict[str, Any]],
    *,
    ref: str = "",
    url_hint: str = "",
) -> CIResult:
    """Convert raw check_runs into a ``CIResult``.

    The overall status is SUCCESS only when ALL check runs have concluded
    successfully. If any are still in progress, status is RUNNING.
    """
    if not check_runs:
        return CIResult(
            pipeline_id=ref,
            pipeline_url=url_hint,
            status=PipelineStatus.UNKNOWN,
        )

    jobs: list[JobResult] = []
    any_in_progress = False
    any_failed = False
    any_indeterminate = False

    # A completed check counts as "clean" ONLY with one of these conclusions.
    # Anything else that has completed (action_required, stale, startup_failure,
    # a null/unknown conclusion, ...) is INDETERMINATE — we must not call it
    # green (a workflow that failed to start or is awaiting manual approval has
    # NOT run the tests). SKIPPED = didn't need to run; NEUTRAL maps to SUCCESS.
    _CLEAN = (PipelineStatus.SUCCESS, PipelineStatus.SKIPPED)

    for cr in check_runs:
        name = cr.get("name", "?")
        conclusion = (cr.get("conclusion") or "").lower()
        status = (cr.get("status") or "").lower()
        web_url = cr.get("html_url", "")

        if status in ("queued", "in_progress"):
            any_in_progress = True
            jobs.append(JobResult(name=name, status="running", web_url=web_url))
            continue

        mapped = _CONCLUSION_MAP.get(conclusion, PipelineStatus.UNKNOWN)
        if mapped in (PipelineStatus.FAILED, PipelineStatus.CANCELED):
            any_failed = True
        elif mapped not in _CLEAN:
            any_indeterminate = True
        jobs.append(JobResult(
            name=name,
            status=mapped.value,
            failure_reason=conclusion if mapped == PipelineStatus.FAILED else None,
            web_url=web_url,
        ))

    if any_in_progress:
        overall = PipelineStatus.RUNNING
    elif any_failed:
        overall = PipelineStatus.FAILED
    elif any_indeterminate:
        # A completed-but-not-clean check we cannot vouch for -> UNKNOWN, never
        # green. The caller treats UNKNOWN as "no verdict", not a pass.
        overall = PipelineStatus.UNKNOWN
    else:
        overall = PipelineStatus.SUCCESS

    return CIResult(
        pipeline_id=ref,
        pipeline_url=url_hint,
        status=overall,
        jobs=jobs,
    )


class GHECheckRunsCI(CIBackend):
    """Read-only CI backend that reads check-run results from GitHub/GHE.

    Unlike GitHubActionsCI, this does NOT trigger CI — it only reads the
    status checks that external systems post to the GHE Checks API.
    """

    name = "ghe_checkruns"
    max_infra_retries = 0  # read-only, nothing to retry

    def __init__(
        self,
        repo: str,
        *,
        hostname: str = "",
        poll_interval: int = 30,
        timeout_minutes: int = 60,
        fetch_runs: Any | None = None,
        fetch_statuses: Any | None = None,
    ):
        self.repo = repo
        self.hostname = hostname
        self.poll_interval = poll_interval
        self.timeout_minutes = timeout_minutes
        # Injectable so tests never touch the network / the `gh` CLI.
        self._fetch_runs = fetch_runs or fetch_check_runs
        self._fetch_statuses = fetch_statuses or fetch_commit_statuses

    async def _read_source(
        self, fetch: Any, to_result: Any, ref: str,
    ) -> CIResult:
        """Fetch ONE surface (check-runs or commit-statuses) -> CIResult,
        classifying read failures.

        An auth wall -> ``access_failure``; any other read error -> a transient
        ``infra_failure``. Both map to ``UNKNOWN`` (never green) and carry their
        flag, which ``merge_ci_results`` propagates and never drops.
        """
        try:
            data = await fetch(self.repo, ref, hostname=self.hostname)
        except Exception as exc:  # noqa: BLE001 — normalize gh/RuntimeError to a CIResult
            msg = str(exc)
            if _is_auth_error(msg):
                log.warning("GHE check-runs read blocked (access) for %s@%s: %s",
                            self.repo, ref, msg)
                return CIResult(
                    pipeline_id=ref, pipeline_url="",
                    status=PipelineStatus.UNKNOWN,
                    access_failure=True, access_env_key=_ACCESS_ENV_KEY,
                    parsed_output=f"cannot read GHE checks: {msg}",
                )
            log.warning("GHE check-runs read failed (infra) for %s@%s: %s",
                        self.repo, ref, msg)
            return CIResult(
                pipeline_id=ref, pipeline_url="",
                status=PipelineStatus.UNKNOWN,
                infra_failure=True,
                parsed_output=f"transient error reading GHE checks: {msg}",
            )
        return to_result(data, ref=ref)

    async def _read(self, ref: str) -> CIResult:
        """Read check-runs AND commit-statuses for ``ref`` and merge them.

        External reporters (Jenkins, GitLab, bespoke bots) post to whichever
        surface they choose, so both are read and merged; a green on one surface
        never hides a failure on the other (``merge_ci_results``). Neither read
        is allowed to raise — a failure on either surface is classified into
        ``access_failure``/``infra_failure`` by ``_read_source``.
        """
        checks = await self._read_source(self._fetch_runs, check_runs_to_result, ref)
        statuses = await self._read_source(self._fetch_statuses, statuses_to_result, ref)
        return merge_ci_results(checks, statuses, ref=ref)

    async def trigger(
        self, branch: str, extra_variables: dict[str, str] | None = None
    ) -> CIResult:
        """Poll (check-runs + commit-statuses) until complete or timeout."""
        deadline = asyncio.get_event_loop().time() + self.timeout_minutes * 60
        while True:
            result = await self._read(branch)
            if result.status.is_terminal or result.status == PipelineStatus.UNKNOWN:
                return result
            if asyncio.get_event_loop().time() > deadline:
                log.warning("GHE check-runs timeout for %s/%s", self.repo, branch)
                return result
            await asyncio.sleep(self.poll_interval)

    async def check_status(self, pipeline_id: str) -> CIResult:
        """Non-blocking check of current status for a ref."""
        return await self._read(pipeline_id)
