"""GitLab CI backend: trigger + poll via `glab api`.

Facts encoded here:
  - Trigger uses `glab api --hostname <h> --method POST projects/<p>/pipeline
    --input <body.json>`. The `glab ci run` form is BROKEN against a
    self-hosted GitLab (defaults to gitlab.com → 401, drops variables) —
    proven live against a real instance via the POST form.
  - Always check job statuses via `glab api`, not just pipeline status.
  - On infra failures: auto-retry after 2 minutes, max_infra_retries times.
  - A permissions wall is NOT an infra failure. `_subprocess_run` used to
    return "" for every nonzero exit, discarding stderr, so a 401 was
    indistinguishable from a dropped packet: it was retried twice at 120 s and
    then escalated as TRANSIENT_INFRA — the one wording that does not tell the
    operator which key to set. Every sibling backend already made this
    distinction; this one had zero `access_failure` paths.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
import urllib.parse
from collections.abc import Sequence
from typing import Any

from .base import CIBackend, CIResult, JobResult, PipelineStatus

# Job failure_reason values that indicate infra problems, not real test failures.
_INFRA_REASONS = frozenset({
    "runner_system_failure",
    "stuck_or_timeout_failure",
    "scheduler_failure",
    "unmet_prerequisites",
    "data_integrity_failure",
    "runner_unsupported",
})

_PIPELINE_URL_RE = re.compile(r"https://[^\s]*/pipelines?/(\d+)")
_PIPELINE_ID_RE = re.compile(r"[Pp]ipeline[:\s#]+(\d+)")
_INFRA_BACKOFF_SECONDS = 120  # 2 minutes between infra retries

#: The key the escalation names. `glab` reads GITLAB_TOKEN from the PROCESS
#: environment (`glab auth login --help`: "If GITLAB_TOKEN, GITLAB_ACCESS_TOKEN,
#: or OAUTH_TOKEN are set..."), and `onboard.py` already tells an operator on a
#: GitLab backend to set exactly this. Nothing in the package loaded it from
#: ~/.no_human/.env, though, so the ask would have named a key that setting
#: changed nothing — `_load_gitlab_token` below is what makes it actionable.
_ACCESS_ENV_KEY = "GITLAB_TOKEN"

#: Substrings of a failed `glab` invocation that mean "auth wall", not "blip".
#: The first three are VERBATIM from glab 1.92.1 against gitlab.com: a bad
#: token prints `glab: 401 Unauthorized (HTTP 401)`, and no credential at all
#: prints `Unauthenticated.` The 403 forms follow the same shape and the
#: precedent `ghe_checkruns._AUTH_SIGNALS` already sets for the same HTTP codes.
#: Matched as "(http 401)"/"401 unauthorized" rather than a bare "401" so a
#: byte count or a millisecond timer in an unrelated error cannot trip it —
#: a real DNS failure reads `dial tcp: lookup h: no such host` and matches none
#: of these, which is the case `test_glab_nonzero_exit_separates_...` pins.
_AUTH_SIGNALS: tuple[str, ...] = (
    "(http 401)", "(http 403)",
    "401 unauthorized", "403 forbidden",
    "unauthenticated", "insufficient_scope",
    "glab auth login",
)


class GitLabAccessError(RuntimeError):
    """`glab` hit an auth/permission wall. Retrying cannot help — only a human
    granting access can, so this becomes ``access_failure`` and the orchestrator
    parks a MISSING_ACCESS blocker naming ``GITLAB_TOKEN``."""


#: Substrings of a failed `glab` invocation that mean "transient infra blip",
#: not "the coder's code is broken". Checked AFTER `_AUTH_SIGNALS` so an auth
#: wall always stays non-retryable even if it happens to also contain one of
#: these words. Unmatched failures still fall through to `""`, so every
#: scripted `_run_cmd` test that never intended to exercise this keeps its
#: exact prior behaviour.
_INFRA_SIGNALS: tuple[str, ...] = (
    "(http 502)", "(http 503)", "(http 504)", "(http 429)",
    "502 bad gateway", "503 service unavailable", "504 gateway timeout",
    "429 too many requests",
    "connection refused", "no such host", "i/o timeout",
    "connection reset", "eof", "tls handshake timeout",
)


class GitLabInfraError(RuntimeError):
    """`glab` hit a transient infra wall (5xx/network), not a code failure.
    Unlike :class:`GitLabAccessError`, this IS retryable — it becomes
    ``infra_failure`` and flows into the existing `max_infra_retries` loop."""


#: A URL span in glab's output. Go's HTTP client echoes the whole request URL
#: on a transport failure — `Get "https://host/api/v4/projects/g%2Fp/pipeline":
#: dial tcp: lookup host: no such host` — and both the hostname and the project
#: path inside it are OPERATOR-CHOSEN TEXT. Stopping at a quote as well as at
#: whitespace keeps the span from eating a signal that follows without a space.
_URL_SPAN_RE = re.compile(r"https?://[^\s\"']*")


def _operator_echoes(cmd: Sequence[str]) -> tuple[str, ...]:
    """The argv tokens that are operator DATA rather than glab's own vocabulary.

    glab quotes our command line back at us in its errors, so every byte we put
    there — the hostname, the project path, the ci.variables — reappears in the
    text `_is_access_error` classifies. None of it is evidence of anything: a
    project named ``unauthenticated-proxy`` must not be able to turn a DNS blip
    into a permissions wall.

    Flags and short tokens are skipped so a stray ``-F`` or ``api`` cannot chew
    letters out of unrelated words. The last clause is the load-bearing one: a
    token that is itself part of a signal's WORDING is never stripped, so argv's
    own ``glab`` cannot blind us to ``glab auth login``. Both the raw and the
    percent-decoded spelling go in, because the path travels as ``g%2Fp`` on the
    command line and may be echoed either way — and the signal-wording clause
    applies to BOTH spellings, because percent-decoding can MANUFACTURE a
    signal out of a token that carried none: ``glab%20auth%20login`` passes the
    raw check and decodes to the whole of ``glab auth login``, which would then
    blank a real wall out of `_signal_view` and turn it into an infra retry.
    Today's argv cannot carry that (a project path is ``[A-Za-z0-9_.-]`` joined
    by ``/`` and reaches here only as ``%2F``; hostnames cannot hold ``%``; CI
    variables travel in a temp JSON file, not argv), so this is a pin on a
    property, not a fix for an observed failure.
    """
    def is_signal_wording(text: str) -> bool:
        low = text.lower()
        return any(low in sig for sig in _AUTH_SIGNALS)

    echoes: list[str] = []
    for tok in cmd:
        if len(tok) < 4 or tok.startswith("-"):
            continue
        if is_signal_wording(tok):
            continue
        echoes.append(tok)
        decoded = urllib.parse.unquote(tok)
        if decoded != tok and not is_signal_wording(decoded):
            echoes.append(decoded)
    return tuple(echoes)


def _signal_view(text: str, echoed: Sequence[str] = ()) -> str:
    """The part of glab's output an auth signal may legitimately be read from.

    Everything the operator chose is blanked first. Note this strips URL SPANS
    specifically and not "anything in quotes": a genuine OAuth challenge reads
    ``Bearer realm="gitlab", error="insufficient_scope"``, where the signal
    lives inside quotes and must survive.
    """
    view = _URL_SPAN_RE.sub(" ", text)
    for echo in echoed:
        view = view.replace(echo, " ")
    return view.lower()


def _is_access_error(text: str, echoed: Sequence[str] = ()) -> bool:
    view = _signal_view(text, echoed)
    return any(sig in view for sig in _AUTH_SIGNALS)


def _access_reason(text: str, echoed: Sequence[str] = ()) -> str:
    """The ONE line of `glab` output to quote in the blocker a human reads.

    "First non-blank line" is wrong here: glab renders errors in a box whose
    first line is the bare word ``ERROR``, so a no-credential failure quoted as
    "ERROR" tells the operator nothing. Quote the line that actually matched an
    auth signal — that is the line naming the wall.

    Matching uses the same redacted view as :func:`_is_access_error` so the two
    cannot disagree about which line is the wall, but the ORIGINAL line is what
    gets quoted — the human needs to read what glab really printed.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in lines:
        view = _signal_view(line, echoed)
        if any(sig in view for sig in _AUTH_SIGNALS):
            return line
    return lines[0] if lines else "glab reported an authentication failure"


