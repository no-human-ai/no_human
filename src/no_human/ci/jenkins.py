"""Jenkins CI backend (build.example.com) — trigger / watch / human-gated.

Three modes (``ci.mode``):
  - ``watch``       DEFAULT, read-only: poll the build the PR push already
                    spawned on the configured job path. No outward-facing action.
  - ``trigger``     opt-in: POST ``buildWithParameters`` to start a build, then
                    poll it. Triggering remote builds is outward-facing — gate it.
  - ``human_gated`` a person must build an image first (the original Phase-4
                    seam): ``trigger`` raises ``HumanGatedCI`` so the orchestrator
                    parks the task with a wake hint rather than faking the step.

Access is over the Jenkins REST JSON API via ``curl`` (no new runtime dep, reuses
the ``_run_cmd`` subprocess seam so tests inject recorded fixtures). Two auth
modes (``ci.auth``):
  - ``token``  DEFAULT: basic auth from ``JENKINS_USER`` / ``JENKINS_API_TOKEN``.
  - ``cookie`` form-login session cookie (CloudBees build.example.com rejects
               API-token basic auth). The cookie is captured once via a
               Playwright form login (see ``jenkins_session``), reused headlessly
               from the persisted ``storage_state``, auto-refreshed on expiry,
               and a CSRF crumb is attached to write (POST) requests.
Credentials come from ``~/.no_human/.env`` (chmod 600) or the process env —
NEVER from config.yaml or the repo, and never logged.

Trust rule (§3.4): an unreachable or un-run build never reads as green. A status
API that won't answer is treated as an infra failure (park/retry), not a pass.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any

from .base import CIBackend, CIResult, HumanGatedCI, JobResult, PipelineStatus

# Console-log substrings that mark an INFRA failure (agent/network/disk), not a
# real test or compile failure. Matched case-insensitively against the tail.
_INFRA_SIGNATURES = (
    "cannot contact",
    "connection refused",
    "channel is already closed",
    "went offline",
    "backing channel",
    "no space left on device",
    "503 service unavailable",
    "502 bad gateway",
    "queue task was cancelled",
    "hudson.remoting.channelclosedexception",
    "failed to connect to",
    "could not initialize class",
    "timeout waiting for",
)

# Jenkins build `result` → our PipelineStatus.
_RESULT_STATUS = {
    "SUCCESS": PipelineStatus.SUCCESS,
    "UNSTABLE": PipelineStatus.FAILED,   # test failures — a REAL failure
    "FAILURE": PipelineStatus.FAILED,
    "ABORTED": PipelineStatus.CANCELED,  # cancelled/timed-out — treated as infra
    "NOT_BUILT": PipelineStatus.SKIPPED,
}

_INFRA_BACKOFF_SECONDS = 120  # 2 minutes between infra retries
_CONSOLE_TAIL_LINES = 40

# curl -w marker so we can read the HTTP status without -f (which would collapse
# every 4xx/5xx into one opaque non-zero exit). The real runner appends it; the
# test fakes omit it, in which case a returned body is treated as a 200.
_HTTP_MARKER = "__NH_HTTP__"
_ACCESS_CODES = (401, 403, 407)


class JenkinsCI(CIBackend):
    name = "jenkins"

    def __init__(
        self,
        job: str,
        base_url: str = "https://build.example.com",
        mode: str = "watch",
        user: str | None = None,
        token: str | None = None,
        timeout_minutes: int = 60,
        max_infra_retries: int = 2,
        poll_interval: int = 30,
        result_parser: str = "surefire",
        wake_hint: str = "",
        auth: str = "token",
        crumb_path: str = "crumbIssuer/api/json",
        storage_state_path: str | None = None,
        cookie_auto_refresh: bool = True,
        cookie_provider: Any = None,  # Callable[[bool], dict[str, str]]; test seam
        _run_cmd: Any = None,  # override for testing
    ):
        self.job = job.strip("/")
        self.base_url = base_url.rstrip("/")
        self.mode = mode
        self.timeout_minutes = timeout_minutes
        self.max_infra_retries = max_infra_retries
        self.poll_interval = poll_interval
        self.result_parser = result_parser
        self._run_cmd = _run_cmd or _subprocess_run
        # Auth: "token" (basic auth, default) or "cookie" (form-login session
        # cookie — Jenkins here rejects API-token basic auth). Cookie auth also
        # attaches a CSRF crumb on write (POST) requests.
        self.auth = auth
        self.crumb_path = crumb_path.strip("/")
        self.cookie_auto_refresh = cookie_auto_refresh
        self._cookie_provider = cookie_provider
        if storage_state_path is None:
            from .jenkins_session import DEFAULT_STATE
            storage_state_path = DEFAULT_STATE
        self.storage_state_path = storage_state_path
        self._cookies_cache: dict[str, str] | None = None
        self._crumb_cache: tuple[str, str] | None = None
        # Credentials: explicit args win, else the private .env / process env.
        # Loaded lazily and never echoed.
        if user is None or token is None:
            from ..config import load_env_var
            user = user or load_env_var("JENKINS_USER")
            token = token or load_env_var("JENKINS_API_TOKEN")
        self.user = user
        self.token = token
        self.wake_hint = wake_hint or (
            f"Build the Jenkins job {self.job} ({self.base_url}), then reply to "
            "unblock so integration CI can run."
        )

    # ------------------------------ trigger -------------------------------- #

    async def trigger(
        self, branch: str, extra_variables: dict[str, str] | None = None
    ) -> CIResult:
        if self.mode == "human_gated":
            raise HumanGatedCI(
                f"Jenkins job {self.job} must be built by a human before CI can run.",
                wake_hint=self.wake_hint,
            )
        merged_vars = {**(extra_variables or {})}
        result = None
        for attempt in range(self.max_infra_retries + 1):
            result = await asyncio.to_thread(self._run_once, branch, merged_vars)
            if not result.infra_failure:
                return result
            if attempt < self.max_infra_retries:
                await asyncio.sleep(_INFRA_BACKOFF_SECONDS)
        return result  # still infra after retries

    # ------------------------------ internals ------------------------------ #

    def _run_once(self, branch: str, variables: dict[str, str]) -> CIResult:
        """Blocking: (optionally start) then poll one build to terminal."""
        if self.mode == "trigger":
            kind = self._start_build(variables)
            if kind == "access":
                return self._access("triggering the build")
            if kind != "ok":
                return self._infra("could not POST buildWithParameters")
        return self._wait_for_build()

    def _start_build(self, variables: dict[str, str]) -> str:
        qs = "&".join(f"{k}={v}" for k, v in variables.items())
        url = f"{self.base_url}/{self.job}/buildWithParameters"
        if qs:
            url += f"?{qs}"
        # Jenkins returns 201 with an empty body on a successful trigger.
        _body, kind = self._curl(url, method="POST")
        return kind

    def _wait_for_build(self) -> CIResult:
        import time

        build_url = f"{self.base_url}/{self.job}/lastBuild"
        deadline = time.monotonic() + self.timeout_minutes * 60
        while time.monotonic() < deadline:
            body, kind = self._curl(
                f"{build_url}/api/json?tree=building,result,number,url"
            )
            if kind == "access":
                return self._access("reading the build status")
            if kind != "ok" or body is None:
                return self._infra("build status API unreachable")
            meta = _loads(body)
            if meta is None:
                return self._infra("build status API returned non-JSON")
            if meta.get("building"):
                time.sleep(self.poll_interval)
                continue
            return self._finalize_build(meta)
        return self._infra(f"timed out after {self.timeout_minutes}m")

    def _finalize_build(self, meta: dict) -> CIResult:
        number = meta.get("number")
        url = meta.get("url", f"{self.base_url}/{self.job}/{number or 'lastBuild'}")
        raw_result = meta.get("result")
        status = _RESULT_STATUS.get(raw_result or "", PipelineStatus.UNKNOWN)
        build_root = f"{self.base_url}/{self.job}/{number}" if number else \
            f"{self.base_url}/{self.job}/lastBuild"

        if status == PipelineStatus.SUCCESS:
            return CIResult(pipeline_id=str(number or ""), pipeline_url=url,
                            status=status)

        # Failure path: read the test report and the console tail for evidence.
        failing = self._failing_tests(build_root)
        console = self._console_tail(build_root)
        # Real test failures outrank any infra signature: if tests failed, the
        # build is genuinely red regardless of noisy console lines.
        if failing:
            infra = False
        else:
            infra = (
                raw_result == "ABORTED"
                or _console_is_infra(console)
            )
        parsed = _build_failure_report(raw_result, failing, console)
        return CIResult(
            pipeline_id=str(number or ""),
            pipeline_url=url,
            status=status if status.is_terminal else PipelineStatus.FAILED,
            jobs=failing,
            infra_failure=infra,
            parsed_output=parsed,
        )

    def _failing_tests(self, build_root: str) -> list[JobResult]:
        data = self._get_json(
            f"{build_root}/testReport/api/json"
            "?tree=failCount,suites[cases[className,name,status,errorDetails]]"
        )
        if not isinstance(data, dict):
            return []
        out: list[JobResult] = []
        for suite in data.get("suites", []) or []:
            for case in suite.get("cases", []) or []:
                if case.get("status") in ("FAILED", "REGRESSION", "ERROR"):
                    name = ".".join(
                        p for p in (case.get("className"), case.get("name")) if p
                    )
                    out.append(JobResult(
                        name=name or "unknown",
                        status="failed",
                        failure_reason=(case.get("errorDetails") or "")[:300] or None,
                    ))
        return out

    def _console_tail(self, build_root: str) -> str:
        body, kind = self._curl(f"{build_root}/consoleText")
        if kind != "ok" or not body:
            return ""
        lines = body.splitlines()
        return "\n".join(lines[-_CONSOLE_TAIL_LINES:])

    def _infra(self, why: str) -> CIResult:
        # Unreachable CI is not a verdict. All backends (github_actions.py:103,
        # circleci.py:101, gitlab.py:297/:318, ghe_checkruns.py:359) return
        # UNKNOWN + infra_failure here; only a build that actually ran can be
        # FAILED (see _finalize_build).
        return CIResult(
            pipeline_id="", pipeline_url=f"{self.base_url}/{self.job}",
            status=PipelineStatus.UNKNOWN, infra_failure=True, parsed_output=why,
        )

    def _access(self, doing: str) -> CIResult:
        codes = "/".join(map(str, _ACCESS_CODES))
        if self.auth == "cookie":
            # Cookie auth already tried a session refresh before surfacing this;
            # the remediation is to (re)establish the SSO session, not a token.
            # Same UNKNOWN-not-FAILED contract as _infra() above.
            return CIResult(
                pipeline_id="", pipeline_url=f"{self.base_url}/{self.job}",
                status=PipelineStatus.UNKNOWN, access_failure=True,
                access_env_key="SSO_PASSWORD",
                parsed_output=(
                    f"Jenkins denied access ({codes}) while {doing} for job "
                    f"{self.job}. The session cookie is missing/expired and could "
                    "not be refreshed. Set SSO_USERNAME / SSO_PASSWORD in "
                    "~/.no_human/.env (chmod 600) and ensure Playwright is "
                    "installed, or grant the account access to this job."
                ),
            )
        # Token auth: if no token at all, the token is the blocker; if a user but
        # no token, still the token.
        env_key = "JENKINS_API_TOKEN" if not self.token else "JENKINS_USER"
        return CIResult(
            pipeline_id="", pipeline_url=f"{self.base_url}/{self.job}",
            status=PipelineStatus.UNKNOWN, access_failure=True,
            access_env_key=env_key,
            parsed_output=(
                f"Jenkins denied access ({codes}) while "
                f"{doing} for job {self.job}. Set JENKINS_USER / JENKINS_API_TOKEN "
                "in ~/.no_human/.env (chmod 600), or grant the account access to "
                "this job."
            ),
        )

    # --------------------------- http (curl) ------------------------------- #

    def _get_json(self, url: str) -> Any | None:
        body, kind = self._curl(url)
        if kind != "ok" or not body:
            return None
        return _loads(body)

    def _curl(self, url: str, method: str = "GET",
              _allow_refresh: bool = True) -> tuple[str | None, str]:
        """Return (body, kind) where kind is 'ok' | 'access' | 'infra'. We omit
        curl -f so HTTP 4xx/5xx still return a body + status (via -w marker) and
        we can tell 401/403 (access) apart from a 5xx/connection error (infra).

        Auth: cookie mode attaches the session cookie jar (and a CSRF crumb for
        writes); token mode uses basic auth. On an access wall under cookie auth
        we force one session refresh and retry (handles an expired session)."""
        cmd = ["curl", "-sS", "-w", f"\n{_HTTP_MARKER}%{{http_code}}"]
        if method != "GET":
            cmd += ["-X", method]
        if self.auth == "cookie":
            cookies = self._cookies()
            if cookies:
                cmd += ["-b", "; ".join(f"{k}={v}" for k, v in cookies.items())]
            if method != "GET":
                crumb = self._get_crumb()
                if crumb:
                    cmd += ["-H", f"{crumb[0]}: {crumb[1]}"]
        elif self.user and self.token:
            cmd += ["-u", f"{self.user}:{self.token}"]
        cmd.append(url)
        body, kind = _interpret_curl(self._run_cmd(cmd))
        if kind == "access" and self.auth == "cookie" and _allow_refresh:
            # Session likely expired: drop caches, force a re-login, retry once.
            self._crumb_cache = None
            if self._cookies(force_refresh=True):
                return self._curl(url, method, _allow_refresh=False)
        return body, kind

    def _cookies(self, force_refresh: bool = False) -> dict[str, str]:
        """Session cookie jar for cookie auth. Cached per instance; a provider
        (or the default storage_state-backed one) supplies/refreshes it."""
        if force_refresh:
            self._cookies_cache = None
        if self._cookies_cache is None:
            if self._cookie_provider is not None:
                self._cookies_cache = self._cookie_provider(force_refresh) or {}
            else:
                from .jenkins_session import get_session_cookies
                self._cookies_cache = get_session_cookies(
                    self.base_url, self.storage_state_path,
                    auto_refresh=self.cookie_auto_refresh,
                    force_refresh=force_refresh,
                ) or {}
        return self._cookies_cache

    def _get_crumb(self) -> tuple[str, str] | None:
        """Fetch (and cache) the Jenkins CSRF crumb (field, value) for writes.
        Returns None if the crumb issuer is unavailable (older Jenkins with CSRF
        off) — the POST then proceeds without a crumb."""
        if self._crumb_cache is None:
            body, kind = self._curl(f"{self.base_url}/{self.crumb_path}",
                                    _allow_refresh=False)
            if kind == "ok" and body:
                d = _loads(body)
                if isinstance(d, dict) and d.get("crumbRequestField"):
                    self._crumb_cache = (d["crumbRequestField"],
                                         str(d.get("crumb", "")))
        return self._crumb_cache


def _loads(text: str) -> Any | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _interpret_curl(raw: str | None) -> tuple[str | None, str]:
    """Map a raw curl result to (body, kind). None (a non-zero curl exit with no
    HTTP response) is a connection-level infra failure. A trailing _HTTP_MARKER
    carries the status; without it (test fakes) a present body is treated as 200.
    """
    if raw is None:
        return None, "infra"
    sep = f"\n{_HTTP_MARKER}"
    idx = raw.rfind(sep)
    if idx == -1:
        return raw, "ok"  # no marker (fake/legacy) → treat as success
    body = raw[:idx]
    code_str = raw[idx + len(sep):].strip()
    code = int(code_str) if code_str.isdigit() else 0
    if code in _ACCESS_CODES:
        return body, "access"
    if 200 <= code < 300:
        return body, "ok"
    # 5xx, 404, connection-with-no-code → infra (retry/park, not an access ask).
    return None, "infra"


def _console_is_infra(console: str) -> bool:
    low = console.lower()
    return any(sig in low for sig in _INFRA_SIGNATURES)


def _build_failure_report(
    raw_result: str | None, failing: list[JobResult], console: str
) -> str:
    parts = [f"Jenkins result: {raw_result or 'unknown'}"]
    if failing:
        parts.append(f"{len(failing)} failing test(s):")
        for j in failing[:20]:
            line = f"  - {j.name}"
            if j.failure_reason:
                line += f": {j.failure_reason}"
            parts.append(line)
    if console:
        parts.append("Console tail:\n" + console)
    return "\n".join(parts)


def _subprocess_run(cmd: list[str]) -> str | None:
    """Run curl and return stdout (body + the trailing _HTTP_MARKER status line).
    We do NOT pass -f, so an HTTP response — even 4xx/5xx — exits 0 and its status
    rides in the marker for _interpret_curl to classify. A non-zero exit means a
    connection-level failure (no HTTP response) → None → infra."""
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return None
    return proc.stdout or ""
