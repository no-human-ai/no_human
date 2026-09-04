"""The UI-evidence runner: a coder-authored browser walk, harness-executed.

TRUST BOUNDARY. The coder writes ``.no_human/ui_evidence.json`` — a small,
bounded manifest choosing which selectors to click and which moments to
screenshot. This module is what the HARNESS runs, not the coder: it drives
a real (or injected fake, in tests) Playwright page through those steps and
captures whatever the page actually showed — screenshots, a video, console
errors — into ``out_dir``. A screenshot proves what the page rendered at the
instant that step ran, and nothing more: not that the feature works, not
that anything else on the page is correct, not that the steps chosen were
the right ones. The coder chose the walk; the harness took the pictures.

Mirrors the shape of :mod:`no_human.testing.repro_gate`: a small manifest
under ``.no_human/**``, never committed (``.no_human/`` is excluded from
every commit — see ``MANIFEST``), read by a pure function that degrades
loudly (``not_run`` with a named reason) rather than raising or silently
skipping.

Playwright is an OPTIONAL dependency (the ``e2e`` extras group,
``pyproject.toml``) — it is imported lazily, only inside :func:`run`, via
the :func:`_import_playwright` seam, so this module and its test suite work
with no browser installed. :func:`run` never raises; every failure mode is a
``UiEvidenceResult`` with a documented reason.

Conventions: step indices are 0-based everywhere (manifest order); artefact
paths recorded in the result are bare filenames relative to ``out_dir``
(shots and the video are always co-located there); writing artefacts is an
idempotent overwrite — a re-run replaces same-named files but does not
delete stale files left by a prior run with different shot names, so a
renderer should trust ``result.json``, not a directory listing. This module
never touches ``~/.no_human`` — ``out_dir`` is entirely caller-supplied.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import os
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .. import config, proc

MANIFEST = ".no_human/ui_evidence.json"
SCHEMA_HINT = (
    '{"base_url": "http://127.0.0.1:5173", "steps": [{"goto": "/"}, ...]}'
)

_ACTIONS = ("goto", "wait_for", "click", "fill", "select", "shot", "assert_text", "press")
_MAX_STEPS = 40
_MAX_SHOTS = 12
_DEFAULT_TIMEOUT_MS = 10_000
_MAX_TIMEOUT_MS = 60_000
_SHOT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
_PROBE_TIMEOUT_S = 5.0
_MAX_CONSOLE = 50
_CONSOLE_CHARS = 300
_DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
_FINAL_SHOT_TIMEOUT_S = 10.0


def default_out_dir(task_id: str) -> Path:
    """A fresh scratch directory for one `run()` call's raw artifacts
    (shots, video, `manifest.json`/`console.json`/`result.json`) — never
    inside the target repo (this module's docstring: "never touches
    ``~/.no_human``"; a caller does not need it to, either — it is a plain
    OS temp directory, cleaned up by the caller once it has read back
    whatever it wants to keep, e.g. `core/orchestrator.py`'s attempt-time
    delivery step, which copies out only the specific shot/video files it
    is about to commit elsewhere and never trusts this directory to
    persist).

    Centralized here (D1.2) rather than left to each caller's own
    `tempfile.mkdtemp` call so there is exactly one place that decides the
    naming convention — a caller can still pass any `out_dir` it likes
    directly to :func:`run`; this is a convenience, not a requirement.
    """
    return Path(tempfile.mkdtemp(prefix=f"nh-ui-evidence-{task_id[:8]}-"))


@dataclass(frozen=True)
class Step:
    action: str
    value: str
    text: str | None
    timeout_ms: int
    index: int


@dataclass(frozen=True)
class Manifest:
    base_url: str
    viewport: dict
    steps: tuple[Step, ...]
    raw: dict


@dataclass
class UiEvidenceResult:
    verdict: str  # "ran" | "not_run" | "failed"
    reason: str = ""
    shots: list = field(default_factory=list)
    video: str | None = None
    console_errors: list = field(default_factory=list)
    steps_run: int = 0
    steps_total: int = 0
    duration_s: float = 0.0
    manifest: dict = field(default_factory=dict)
    failed_step: int | None = None
    failed_action: str | None = None

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "shots": list(self.shots),
            "video": self.video,
            "console_errors": list(self.console_errors),
            "steps_run": self.steps_run,
            "steps_total": self.steps_total,
            "duration_s": round(self.duration_s, 3),
            "manifest": self.manifest,
            "failed_step": self.failed_step,
            "failed_action": self.failed_action,
        }


# --------------------------------------------------------------------------
# Validation — `_load` (file → JSON) + `_parse` (JSON → Manifest) are the
# ONE shared path for both `manifest_problem` and `read_manifest`, so the
# two cannot drift into disagreeing about what is valid.
# --------------------------------------------------------------------------


def _load(repo_path: Path) -> tuple[object | None, str | None]:
    """Read + JSON-decode the manifest file. Returns (data, problem).

    Both None means the file is absent. `problem` set means data is None —
    the file exists but could not be read or parsed.
    """
    p = repo_path / MANIFEST
    try:
        text = p.read_text(errors="replace")
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"{MANIFEST} is present but unreadable ({exc.__class__.__name__})"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"{MANIFEST} is not valid JSON ({exc.msg} at line {exc.lineno})"
    return data, None


def _hostname_problem(value: str, parsed) -> str | None:
    hostname = parsed.hostname or ""
    if hostname not in _LOCAL_HOSTS:
        return (
            f"must be http(s) on 127.0.0.1 or localhost — the runner drives "
            f"the attempt's own dev server, never a remote host; got `{value}`"
        )
    return None


def _base_url_problem(value) -> str | None:
    if not isinstance(value, str) or not value:
        return f"base_url is missing or not a non-empty string — expected {SCHEMA_HINT}"
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        return f"base_url `{value}` is not a parseable URL ({exc})"
    if parsed.scheme not in ("http", "https"):
        return (
            f"base_url must be http(s) on 127.0.0.1 or localhost — the "
            f"runner drives the attempt's own dev server, never a remote "
            f"host; got `{value}`"
        )
    return _hostname_problem(value, parsed)


def _goto_local_problem(value: str) -> str | None:
    """None for a value that stays on the attempt's own host once resolved.

    A bare relative path (``/foo``, ``foo/bar``) always resolves under the
    manifest's own ``base_url`` host via ``urljoin`` and is safe. A value
    carrying a netloc — whether a full absolute URL (``http://evil.com/``)
    OR a protocol-relative one (``//evil.com/x``, empty scheme, non-empty
    netloc — ``urljoin`` still replaces the authority component with it) —
    must be checked against the same local-host rule as ``base_url``.
    """
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        return f"is not a parseable URL ({exc})"
    if not parsed.scheme and not parsed.netloc:
        return None
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return f"scheme must be http(s), got `{parsed.scheme}`"
    return _hostname_problem(value, parsed)


def _viewport_problem(viewport) -> str | None:
    if not isinstance(viewport, dict):
        return f"viewport must be an object — expected {SCHEMA_HINT}"
    for name in ("width", "height"):
        v = viewport.get(name)
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0 or v > 10000:
            return f"viewport.{name} must be a positive integer <= 10000"
    return None


def _step_problem(raw_step, i: int, shot_names: set) -> tuple[Step | None, str | None]:
    if not isinstance(raw_step, dict):
        return None, f"step {i} is {type(raw_step).__name__}, not an object"
    extra = set(raw_step) - set(_ACTIONS) - {"text", "timeout_ms"}
    if extra:
        return None, f"step {i} has unknown key(s): {', '.join(sorted(extra))}"
    action_keys = [k for k in _ACTIONS if k in raw_step]
    if not action_keys:
        return None, f"step {i} has no action key — expected one of {_ACTIONS}"
    if len(action_keys) > 1:
        return None, f"step {i} has multiple action keys: {', '.join(sorted(action_keys))}"
    action = action_keys[0]
    value = raw_step[action]
    if not isinstance(value, str) or not value:
        return None, f"step {i} ({action}) value must be a non-empty string"

    text = raw_step.get("text")
    if action in ("fill", "select"):
        if not isinstance(text, str) or not text:
            return None, f"step {i} ({action}) requires a non-empty \"text\""
    elif "text" in raw_step:
        return None, (
            f"step {i} ({action}) — \"text\" is only valid on a \"fill\" or "
            "\"select\" step"
        )

    if action == "goto":
        problem = _goto_local_problem(value)
        if problem:
            return None, f"step {i} (goto) {problem}"

    if action == "shot":
        if not _SHOT_RE.match(value):
            return None, (
                f"step {i} (shot) name `{value}` does not match "
                f"{_SHOT_RE.pattern}"
            )
        if value == "final":
            return None, f"step {i} (shot) uses the reserved name `final`"
        if value in shot_names:
            return None, f"step {i} (shot) duplicates shot name `{value}`"

    timeout_ms = raw_step.get("timeout_ms", _DEFAULT_TIMEOUT_MS)
    if (
        isinstance(timeout_ms, bool)
        or not isinstance(timeout_ms, int)
        or timeout_ms <= 0
        or timeout_ms > _MAX_TIMEOUT_MS
    ):
        return None, (
            f"step {i} ({action}) timeout_ms must be an integer in "
            f"(0, {_MAX_TIMEOUT_MS}]"
        )

    step = Step(
        action=action,
        value=value,
        text=text if action in ("fill", "select") else None,
        timeout_ms=timeout_ms,
        index=i,
    )
    return step, None


def _parse(data) -> tuple[Manifest | None, str | None]:
    """JSON document -> (Manifest, None) or (None, problem sentence)."""
    if not isinstance(data, dict):
        return None, (
            f"{MANIFEST} top level is {type(data).__name__}, not an object "
            f"— expected {SCHEMA_HINT}"
        )

    base_url = data.get("base_url")
    problem = _base_url_problem(base_url)
    if problem:
        return None, f"{MANIFEST}: {problem}"

    viewport = data.get("viewport", _DEFAULT_VIEWPORT)
    if viewport is not _DEFAULT_VIEWPORT:
        problem = _viewport_problem(viewport)
        if problem:
            return None, f"{MANIFEST}: {problem}"

    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list):
        return None, (
            f'{MANIFEST}: "steps" is missing or not a list — expected {SCHEMA_HINT}'
        )
    if not steps_raw:
        return None, f'{MANIFEST}: "steps" is empty'
    if len(steps_raw) > _MAX_STEPS:
        return None, (
            f'{MANIFEST}: "steps" has {len(steps_raw)} entries, more than '
            f"the max {_MAX_STEPS}"
        )

    steps: list[Step] = []
    shot_names: set = set()
    shot_count = 0
    for i, raw_step in enumerate(steps_raw):
        step, problem = _step_problem(raw_step, i, shot_names)
        if problem:
            return None, f"{MANIFEST}: {problem}"
        steps.append(step)
        if step.action == "shot":
            shot_names.add(step.value)
            shot_count += 1

    if shot_count > _MAX_SHOTS:
        return None, (
            f"{MANIFEST}: {shot_count} shot steps, more than the max {_MAX_SHOTS}"
        )

    manifest = Manifest(
        base_url=base_url,
        viewport=dict(viewport) if isinstance(viewport, dict) else dict(_DEFAULT_VIEWPORT),
        steps=tuple(steps),
        raw=data,
    )
    return manifest, None


def manifest_problem(repo_path: Path) -> str | None:
    """One sentence naming the first defect, or None if absent/usable."""
    p = repo_path / MANIFEST
    if not p.is_file():
        return None
    data, problem = _load(repo_path)
    if problem:
        return problem
    _, problem = _parse(data)
    return problem


def read_manifest(repo_path: Path) -> Manifest:
    """The parsed manifest. Raises ValueError with the problem sentence."""
    p = repo_path / MANIFEST
    if not p.is_file():
        raise ValueError(f"no {MANIFEST} manifest")
    data, problem = _load(repo_path)
    if problem:
        raise ValueError(problem)
    manifest, problem = _parse(data)
    if problem:
        raise ValueError(problem)
    return manifest


# --------------------------------------------------------------------------
# Seams
# --------------------------------------------------------------------------


def _import_playwright():
    """The `async_playwright` factory, or None if playwright is not installed.

    The only reference to `playwright.async_api` in this module — never
    imported at module scope, so importing `ui_evidence` never requires
    it. Loop-safe: importing a module touches no event loop, so this
    returns the same value whether called from a plain sync context or
    from inside a running `asyncio` loop. Tests monkeypatch this symbol
    directly.
    """
    try:
        from playwright.async_api import async_playwright
    except Exception:
        # Broader than ImportError: a corrupted/partial install can raise
        # other errors at import time (e.g. a broken native extension);
        # any of those means "not usable" here, not a crash.
        return None
    return async_playwright


MISSING_PLAYWRIGHT_REASON = "playwright not installed (uv sync --group e2e)"


def playwright_available() -> bool:
    """True when the `playwright` package imports in this environment.
    Pure/read-only; never raises.

    Loop-safe by construction: the only work is `_import_playwright`,
    which touches `playwright.async_api` and starts no driver, so this
    returns the SAME value whether called synchronously (`nh doctor
    --fix-walks`) or from inside a running `asyncio` loop (`nh doctor`'s
    async `_go`, `_maybe_capture_ui_evidence`) — pinned by
    `tests/test_ui_evidence_playwright_probe_parity.py::test_probe_agrees_between_sync_and_async_contexts`.

    This module previously also resolved the chromium binary's path via
    `playwright.sync_api.sync_playwright()`. That facade raises whenever
    it is started inside a running event loop, and both production call
    sites (`_maybe_capture_ui_evidence`, `nh doctor`'s `_go`) run under
    one — so the binary check made this function return False for every
    provisioned user, unconditionally. It has been removed; this now
    checks package presence only.

    Narrowed contract: a package-present/binary-missing partial install
    now reads **available**. The walk then runs, `_open`'s browser launch
    fails, `result.shots` stays empty, and `_maybe_capture_ui_evidence`
    discloses that (`"the walk captured no shots"`) rather than rendering
    `""` — see `orchestrator.py`'s `_ui_evidence_skipped` paths.
    """
    return _import_playwright() is not None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a 3xx from the probed server is the answer
    ("it is up"), never an instruction to open a second connection. Without
    this the default handler would follow `Location:` to ANY host, which is
    the one way a loopback-validated base_url could make this process talk
    to the outside."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _reachable(base_url: str) -> bool:
    """One local HTTP GET probe. Fails closed (False) on anything but 2xx/3xx/4xx/5xx.

    A `ProxyHandler({})` opener means a shell HTTP_PROXY cannot reroute a
    127.0.0.1 probe to somewhere else, and `_NoRedirect` means a `Location:`
    header cannot either — the probe opens exactly one connection, to
    base_url, and follows nothing.
    """
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        opener.open(base_url, timeout=_PROBE_TIMEOUT_S)
        return True
    except urllib.error.HTTPError:
        # A dev server 404/500 on `/` is still a running server.
        return True
    except Exception:
        return False


_READY_POLL_S = 0.5
_READY_TIMEOUT_MIN, _READY_TIMEOUT_MAX = 1, 300
_KILL_GRACE_S = 5.0
_DEV_SERVER_LOG = "dev-server.log"
_BUILD_LOG = "ui-build.log"
_BUILD_TIMEOUT_DEFAULT_S = 300
_BUILD_TIMEOUT_MIN, _BUILD_TIMEOUT_MAX = 1, 3600
_BUILD_TAIL_LINES = 10


@dataclass  # NOT frozen — teardown stamps exit_code/detail onto the caller's object
class DevServerOutcome:
    """What `dev_server` decided and (if it spawned anything) what happened.

    `mode` is one of ``pre-existing`` (something already answered at
    `base_url` — never killed, replaced, or checkout-verified), ``booted``
    (the harness spawned `start_cmd` and it became reachable), ``unconfigured``
    (no `start_cmd`/`base_url` — the byte-identical-to-today path), or
    ``boot-failed`` (spawn/host/timeout/early-exit/build failure — see
    `detail`).
    `detail` is LOG-ONLY: never rendered into a PR body, only an advisory.

    `cause` is a fixed vocabulary and therefore safe to render (unlike
    `detail`): ``""`` (not a failure), ``"timeout"`` (spawned, never answered
    before `ready_timeout_s`), ``"failed-to-start"`` (never became a
    polling server — non-loopback refusal, unparsable `start_cmd`, spawn
    `OSError`, or early process exit), ``"build-failed"`` (the optional
    `build_cmd` exited nonzero, was unparsable, or raised an `OSError` —
    `exit_code` is the BUILD's exit code here, `None` when there wasn't one
    to report), or ``"build-timeout"`` (`build_cmd` did not finish within
    `build_timeout_s`; `exit_code` is always `None`). Only meaningful when
    `mode == "boot-failed"`.
    """

    mode: str
    start_cmd: str = ""
    base_url: str = ""  # the manifest's, for the caller's disclosure
    ready_timeout_s: int = 0  # clamped, for the caller's skip line
    waited_s: float = 0.0
    exit_code: int | None = None
    detail: str = ""  # LOG-ONLY — never rendered into a PR body
    cause: str = ""  # "" | "timeout" | "failed-to-start" | "build-failed" | "build-timeout"


def _build_argvs(build_cmd: str) -> "list[list[str]] | None":
    """`a && b` -> [argv(a), argv(b)]. None when unparsable/empty — no shell
    is ever spawned, so `&&` is the ONLY operator honored; `;`, `|`, `>` and
    friends stay literal argv tokens, exactly as `start_cmd` treats them."""
    segments = build_cmd.split("&&")
    argvs: list[list[str]] = []
    for segment in segments:
        segment = segment.strip()
        if not segment:
            return None
        try:
            argv = shlex.split(segment)
        except ValueError:
            return None
        if not argv:
            return None
        argvs.append(argv)
    return argvs or None


def _run_build(repo_path, argvs, out_dir: Path, timeout_s: float, *,
                run=subprocess.run) -> "tuple[str, int | None, str]":
    """Run each `argvs` segment sequentially, `shell=False`, stopping at the
    first failure. Never raises. Returns `(cause, exit_code, detail)`:
    `cause` is `""` on success, else `"build-timeout"` or `"build-failed"` —
    the caller's `DevServerOutcome.cause` vocabulary, decided HERE (not
    inferred from a `None` exit code, which timeout/unparsable/OSError all
    share). `detail` is a LOG-ONLY, newline-folded, size-capped tail of the
    combined output — same convention `dev_server`'s own `detail` already
    uses. `exit_code` is only ever the BUILD's own exit code (`None` unless
    a segment actually ran to completion and exited nonzero)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / _BUILD_LOG
    started = time.monotonic()
    with open(log_path, "ab") as fh:
        for argv in argvs:
            remaining = timeout_s - (time.monotonic() - started)
            if remaining <= 0:
                return "build-timeout", None, _build_tail(log_path, "build timed out")
            try:
                proc = run(argv, cwd=str(repo_path), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                if exc.output:
                    fh.write(exc.output if isinstance(exc.output, bytes)
                              else exc.output.encode("utf-8", "replace"))
                    fh.flush()
                return "build-timeout", None, _build_tail(log_path, "build timed out")
            except OSError as exc:
                fh.write(f"{type(exc).__name__}: {exc}\n".encode())
                fh.flush()
                return "build-failed", None, _build_tail(
                    log_path, f"{type(exc).__name__}: {exc}")
            fh.write(proc.stdout or b"")
            fh.flush()
            if proc.returncode != 0:
                return "build-failed", proc.returncode, _build_tail(
                    log_path, f"build exit {proc.returncode}")
    return "", 0, ""


def _build_tail(log_path: Path, prefix: str) -> str:
    """Last `_BUILD_TAIL_LINES` lines of `log_path`, newline-folded and
    capped — LOG-ONLY, mirrors `dev_server`'s own `detail` convention."""
    try:
        text = log_path.read_bytes()[-8192:].decode("utf-8", "replace")
    except OSError:
        text = ""
    lines = text.splitlines()[-_BUILD_TAIL_LINES:]
    detail = f"{prefix}: " + " ".join(line.strip() for line in lines)
    return detail[:2048]


def _kill_dev_server(process) -> None:
    """Stop a booted dev server. SIGTERM-first (not `runner._kill_process_tree`'s
    shape) so the server can release its port; escalates to SIGKILL only if it
    ignores the grace period. Never raises."""
    with contextlib.suppress(Exception):
        if os.name == "nt":
            process.terminate()
            with contextlib.suppress(Exception):
                process.wait(timeout=_KILL_GRACE_S)
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        try:
            process.wait(timeout=_KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            with contextlib.suppress(Exception):
                process.wait(timeout=_KILL_GRACE_S)


@contextlib.asynccontextmanager
async def dev_server(
    repo_path, ui_conf, base_url, out_dir, *,
    spawn=subprocess.Popen, clock=time.monotonic,
    sleep=asyncio.sleep, reachable=_reachable,
    kill=_kill_dev_server, extra_env: dict | None = None,
    build_run=subprocess.run,
):
    """Boot the repo's configured dev server if nothing already answers at
    `base_url`, poll for readiness, and tear it down (kill the process group)
    on exit — normal or exceptional. `base_url` must be the MANIFEST's (the
    URL the walk will actually hit), never the profile's — `ui_conf["base_url"]`
    is never read here, only `start_cmd`/`ready_path`/`ready_timeout_s`.

    `out_dir` is a required positional: the orchestrator owns its lifecycle
    and already `rmtree`s it after the attempt. Every seam is injectable so
    tests never spawn a real subprocess, hit the network, or open a browser.

    `extra_env` is merged over `os.environ` for the spawned process only
    (e.g. `VITE_API_TARGET`, so a booted dev server proxies to a hermetic
    backend rather than the operator's live one) — when falsy the `spawn(...)`
    call passes NO `env=` kwarg at all, so the customer path (no hermetic
    backend involved) is byte-identical to before this parameter existed.

    `ui_conf["build_cmd"]` (optional) runs in `repo_path`, `shell=False`,
    BEFORE `start_cmd` is spawned — but only once every earlier branch has
    already decided a server is genuinely about to be booted (a manifest
    `base_url` that is loopback and not already answering, plus a parsable
    `start_cmd`). `&&`-separated segments run sequentially; a nonzero exit,
    an unparsable command, or exceeding `build_timeout_s` (default 300,
    clamped [1, 3600]) yields `boot-failed`/`build-failed`|`build-timeout`
    and spawns nothing — a disclosed skip, never an exception. When
    `build_cmd` is unset, not one new statement runs before the spawn.

    Decision order — each early branch yields exactly one `DevServerOutcome`
    and spawns nothing: falsy/unconfigured `base_url` or `start_cmd` ->
    `unconfigured`; something already reachable at `base_url` -> `pre-existing`
    (never killed/replaced); a non-loopback `base_url` host -> `boot-failed`
    (refused); an unparsable/failing/timing-out `build_cmd` -> `boot-failed`;
    otherwise spawn `start_cmd` and poll `base_url + ready_path` until
    reachable, the process exits early, or `ready_timeout_s` elapses.
    """
    ui_conf = ui_conf or {}
    start_cmd = str(ui_conf.get("start_cmd") or "").strip()
    ready_path = str(ui_conf.get("ready_path") or "/")
    build_cmd = str(ui_conf.get("build_cmd") or "").strip()
    try:
        # `.get(..., 60)` (not `or 60`) so an explicit 0 clamps to the floor
        # instead of falling back to the default — 0 is falsy but valid input.
        timeout = int(ui_conf.get("ready_timeout_s", 60))
    except (TypeError, ValueError):
        timeout = 60
    timeout = min(max(timeout, _READY_TIMEOUT_MIN), _READY_TIMEOUT_MAX)
    try:
        build_timeout = int(ui_conf.get("build_timeout_s", _BUILD_TIMEOUT_DEFAULT_S))
    except (TypeError, ValueError):
        build_timeout = _BUILD_TIMEOUT_DEFAULT_S
    build_timeout = min(max(build_timeout, _BUILD_TIMEOUT_MIN), _BUILD_TIMEOUT_MAX)

    if not base_url:
        yield DevServerOutcome(mode="unconfigured", start_cmd=start_cmd,
                                base_url=base_url, ready_timeout_s=timeout)
        return

    host = urlsplit(base_url).hostname or ""
    if host not in _LOCAL_HOSTS and f"[{host}]" not in _LOCAL_HOSTS:
        yield DevServerOutcome(mode="boot-failed", start_cmd=start_cmd,
                                base_url=base_url, ready_timeout_s=timeout,
                                detail="refused: non-loopback host",
                                cause="failed-to-start")
        return

    try:
        already_up = reachable(base_url)
    except Exception:
        already_up = False
    if already_up:
        yield DevServerOutcome(mode="pre-existing", start_cmd=start_cmd,
                                base_url=base_url, ready_timeout_s=timeout)
        return

    if not start_cmd:
        yield DevServerOutcome(mode="unconfigured", start_cmd=start_cmd,
                                base_url=base_url, ready_timeout_s=timeout)
        return

    try:
        argv = shlex.split(start_cmd)
    except ValueError as exc:
        yield DevServerOutcome(mode="boot-failed", start_cmd=start_cmd,
                                base_url=base_url, ready_timeout_s=timeout,
                                detail=f"unparsable start_cmd: {exc}",
                                cause="failed-to-start")
        return
    if not argv:
        yield DevServerOutcome(mode="boot-failed", start_cmd=start_cmd,
                                base_url=base_url, ready_timeout_s=timeout,
                                detail="unparsable start_cmd",
                                cause="failed-to-start")
        return

    if build_cmd:
        argvs = _build_argvs(build_cmd)
        if argvs is None:
            yield DevServerOutcome(mode="boot-failed", start_cmd=start_cmd,
                                    base_url=base_url, ready_timeout_s=timeout,
                                    cause="build-failed",
                                    detail=f"unparsable build_cmd: {build_cmd[:200]}")
            return
        cause, exit_code, detail = _run_build(
            repo_path, argvs, Path(out_dir), build_timeout, run=build_run)
        if cause:
            yield DevServerOutcome(mode="boot-failed", start_cmd=start_cmd,
                                    base_url=base_url, ready_timeout_s=timeout,
                                    exit_code=exit_code, cause=cause, detail=detail)
            return

    out_dir = Path(out_dir)
    process = None
    fh = None
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        fh = open(out_dir / _DEV_SERVER_LOG, "wb")
        spawn_kwargs = dict(cwd=str(repo_path), stdout=fh, stderr=subprocess.STDOUT,
                             **proc.hidden_console_kwargs(new_group=True))
        if extra_env:
            spawn_kwargs["env"] = {**os.environ, **extra_env}
        process = spawn(argv, **spawn_kwargs)
    except OSError as exc:
        if fh is not None:
            with contextlib.suppress(Exception):
                fh.close()
        yield DevServerOutcome(mode="boot-failed", start_cmd=start_cmd,
                                base_url=base_url, ready_timeout_s=timeout,
                                detail=f"{type(exc).__name__}: {exc}",
                                cause="failed-to-start")
        return

    probe_url = base_url + ready_path
    outcome = DevServerOutcome(mode="boot-failed", start_cmd=start_cmd,
                                base_url=base_url, ready_timeout_s=timeout)
    started = clock()
    try:
        while clock() - started < timeout:
            rc = process.poll()
            if rc is not None:
                outcome.exit_code = rc
                outcome.cause = "failed-to-start"
                break
            try:
                ok = reachable(probe_url)
            except Exception:
                ok = False
            if ok:
                outcome.mode = "booted"
                break
            await sleep(_READY_POLL_S)
        else:
            outcome.exit_code = process.poll()
            outcome.cause = "timeout"
        outcome.waited_s = clock() - started

        try:
            yield outcome
        finally:
            # `poll() is None` guards this so a process that exited early (or
            # was already reaped) is never killed twice.
            with contextlib.suppress(Exception):
                if process.poll() is None:
                    kill(process)
            with contextlib.suppress(Exception):
                outcome.exit_code = process.poll()
            with contextlib.suppress(Exception):
                if fh is not None:
                    fh.flush()
            with contextlib.suppress(Exception):
                log_bytes = (out_dir / _DEV_SERVER_LOG).read_bytes()[-2048:]
                outcome.detail = log_bytes.decode("utf-8", "replace").replace("\n", " ")
    finally:
        if fh is not None:
            with contextlib.suppress(Exception):
                fh.close()


def _pick_ephemeral_port() -> int:
    """Bind to an OS-assigned loopback port, read it back, and release it.

    Injectable as `pick_port=`. A TOCTOU race — something else grabs the
    port between this call returning and the caller's `spawn` binding it —
    is an accepted failure mode: it surfaces as an ordinary spawn/never-ready
    failure (`mode="failed"`), the same shape as any other boot failure, not
    a crash.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _hermetic_start_argv(port: int) -> list[str]:
    """argv for `nh start` under THIS interpreter, isolated only by the
    caller's env (`HOME`/`NO_HUMAN_HOME`) — never a new CLI surface.

    Mirrors `repro_gate._pytest_python`'s frozen/non-frozen split: in a
    PyInstaller build `sys.executable` IS the `nh` binary already (there is
    no separate Python to hand a module path to), so it is invoked directly.
    In a normal install `sys.executable` is the interpreter, and
    `no_human.cli.commands` (the module backing the `nh`/`no-human` console
    scripts, `pyproject.toml`'s `[project.scripts]`) has a
    `if __name__ == "__main__"` guard, so `-m` reaches the exact same
    `cli()` Click group a PATH `nh` would — preferred over a PATH lookup so
    a customer install with no `nh` on PATH fails loudly into the skip path
    instead of silently resolving to something else.
    """
    base = (
        [sys.executable] if getattr(sys, "frozen", False)
        else [sys.executable, "-m", "no_human.cli.commands"]
    )
    return base + [
        "start", "--host", "127.0.0.1", "--port", str(port),
        "--no-open", "--workers", "1",
    ]


_HERMETIC_API_LOG = "hermetic-api.log"
_HERMETIC_READY_PATH = "/api/tasks"
_HERMETIC_READY_TIMEOUT_S = 60


def _log_tail(path, limit: int = 2048) -> str:
    """Best-effort read of a spawned process's captured log: newline-collapsed
    and length-capped, same shape `dev_server` stamps into its own `detail`
    post-teardown. Never raises — a missing file, a permission error, or a
    log that hasn't been flushed yet all just yield `""`. Callers must be
    able to call this BEFORE yielding a failure `HermeticBackend`, so a
    reader of `hb.detail` during the `yield` (the orchestrator's
    `walk_skip::hermetic_backend_init_failed:` advisory) never sees an empty
    string for a cause where a log actually exists."""
    try:
        data = Path(path).read_bytes()[-limit:]
    except Exception:
        return ""
    return data.decode("utf-8", "replace").replace("\n", " ").strip()


@dataclass  # NOT frozen — teardown stamps detail onto the caller's object
class HermeticBackend:
    """What `hermetic_backend` decided and (if it spawned anything) what
    happened.

    `mode` is `"armed"` (a fresh, throwaway-HOME `nh start` answered before
    `_HERMETIC_READY_TIMEOUT_S`) or `"failed"` (never got there — see
    `cause`). `detail` is LOG-ONLY, same promise as `DevServerOutcome.detail`:
    never rendered into a PR body, only an advisory.

    `cause` is a fixed vocabulary, safe to render (unlike `detail`): ``""``
    (armed), ``"port-unavailable"`` (the picked port could not be bound),
    ``"home-seed-failed"`` (the throwaway HOME could not be created/seeded),
    ``"spawn-failed"`` (the API server process never started),
    ``"exited-early"`` (it started and exited before answering), or
    ``"timeout"`` (it never answered before the deadline). Only meaningful
    when `mode == "failed"`.
    """

    mode: str
    home: str = ""
    port: int = 0
    api_target: str = ""
    detail: str = ""
    cause: str = ""


@contextlib.asynccontextmanager
async def hermetic_backend(
    out_dir, *, spawn=subprocess.Popen, pick_port=_pick_ephemeral_port,
    clock=time.monotonic, sleep=asyncio.sleep, reachable=_reachable,
    kill=_kill_dev_server, home_root=None, auth_mode: str = "subscription",
):
    """Boot an ISOLATED `nh start` under a throwaway HOME so a walk step that
    clicks Save/Reset-to-defaults writes into a directory nobody will ever
    read again — never the operator's real `~/.no_human/config.yaml`. This
    is the fix for the bug this module exists to close: a walk's dev server
    used to proxy `/api` straight at the operator's live `:8420` board.

    Always yields exactly one `HermeticBackend`, never raises; every seam is
    injectable so tests never spawn a real subprocess, hit the network, or
    touch a real filesystem beyond `tmp_path`. `home_root` lets tests confine
    the throwaway `mkdtemp` under `tmp_path`; `None` (production) uses the
    OS default temp directory, same as `default_out_dir`.

    `auth_mode` is seeded into the throwaway config's `llm.auth_mode` so the
    hermetic child's own `nh start` boots under the SAME billing path the
    parent run is on. Left at the default (`"subscription"`), a sanctioned
    `api_key` install's child would otherwise still default to subscription
    mode, inherit the parent's exported `ANTHROPIC_API_KEY` (put there for
    the parent's OWN run by `config._assert_api_key_mode`), hit
    `assert_subscription_mode`'s strict metered-key refusal, and exit 2 —
    silently skipping every walk on that install forever. Anything other
    than the literal `"api_key"` normalizes to `"subscription"`. Only the
    MODE is ever written; the throwaway config never seeds `llm.auth_profile`
    — leaving it unset lets the child resolve its own credential exactly the
    way any other `nh start` does (the parent's unsuffixed OAuth token in
    subscription mode, or the parent's exported key in api_key mode), which
    is what `auth_mode` alone is meant to select.

    On any failure — before or during boot — teardown still runs: kill the
    process if one was spawned and is still alive, and always remove the
    throwaway HOME. Nothing is left running or on disk outside `out_dir`.
    """
    home = None
    process = None
    fh = None
    port = 0
    normalized_auth_mode = "api_key" if auth_mode == "api_key" else "subscription"
    try:
        try:
            port = pick_port()
        except OSError as exc:
            yield HermeticBackend(mode="failed",
                                   detail=f"{type(exc).__name__}: {exc}",
                                   cause="port-unavailable")
            return

        try:
            home = Path(tempfile.mkdtemp(prefix="nh-walk-home-", dir=home_root))
            config.ensure_private_dir(home / ".no_human")
            config.atomic_write_0600(
                home / ".no_human" / "config.yaml",
                "server:\n"
                "  host: 127.0.0.1\n"
                f"  port: {port}\n"
                "llm:\n"
                f"  auth_mode: {normalized_auth_mode}\n",
            )
        except Exception as exc:
            yield HermeticBackend(mode="failed", home=str(home or ""),
                                   port=port,
                                   detail=f"{type(exc).__name__}: {exc}",
                                   cause="home-seed-failed")
            return

        api_target = f"http://127.0.0.1:{port}"
        out_dir = Path(out_dir)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            fh = open(out_dir / _HERMETIC_API_LOG, "wb")
            process = spawn(
                _hermetic_start_argv(port),
                # Isolation comes from HOME/USERPROFILE alone — `nh start`
                # resolves its own private dir from those, and
                # `config.NO_HUMAN_HOME` is computed once at import time from
                # the environment `nh` (the parent) started under, so setting
                # it here would name the PARENT's directory in the CHILD's
                # env and do nothing useful with it.
                env={**os.environ, "HOME": str(home), "USERPROFILE": str(home)},
                cwd=str(home), stdout=fh, stderr=subprocess.STDOUT,
                **proc.hidden_console_kwargs(new_group=True),
            )
        except OSError as exc:
            if fh is not None:
                with contextlib.suppress(Exception):
                    fh.close()
            yield HermeticBackend(mode="failed", home=str(home), port=port,
                                   api_target=api_target,
                                   detail=f"{type(exc).__name__}: {exc}",
                                   cause="spawn-failed")
            return

        probe_url = api_target + _HERMETIC_READY_PATH
        outcome = HermeticBackend(mode="failed", home=str(home), port=port,
                                   api_target=api_target)
        started = clock()
        try:
            while clock() - started < _HERMETIC_READY_TIMEOUT_S:
                rc = process.poll()
                if rc is not None:
                    outcome.cause = "exited-early"
                    with contextlib.suppress(Exception):
                        if fh is not None:
                            fh.flush()
                    tail = _log_tail(out_dir / _HERMETIC_API_LOG)
                    outcome.detail = (
                        f"exit code {rc}: {tail}" if tail else f"exit code {rc}"
                    )
                    break
                try:
                    ok = reachable(probe_url)
                except Exception:
                    ok = False
                if ok:
                    outcome.mode = "armed"
                    break
                await sleep(_READY_POLL_S)
            else:
                outcome.cause = "timeout"
                with contextlib.suppress(Exception):
                    if fh is not None:
                        fh.flush()
                tail = _log_tail(out_dir / _HERMETIC_API_LOG)
                sentence = (
                    f"no response at {probe_url} within "
                    f"{_HERMETIC_READY_TIMEOUT_S}s"
                )
                outcome.detail = f"{sentence}: {tail}" if tail else sentence

            try:
                yield outcome
            finally:
                # `poll() is None` guards this so a process that exited early
                # (or was already reaped) is never killed twice. `detail` is
                # already set (or left "" for a successful `armed` outcome)
                # BEFORE the yield above — never stamped here — so a reader
                # of `hb.detail` during the `yield` (the orchestrator's
                # `walk_skip::hermetic_backend_init_failed:` advisory) always
                # sees the same text this teardown would have computed.
                with contextlib.suppress(Exception):
                    if process.poll() is None:
                        kill(process)
        finally:
            if fh is not None:
                with contextlib.suppress(Exception):
                    fh.close()
    finally:
        if home is not None:
            with contextlib.suppress(Exception):
                shutil.rmtree(home, ignore_errors=True)


def _png_size(data: bytes, fallback: dict) -> tuple[int, int]:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            return struct.unpack(">II", data[16:24])
        except struct.error:
            pass
    return fallback["width"], fallback["height"]


async def _maybe_await(value):
    """Awaits `value` if it is awaitable — tolerates a sync fake in tests."""
    if inspect.isawaitable(value):
        return await value
    return value


async def _open(out_dir: Path, viewport: dict, launch):
    """Returns (page, closer). `closer` is an async no-arg callable.

    Closing the CONTEXT (not just the page) is what flushes the `.webm`
    recording to disk, so the closer always prefers `page.context.close()`.
    """
    if launch is not None:
        page = await launch(out_dir, viewport)

        async def _injected_closer() -> None:
            context = getattr(page, "context", None)
            if context is not None:
                with contextlib.suppress(Exception):
                    await _maybe_await(context.close())
                    return
            with contextlib.suppress(Exception):
                await _maybe_await(page.close())

        return page, _injected_closer

    async_playwright = _import_playwright()
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True)
    context = await browser.new_context(
        viewport=viewport,
        record_video_dir=str(out_dir),
        record_video_size=viewport,
        ignore_https_errors=True,
    )
    page = await context.new_page()

    async def _real_closer() -> None:
        with contextlib.suppress(Exception):
            await context.close()
        with contextlib.suppress(Exception):
            await browser.close()
        with contextlib.suppress(Exception):
            await pw.stop()

    return page, _real_closer


async def _write_shot(page, out_dir: Path, name: str, step_index: int, shots: list) -> None:
    png = await _maybe_await(page.screenshot())
    (out_dir / f"{name}.png").write_bytes(png)
    width, height = _png_size(png, _DEFAULT_VIEWPORT)
    shots.append({
        "name": name,
        "path": f"{name}.png",
        "step_index": step_index,
        "width": width,
        "height": height,
        "sha256": hashlib.sha256(png).hexdigest(),
    })


async def _dispatch(page, step: Step, base_url: str, out_dir: Path, shots: list) -> None:
    action, value = step.action, step.value
    if action == "goto":
        await _maybe_await(page.goto(urljoin(base_url, value)))
    elif action == "wait_for":
        await _maybe_await(page.wait_for_selector(value))
    elif action == "click":
        await _maybe_await(page.click(value))
    elif action == "fill":
        await _maybe_await(page.fill(value, step.text))
    elif action == "select":
        # `<select>` elements reject `fill()` (Playwright: fill only works on
        # <input>/<textarea>/[contenteditable]) — `select_option` is the
        # dedicated primitive for choosing an option by its `value` attr.
        await _maybe_await(page.select_option(value, step.text))
    elif action == "press":
        # Global key press — Playwright's `Page.press(selector, key)` takes a
        # selector as its FIRST positional argument, so calling it with only
        # the key name would target a nonexistent selector. `Keyboard.press`
        # is the actual global key-press primitive and takes just the key.
        await _maybe_await(page.keyboard.press(value))
    elif action == "assert_text":
        locator = page.locator(f"text={value}")
        count = await _maybe_await(locator.count())
        if not count:
            raise RuntimeError(f"text not found: {value}")
    elif action == "shot":
        await _write_shot(page, out_dir, value, step.index, shots)
    else:  # pragma: no cover - validation forbids this
        raise RuntimeError(f"unknown action: {action}")


def _finalize_video(out_dir: Path) -> str | None:
    target = out_dir / "walk.webm"
    if target.exists():
        return "walk.webm"
    webms = sorted(out_dir.glob("*.webm"))
    if not webms:
        return None
    with contextlib.suppress(OSError):
        os.replace(webms[0], target)
    return "walk.webm" if target.exists() else None


def _write_artifacts(out_dir: Path, result: UiEvidenceResult) -> None:
    with contextlib.suppress(OSError, TypeError):
        (out_dir / "manifest.json").write_text(json.dumps(result.manifest, indent=2))
    with contextlib.suppress(OSError, TypeError):
        (out_dir / "console.json").write_text(
            json.dumps({"errors": list(result.console_errors)}, indent=2)
        )
    with contextlib.suppress(OSError, TypeError):
        (out_dir / "result.json").write_text(json.dumps(result.as_dict(), indent=2))


async def _run_body(
    repo_path: Path, out_dir: Path, deadline_s: float, launch, start: float,
    result: UiEvidenceResult,
) -> None:
    p = repo_path / MANIFEST
    if not p.is_file():
        result.reason = "no manifest"
        return

    data, problem = _load(repo_path)
    manifest = None
    if problem is None:
        manifest, problem = _parse(data)
    if problem:
        result.reason = problem
        return

    result.manifest = manifest.raw
    result.steps_total = len(manifest.steps)

    if not _reachable(manifest.base_url):
        result.reason = f"app not reachable at {manifest.base_url}"
        return

    if launch is None and _import_playwright() is None:
        result.reason = MISSING_PLAYWRIGHT_REASON
        return

    try:
        page, closer = await _open(out_dir, manifest.viewport, launch)
    except Exception as exc:
        msg = str(exc).replace("\n", " ")[:300]
        result.reason = f"browser launch failed: {msg}"
        return

    console_errors: list[str] = []

    def _on_console(msg) -> None:
        with contextlib.suppress(Exception):
            if len(console_errors) < _MAX_CONSOLE and getattr(msg, "type", "error") == "error":
                console_errors.append(str(getattr(msg, "text", msg))[:_CONSOLE_CHARS])

    def _on_pageerror(exc) -> None:
        with contextlib.suppress(Exception):
            if len(console_errors) < _MAX_CONSOLE:
                console_errors.append(str(exc)[:_CONSOLE_CHARS])

    with contextlib.suppress(Exception):
        page.on("console", _on_console)
    with contextlib.suppress(Exception):
        page.on("pageerror", _on_pageerror)

    shots: list[dict] = []
    steps_run = 0
    verdict, reason = "ran", ""
    failed_step: int | None = None
    failed_action: str | None = None

    for i, step in enumerate(manifest.steps):
        remaining_s = deadline_s - (time.monotonic() - start)
        if remaining_s <= 0:
            verdict = "failed"
            reason = f"step {i} ({step.action}): deadline of {deadline_s:g}s exceeded"
            failed_step, failed_action = i, step.action
            break
        timeout_s = min(step.timeout_ms / 1000, remaining_s)
        deadline_capped = timeout_s < (step.timeout_ms / 1000) - 1e-9
        try:
            await asyncio.wait_for(
                _dispatch(page, step, manifest.base_url, out_dir, shots),
                timeout=timeout_s,
            )
        except TimeoutError:
            verdict = "failed"
            if deadline_capped:
                reason = f"step {i} ({step.action}): deadline of {deadline_s:g}s exceeded"
            else:
                reason = f"step {i} ({step.action}): timed out after {step.timeout_ms}ms"
            failed_step, failed_action = i, step.action
            break
        except Exception as exc:
            verdict = "failed"
            reason = f"step {i} ({step.action}): {exc}"
            failed_step, failed_action = i, step.action
            break
        steps_run += 1

    with contextlib.suppress(Exception):
        await asyncio.wait_for(
            _write_shot(page, out_dir, "final", steps_run, shots),
            timeout=_FINAL_SHOT_TIMEOUT_S,
        )

    with contextlib.suppress(Exception):
        await closer()

    result.verdict = verdict
    result.reason = reason
    result.shots = shots
    result.video = _finalize_video(out_dir)
    result.console_errors = console_errors
    result.steps_run = steps_run
    result.failed_step = failed_step
    result.failed_action = failed_action


async def run(
    repo_path: Path, out_dir: Path, *, deadline_s: float = 120.0, launch=None,
) -> UiEvidenceResult:
    """Execute the coder-authored manifest, capturing evidence into `out_dir`.

    `launch` injects a page-producing async callable, `(out_dir, viewport)
    -> page`, in place of a real headless-chromium launch — the module's
    test seam. Never raises; every failure path returns a `UiEvidenceResult`
    with `verdict` in {"ran", "not_run", "failed"}.
    """
    start = time.monotonic()
    result = UiEvidenceResult(verdict="not_run")
    out_dir = Path(out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.reason = exc.__class__.__name__
        result.duration_s = time.monotonic() - start
        return result

    try:
        await _run_body(repo_path, out_dir, deadline_s, launch, start, result)
    except Exception as exc:
        result.verdict = "not_run"
        result.reason = exc.__class__.__name__

    result.duration_s = time.monotonic() - start
    with contextlib.suppress(Exception):
        _write_artifacts(out_dir, result)
    return result


def summary_line(result: UiEvidenceResult) -> str:
    if result.verdict == "not_run":
        return f"NOT RUN — {result.reason}"

    n_shots = len(result.shots)
    shot_word = "screenshot" if n_shots == 1 else "screenshots"

    if result.verdict == "failed":
        return f"failed at step {result.failed_step} ({result.failed_action}) — {n_shots} {shot_word}"

    video_part = "video" if result.video else "no video"
    n_err = len(result.console_errors)
    err_word = "console error" if n_err == 1 else "console errors"
    return f"ran — {n_shots} {shot_word}, {video_part}, {n_err} {err_word}"