def _is_infra_error(text: str, echoed: Sequence[str] = ()) -> bool:
    view = _signal_view(text, echoed)
    return any(sig in view for sig in _INFRA_SIGNALS)


def _infra_reason(text: str, echoed: Sequence[str] = ()) -> str:
    """Same idiom as `_access_reason`: quote the line that actually matched,
    not just the first (boxed-error) line."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in lines:
        view = _signal_view(line, echoed)
        if any(sig in view for sig in _INFRA_SIGNALS):
            return line
    return lines[0] if lines else "glab reported an infrastructure failure"


def _parse_trigger_output(text: str) -> tuple[str, str]:
    """Extract (pipeline_id, pipeline_url) from the trigger response.

    The POST API returns JSON with `id` + `web_url`; the regex fallback covers
    any non-JSON output so a partial response still yields the pipeline ID.
    """
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("id"):
            return str(data["id"]), data.get("web_url", "")
    except json.JSONDecodeError:
        pass
    m = _PIPELINE_URL_RE.search(text)
    if m:
        return m.group(1), m.group(0)
    m = _PIPELINE_ID_RE.search(text)
    if m:
        return m.group(1), ""
    return "", ""


class GitLabCI(CIBackend):
    """Async CI runner for GitLab — injectable in the orchestrator."""

    name = "gitlab"

    def __init__(
        self,
        project: str,
        hostname: str = "gitlab.acme.net",
        timeout_minutes: int = 60,
        max_infra_retries: int = 2,
        poll_interval: int = 30,
        variables: dict[str, str] | None = None,
        result_parser: str = "pytest",
        _run_cmd: Any = None,  # override for testing
    ):
        self.project = project
        self.hostname = hostname
        self.timeout_minutes = timeout_minutes
        self.max_infra_retries = max_infra_retries
        self.poll_interval = poll_interval
        self.variables = variables or {}
        self.result_parser = result_parser
        self._run_cmd = _run_cmd or _subprocess_run

    async def trigger(
        self,
        branch: str,
        extra_variables: dict[str, str] | None = None,
    ) -> CIResult:
        """Trigger CI and wait. Retry only on infra failures (max_infra_retries)."""
        merged_vars = {**self.variables, **(extra_variables or {})}
        for attempt in range(self.max_infra_retries + 1):
            result = await asyncio.to_thread(
                self._trigger_and_wait, branch, merged_vars
            )
            if not result.infra_failure:
                return result
            if attempt < self.max_infra_retries:
                await asyncio.sleep(_INFRA_BACKOFF_SECONDS)
        return result  # still infra failure after retries

    async def check_status(self, pipeline_id: str) -> CIResult:
        """Non-blocking status check of an existing pipeline."""
        return await asyncio.to_thread(self._check_pipeline, pipeline_id)

    def _access_result(self, pipeline_id: str, pipeline_url: str,
                       exc: GitLabAccessError) -> CIResult:
        """An auth wall as a CIResult: UNKNOWN (never green, never a code
        failure), ``access_failure`` so the retry loop leaves it alone, and the
        exact env key so `orchestrator` can raise a MISSING_ACCESS ask that
        names it."""
        return CIResult(
            pipeline_id=pipeline_id,
            pipeline_url=pipeline_url,
            status=PipelineStatus.UNKNOWN,
            access_failure=True,
            access_env_key=_ACCESS_ENV_KEY,
            parsed_output=(
                f"GitLab refused the request ({exc}) — set {_ACCESS_ENV_KEY} in "
                f"~/.no_human/.env, or run `glab auth login --hostname "
                f"{self.hostname}`. Retrying cannot clear this."
            ),
        )

    def _infra_result(self, pipeline_id: str, pipeline_url: str,
                      exc: GitLabInfraError) -> CIResult:
        """A transient 5xx/network wall as a CIResult: UNKNOWN status,
        ``infra_failure`` so the existing `max_infra_retries` loop in
        `trigger()` retries it, surfaced honestly — never the coder's red."""
        return CIResult(
            pipeline_id=pipeline_id,
            pipeline_url=pipeline_url,
            status=PipelineStatus.UNKNOWN,
            infra_failure=True,
            parsed_output=f"GitLab infrastructure error ({exc}) — not a code failure",
        )

    def _check_pipeline(self, pipeline_id: str) -> CIResult:
        """Blocking single poll of a pipeline."""
        encoded = urllib.parse.quote(self.project, safe="")
        try:
            return self._check_pipeline_inner(pipeline_id, encoded)
        except GitLabAccessError as exc:
            return self._access_result(pipeline_id, "", exc)
        except GitLabInfraError as exc:
            return self._infra_result(pipeline_id, "", exc)

    def _check_pipeline_inner(self, pipeline_id: str, encoded: str) -> CIResult:
        raw_status = self._get_pipeline_status(pipeline_id, encoded)
        if raw_status is None:
            return CIResult(
                pipeline_id=pipeline_id,
                pipeline_url="",
                status=PipelineStatus.UNKNOWN,
                infra_failure=True,
                parsed_output="pipeline status API call failed",
            )
        try:
            status = PipelineStatus(raw_status)
        except ValueError:
            status = PipelineStatus.UNKNOWN

        if status.is_terminal:
            pipeline_url = f"https://{self.hostname}/{self.project}/-/pipelines/{pipeline_id}"
            jobs = self._get_jobs(pipeline_id, encoded)
            if jobs is None:
                if status == PipelineStatus.FAILED:
                    return CIResult(
                        pipeline_id=pipeline_id,
                        pipeline_url=pipeline_url,
                        status=status,
                        infra_failure=True,
                        parsed_output="pipeline jobs API call failed",
                    )
                # jobs are only load-bearing for FAILED (they drive the
                # infra-vs-coder classification below); a jobs-poll blip on a
                # SUCCESS/other terminal status must not mark infra_failure on
                # an otherwise-passing result — that would trip the retry loop
                # into re-triggering a NEW, non-idempotent pipeline for a run
                # that already succeeded.
                jobs = []
            infra = status == PipelineStatus.FAILED and _is_infra_failure(jobs)
            return CIResult(
                pipeline_id=pipeline_id,
                pipeline_url=pipeline_url,
                status=status,
                jobs=jobs,
                infra_failure=infra,
            )
        return CIResult(
            pipeline_id=pipeline_id,
            pipeline_url=f"https://{self.hostname}/{self.project}/-/pipelines/{pipeline_id}",
            status=status,
        )

    def _trigger_and_wait(
        self, branch: str, variables: dict[str, str]
    ) -> CIResult:
        """Blocking: trigger + poll until terminal or timeout.

        The auth wall is caught here rather than deeper so it covers BOTH the
        trigger POST and every status/jobs poll underneath it with one path.
        The infra wall is caught the same way, for the same reason.
        """
        try:
            return self._trigger_and_wait_inner(branch, variables)
        except GitLabAccessError as exc:
            return self._access_result("", "", exc)
        except GitLabInfraError as exc:
            return self._infra_result("", "", exc)

    def _trigger_and_wait_inner(
        self, branch: str, variables: dict[str, str]
    ) -> CIResult:
        pipeline_id, pipeline_url = self._trigger(branch, variables)
        if not pipeline_id:
            return CIResult(
                pipeline_id="",
                pipeline_url="",
                status=PipelineStatus.FAILED,
                infra_failure=True,
                parsed_output="trigger produced no pipeline ID — treating as infra failure",
            )
        return self._wait_for_pipeline(pipeline_id, pipeline_url)

    def _trigger(self, branch: str, variables: dict[str, str]) -> tuple[str, str]:
        encoded = urllib.parse.quote(self.project, safe="")
        body = {
            "ref": branch,
            "variables": [{"key": k, "value": str(v)} for k, v in variables.items()],
        }
        fd, body_path = tempfile.mkstemp(suffix=".json", prefix="nh_ci_")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(body, f)
            cmd = [
                "glab", "api",
                "--hostname", self.hostname,
                "--method", "POST",
                f"projects/{encoded}/pipeline",
                "--input", body_path,
                "--header", "Content-Type: application/json",
            ]
            out = self._run_cmd(cmd)
        finally:
            try:
                os.unlink(body_path)
            except OSError:
                pass
        return _parse_trigger_output(out)

    def _wait_for_pipeline(
        self, pipeline_id: str, pipeline_url: str
    ) -> CIResult:
        import time
        encoded = urllib.parse.quote(self.project, safe="")
        deadline = time.monotonic() + self.timeout_minutes * 60

        while time.monotonic() < deadline:
            raw_status = self._get_pipeline_status(pipeline_id, encoded)
            if raw_status is None:
                # API call failed — treat as infra
                return CIResult(
                    pipeline_id=pipeline_id,
                    pipeline_url=pipeline_url,
                    status=PipelineStatus.FAILED,
                    infra_failure=True,
                    parsed_output="pipeline status API call failed",
                )
            try:
                status = PipelineStatus(raw_status)
            except ValueError:
                status = PipelineStatus.UNKNOWN

            if status.is_terminal:
                jobs = self._get_jobs(pipeline_id, encoded)
                if jobs is None:
                    if status == PipelineStatus.FAILED:
                        return CIResult(
                            pipeline_id=pipeline_id,
                            pipeline_url=pipeline_url,
                            status=status,
                            infra_failure=True,
                            parsed_output="pipeline jobs API call failed",
                        )
                    # See _check_pipeline_inner: jobs only matter for FAILED
                    # classification; don't mark infra_failure on a SUCCESS/
                    # other terminal status just because the jobs poll blipped.
                    jobs = []
                infra = status == PipelineStatus.FAILED and _is_infra_failure(jobs)
                return CIResult(
                    pipeline_id=pipeline_id,
                    pipeline_url=pipeline_url,
                    status=status,
                    jobs=jobs,
                    infra_failure=infra,
                )
            time.sleep(self.poll_interval)

        return CIResult(
            pipeline_id=pipeline_id,
            pipeline_url=pipeline_url,
            status=PipelineStatus.FAILED,
            infra_failure=True,
            parsed_output=f"timed out after {self.timeout_minutes}m",
        )

    def _glab_api(self, path: str) -> Any:
        cmd = ["glab", "api", "--hostname", self.hostname, path]
        text = self._run_cmd(cmd)
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def _get_pipeline_status(
        self, pipeline_id: str, encoded_project: str
    ) -> str | None:
        data = self._glab_api(
            f"projects/{encoded_project}/pipelines/{pipeline_id}"
        )
        if isinstance(data, dict):
            return data.get("status")
        return None

    def _get_jobs(
        self, pipeline_id: str, encoded_project: str
    ) -> list[JobResult] | None:
        """``None`` means the call itself failed (no output / unparseable) —
        distinct from a genuinely empty jobs list, so a broken jobs poll on a
        terminal ``failed`` pipeline is never read as "no failed jobs found"."""
        data = self._glab_api(
            f"projects/{encoded_project}/pipelines/{pipeline_id}/jobs"
        )
        if not isinstance(data, list):
            return None
        return [
            JobResult(
                name=j.get("name", ""),
                status=j.get("status", ""),
                failure_reason=j.get("failure_reason") or None,
                web_url=j.get("web_url", ""),
            )
            for j in data
        ]


