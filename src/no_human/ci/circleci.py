"""CircleCI CI backend (v2 API) — watch-first, read-only.

WATCH mode (default): CircleCI's VCS integration starts a pipeline on the PR
push; no_human only POLLS it (``GET /project/{slug}/pipeline?branch=…`` → the
pipeline's ``/workflow`` statuses) and never writes. TRIGGER mode is opt-in and
outward-facing (``POST /project/{slug}/pipeline``) — off by default, mirroring
the Jenkins ``watch``/``trigger`` split.

Auth: ``Circle-Token`` header from ``CIRCLECI_TOKEN`` in ~/.no_human/.env (never
config). A 401/403 or a missing token is an *access* failure (a human must grant
access — the orchestrator routes it to MISSING_ACCESS), distinct from a
transient infra failure.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

from .base import CIBackend, CIResult, HumanGatedCI, JobResult, PipelineStatus

_API = "https://circleci.com/api/v2"
_INFRA_BACKOFF_SECONDS = 120  # 2 minutes between infra retries


def _http_get(url: str, headers: dict):
    import httpx
    return httpx.get(url, headers=headers, timeout=30.0)


def _http_post(url: str, headers: dict, json: dict):
    import httpx
    return httpx.post(url, headers=headers, json=json, timeout=30.0)


def _aggregate(statuses: list[str]) -> PipelineStatus:
    """Roll workflow statuses into one pipeline status (worst-wins for failure,
    in-progress beats done)."""
    s = {x for x in statuses if x}
    if not s:
        return PipelineStatus.UNKNOWN
    if s & {"failed", "error"}:
        return PipelineStatus.FAILED
    if s & {"running", "failing"}:
        return PipelineStatus.RUNNING
    if "on_hold" in s:
        return PipelineStatus.MANUAL
    if "canceled" in s:
        return PipelineStatus.CANCELED
    if s <= {"not_run"}:
        # The whole pipeline was filtered out (branch/tag rules, a `when`
        # condition): nothing actually ran, so the pipeline is skipped.
        return PipelineStatus.SKIPPED
    if s <= {"success", "not_run"}:
        # Ran workflows all passed; `not_run` workflows are neutral, not a
        # failure and not pending (CircleCI's own UI shows such a pipeline as
        # Success). Demoting this to SKIPPED would make `passed` False and
        # block an otherwise-green PR whose deploy workflow is branch-filtered.
        return PipelineStatus.SUCCESS
    return PipelineStatus.UNKNOWN


class CircleCICI(CIBackend):
    """Async CircleCI runner. Watch-first: polls the PR-triggered pipeline."""

    name = "circleci"

    def __init__(
        self,
        project_slug: str,
        *,
        mode: str = "watch",
        timeout_minutes: int = 60,
        max_infra_retries: int = 2,
        poll_interval: int = 30,
        result_parser: str = "pytest",
        _get=None,
        _post=None,
    ):
        import os
        self.project_slug = project_slug         # "<vcs>/<org>/<repo>", e.g. "gh/acme/svc"
        self.mode = mode                         # "watch" (default, read-only) | "trigger" (opt-in POST)
        self.timeout_minutes = timeout_minutes
        self.max_infra_retries = max_infra_retries
        self.poll_interval = poll_interval
        self.result_parser = result_parser
        self.token = os.environ.get("CIRCLECI_TOKEN")
        self._get = _get or _http_get
        self._post = _post or _http_post

    def _headers(self) -> dict:
        return {"Circle-Token": self.token or ""}

    def _pipeline_url(self) -> str:
        return f"https://app.circleci.com/pipelines/{self.project_slug}"

    def _access(self, pipeline_id: str = "") -> CIResult:
        return CIResult(
            pipeline_id=pipeline_id, pipeline_url="", status=PipelineStatus.UNKNOWN,
            access_failure=True, access_env_key="CIRCLECI_TOKEN",
            parsed_output="CircleCI access denied — set CIRCLECI_TOKEN in ~/.no_human/.env",
        )

    def _infra(self, pipeline_id: str, msg: str) -> CIResult:
        return CIResult(
            pipeline_id=pipeline_id, pipeline_url="", status=PipelineStatus.UNKNOWN,
            infra_failure=True, parsed_output=msg,
        )

    # ------------------------------ polling ------------------------------- #

    async def check_status(self, pipeline_id: str) -> CIResult:
        """Non-blocking single poll of a pipeline's workflows."""
        return await asyncio.to_thread(self._poll_pipeline, pipeline_id)

    def _poll_pipeline(self, pipeline_id: str) -> CIResult:
        if not self.token:
            return self._access(pipeline_id)
        try:
            resp = self._get(f"{_API}/pipeline/{pipeline_id}/workflow", self._headers())
        except Exception as exc:  # noqa: BLE001
            return self._infra(pipeline_id, f"workflow API unreachable: {type(exc).__name__}")
        if resp.status_code in (401, 403):
            return self._access(pipeline_id)
        if resp.status_code == 404 or resp.status_code >= 500:
            return self._infra(pipeline_id, f"HTTP {resp.status_code}")
        items = (resp.json() or {}).get("items") or []
        statuses = [w.get("status", "") for w in items]
        if "unauthorized" in statuses:
            return self._access(pipeline_id)
        jobs = [JobResult(name=w.get("name", ""), status=w.get("status", "")) for w in items]
        return CIResult(
            pipeline_id=pipeline_id, pipeline_url=self._pipeline_url(),
            status=_aggregate(statuses), jobs=jobs,
        )

    # ------------------------------ trigger ------------------------------- #

    async def trigger(
        self, branch: str, extra_variables: dict[str, str] | None = None
    ) -> CIResult:
        """Watch mode: find the PR-triggered pipeline for ``branch`` and poll it
        to terminal (read-only). Trigger mode (opt-in): POST a new pipeline first.

        Retries only on INFRA failures (transient 5xx/network), up to
        ``max_infra_retries`` with a backoff — matching the gitlab/jenkins
        backends and the project rule that ONLY infra failures are ever
        retried, never a verdict. An access wall or a real test failure is
        never retried; ``HumanGatedCI`` (no pipeline for the branch) propagates."""
        if not self.token:
            return self._access()
        result: CIResult | None = None
        for attempt in range(self.max_infra_retries + 1):
            if self.mode == "trigger":
                pid = await asyncio.to_thread(self._create_pipeline, branch)
            else:
                pid = await asyncio.to_thread(self._latest_pipeline_for, branch)
            if isinstance(pid, CIResult):   # an access/infra failure was surfaced
                result = pid
            elif not pid:
                raise HumanGatedCI(
                    f"No CircleCI pipeline found for branch {branch!r}. Ensure "
                    "CircleCI runs on push, or set ci.mode='trigger' to POST the "
                    "pipeline.", kind="ci", name="circleci",
                )
            else:
                result = await self._wait(pid)
            if not result.infra_failure:
                return result
            if attempt < self.max_infra_retries:
                await asyncio.sleep(_INFRA_BACKOFF_SECONDS)
        return result  # infra failure persisted after the retries

    def _latest_pipeline_for(self, branch: str):
        url = f"{_API}/project/{self.project_slug}/pipeline?branch={quote(branch, safe='')}"
        try:
            resp = self._get(url, self._headers())
        except Exception as exc:  # noqa: BLE001
            return self._infra("", f"pipeline list unreachable: {type(exc).__name__}")
        if resp.status_code in (401, 403):
            return self._access()
        if resp.status_code >= 500:
            return self._infra("", f"HTTP {resp.status_code}")
        items = (resp.json() or {}).get("items") or []
        return items[0].get("id", "") if items else ""

    def _create_pipeline(self, branch: str):
        url = f"{_API}/project/{self.project_slug}/pipeline"
        try:
            resp = self._post(url, self._headers(), {"branch": branch})
        except Exception as exc:  # noqa: BLE001
            return self._infra("", f"pipeline POST unreachable: {type(exc).__name__}")
        if resp.status_code in (401, 403):
            return self._access()
        if resp.status_code >= 400:
            return self._infra("", f"HTTP {resp.status_code}")
        return (resp.json() or {}).get("id", "")

    async def _wait(self, pipeline_id: str) -> CIResult:
        deadline = time.monotonic() + self.timeout_minutes * 60
        infra_strikes = 0
        while True:
            result = await asyncio.to_thread(self._poll_pipeline, pipeline_id)
            if result.access_failure:
                return result
            if result.infra_failure:
                # A transient poll error (5xx/network) over a long watch is
                # expected — tolerate up to max_infra_retries consecutive ones.
                infra_strikes += 1
                if infra_strikes > self.max_infra_retries:
                    return result
                await asyncio.sleep(_INFRA_BACKOFF_SECONDS)
                continue
            infra_strikes = 0  # a good poll clears the streak
            if result.status.is_terminal:
                return result
            if time.monotonic() >= deadline:
                return self._infra(pipeline_id, "timed out waiting for CircleCI")
            await asyncio.sleep(self.poll_interval)