def _is_infra_failure(jobs: list[JobResult]) -> bool:
    """True iff all failed jobs have an infra-class failure_reason."""
    failed = [j for j in jobs if j.status == "failed"]
    if not failed:
        return False
    return all(j.failure_reason in _INFRA_REASONS for j in failed)


def _load_gitlab_token() -> None:
    """Make ``~/.no_human/.env``'s GITLAB_TOKEN visible to the `glab` child
    process, the way ``ci/jenkins.py`` loads JENKINS_USER / JENKINS_API_TOKEN.

    Without this the MISSING_ACCESS ask names a key the operator can set with
    no effect. Only the REAL runner calls it — an injected ``_run_cmd`` never
    touches the .env — and an already-exported value always wins, so this never
    overrides the operator's shell (pinned by
    ``test_gitlab_token_already_in_the_environment_is_not_overwritten``; before
    that test existed, deleting the guard below left tests/test_ci.py at 97
    passed, because ``config.load_env_var`` overwrites unconditionally).

    It DOES read the .env in a test that merely stubs ``subprocess.run`` out:
    the stub sits inside :func:`_subprocess_run`, which has already called this.
    An earlier version of this docstring claimed the opposite, and a probe
    refuted it — four of this module's own tests opened the operator's real
    ``~/.no_human/.env``. Latent only while they have no GITLAB_TOKEN in it,
    i.e. until they follow the MISSING_ACCESS instruction this code prints.
    Tests reaching :func:`_subprocess_run` therefore take the
    ``isolated_env_file`` fixture, which repoints ``config.ENV_PATH``.
    """
    if os.environ.get(_ACCESS_ENV_KEY):
        return
    from ..config import load_env_var
    load_env_var(_ACCESS_ENV_KEY)


def _subprocess_run(cmd: list[str]) -> str:
    """Run `glab`; "" on a plain failure, ``GitLabAccessError`` on an auth
    wall, ``GitLabInfraError`` on a transient 5xx/network wall.

    Returning "" for all three is what conflated a 401 (and a 502) with an
    unclassified failure. The empty-string path is kept exactly as it was so
    every scripted test runner (and every genuinely unclassified failure)
    behaves identically.
    """
    _load_gitlab_token()
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode == 0:
        return (proc.stdout or "") + (proc.stderr or "")
    detail = ((proc.stderr or "") + " " + (proc.stdout or "")).strip()
    echoed = _operator_echoes(cmd)
    if _is_access_error(detail, echoed):
        raise GitLabAccessError(_access_reason(detail, echoed))
    if _is_infra_error(detail, echoed):
        raise GitLabInfraError(_infra_reason(detail, echoed))
    return ""
