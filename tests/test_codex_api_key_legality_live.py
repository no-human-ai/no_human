"""AC1: the end-to-end proof that ``"api_key"`` mode cannot silently bill a
live ChatGPT session on a bogus ``OPENAI_API_KEY`` — the measured defect this
whole ticket exists to close.

Two lanes:

  1. HERMETIC (the default pytest lane, always runs): a fake ``codex`` CLI
     stubbed via ``subprocess.run``, proving the WIRING — that
     ``assert_api_key_billing_path`` actually sits between the credential
     and the child env, and that a CLI reporting a ChatGPT-backed session
     refuses the run before any ``codex exec`` argv is ever built.

  2. LIVE (opt-in, ``NO_HUMAN_CODEX_LIVE=1``, ``@pytest.mark.slow``): the
     real regression, executed against the real installed CLI and a real
     ChatGPT session, mirroring M1-M6 in ``.no_human/PLAN.md``. Skipped by
     default because it needs a real machine state (installed CLI, live
     ChatGPT session) this suite cannot manufacture, and because it is the
     one test in this repo that talks to a real vendor process.

No credential is read anywhere in this file: the hermetic lane never spawns
a real process, and the live lane uses a key that is deliberately invalid
(``sk-bogus-000...``) precisely so nothing it does could ever be billed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from no_human.agent import codex_backend as cx
from no_human.testing.pytest_isolated_home import REAL_HOME

FAKE_ENV = {"OPENAI_API_KEY": "not-a-real-key", "PATH": "/usr/bin:/bin"}


def _fake_cli_run(login_status_stdout: str, calls: list):
    """A `subprocess.run` stand-in that answers `codex login status` with
    `login_status_stdout` and records every argv it was called with, so a
    test can assert `codex exec` was never one of them — the (b) half of
    AC1's acceptance criterion ("no charges appear")."""
    def _run(argv, **kwargs):
        calls.append(tuple(argv))
        class _CP:
            pass
        cp = _CP()
        if len(argv) >= 3 and argv[1:3] == ["login", "status"]:
            cp.returncode = 0
            cp.stdout = login_status_stdout
            cp.stderr = ""
        else:
            # Any other invocation (e.g. `codex exec`) is itself evidence of
            # the defect in test 1 below — answer it, but the assertion that
            # matters is that `calls` never contains one.
            cp.returncode = 0
            cp.stdout = ""
            cp.stderr = ""
        return cp
    return _run


def test_api_key_mode_refuses_when_the_cli_would_bill_the_chatgpt_session(monkeypatch):
    """(a) fails: `_child_env()` raises before any `codex exec` launch.
    (b) no charge: the exec subprocess spawn count is 0.

    Mirrors M1/M2: a `codex login status` that reports a live ChatGPT
    session for no_human's own `CODEX_HOME` (i.e. the api-key `auth.json`
    this module just wrote was NOT what the CLI is honouring) is exactly
    the shape that, unguarded, silently billed the ChatGPT plan."""
    cx.reset_probe_caches()
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    calls: list = []
    monkeypatch.setattr(
        cx.subprocess, "run",
        _fake_cli_run("Logged in using ChatGPT", calls),
    )

    backend = cx.CodexBackend(env={**FAKE_ENV, "OPENAI_API_KEY": "sk-bogus-000"})
    with pytest.raises(cx.CodexAuthError) as exc:
        backend._child_env()

    msg = str(exc.value)
    assert "codex_auth_mode" in msg
    assert "ChatGPT" in msg

    exec_calls = [c for c in calls if len(c) >= 2 and c[1] == "exec"]
    assert exec_calls == [], (
        f"a `codex exec` argv was built despite the refusal: {exec_calls}")


def test_api_key_mode_starts_only_when_the_cli_reports_an_api_key_session(monkeypatch):
    """The accept path: a `codex login status` that reports an api_key-backed
    session for no_human's own `CODEX_HOME` lets `_child_env()` proceed, and
    the credential it wrote is shaped and permissioned exactly as M5 needs —
    `auth_mode: "apikey"`, mode 0o600, in no_human's own home, never the
    operator's `~/.codex`."""
    cx.reset_probe_caches()
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    calls: list = []
    monkeypatch.setattr(
        cx.subprocess, "run",
        _fake_cli_run("Logged in using an API key - ***", calls),
    )

    backend = cx.CodexBackend(env={**FAKE_ENV, "OPENAI_API_KEY": "sk-bogus-000"})
    env = backend._child_env()

    home = cx.codex_api_key_home()
    assert env["CODEX_HOME"] == str(home)

    cred_path = home / ("auth" + ".json")
    assert cred_path.is_file()
    mode = cred_path.stat().st_mode & 0o777
    assert mode == 0o600, f"credential file mode is {oct(mode)}, want 0o600"

    payload = json.loads(cred_path.read_text(encoding="utf-8"))
    assert payload["auth_mode"] == "apikey"
    assert payload["OPENAI_API_KEY"] == "sk-bogus-000"


def _live_chatgpt_session_present() -> bool:
    """Best-effort, read-only check via the sanctioned `login status` probe
    only — never a direct read of the credential file. Any failure to
    determine this (CLI missing, probe error) means "not confirmed", which
    the skip condition below treats as "skip", not "assume present".

    Passes `env_overrides={"HOME": REAL_HOME}`: this whole suite runs under
    `no_human.testing.pytest_isolated_home`, which redirects `HOME` to a
    throwaway temp dir for the ENTIRE process before any test body runs (see
    `tests/conftest.py`), so the operator's real `~/.codex` is invisible by
    default here — a plain `codex_login_status()` call would always answer
    "not present" on this machine regardless of the real session state, which
    would make this precondition permanently unsatisfiable rather than
    correctly gated. `REAL_HOME` is the constant that module captures before
    the redirect for exactly this purpose ("tests that need the truth ...
    read this constant instead" — its own docstring). We still never read the
    credential file ourselves: only the real `codex` binary, spawned with its
    OWN `HOME` pointed at the real one, does that."""
    if shutil.which("codex") is None:
        return False
    try:
        status = cx.codex_login_status(env_overrides={"HOME": str(REAL_HOME)})
    except Exception:
        return False
    return status.present and status.via == "chatgpt"


_LIVE_OPT_IN = os.environ.get("NO_HUMAN_CODEX_LIVE") == "1"


@pytest.mark.slow
@pytest.mark.skipif(
    not _LIVE_OPT_IN, reason="set NO_HUMAN_CODEX_LIVE=1 to run the live repro")
@pytest.mark.skipif(
    shutil.which("codex") is None, reason="codex CLI is not installed on PATH")
@pytest.mark.skipif(
    _LIVE_OPT_IN and not _live_chatgpt_session_present(),
    reason="no live ChatGPT session found via `codex login status`",
)
def test_live_bogus_key_never_bills_the_chatgpt_session():
    """The executable M1->M6 regression: with a bogus `OPENAI_API_KEY` and a
    real ChatGPT session present on this machine, the fix must make the real
    `codex` CLI fail on the bad key (401 / invalid_api_key) rather than
    silently completing the turn on the ChatGPT plan (the pre-fix defect,
    M1/M2). Talks to a real vendor process — that is the point: everything
    else in this file only proves the wiring against a fake one."""
    cx.reset_probe_caches()
    backend = cx.CodexBackend(env={**os.environ, "OPENAI_API_KEY": "sk-bogus-000-live"})
    try:
        env = backend._child_env()
    except cx.CodexAuthError:
        # AC1 only requires that the ChatGPT plan is never billed — refusing
        # before a `codex exec` is ever launched is a STRICTER, equally
        # valid way to satisfy that, per M5/M6 (a hand-shaped `auth.json`
        # with a bogus key is still recognised as api_key-backed by
        # `login status`, so this branch is not expected to fire in
        # practice, but a fix that made the gate MORE conservative than
        # M5/M6 measured must not fail this test for being too safe).
        return

    cli = cx.find_codex_cli()
    assert cli is not None
    proc = subprocess.run(
        [cli, "exec", "--json", "--sandbox", "read-only",
         "--config", 'preferred_auth_method="apikey"', "-"],
        input="say hello\n", capture_output=True, text=True, timeout=60, env=env,
    )
    transcript = f"{proc.stdout}\n{proc.stderr}"
    low = transcript.lower()

    chatgpt_markers = (
        "chatgpt.com", "usage limit", "when using codex with a chatgpt account",
    )
    hit = [m for m in chatgpt_markers if m in low]
    assert not hit, (
        f"the ChatGPT plan was billed on a bogus API key — markers found: "
        f"{hit}\ntranscript tail: {transcript[-2000:]}"
    )
    auth_failure_markers = ("invalid_api_key", "401", "incorrect api key")
    assert any(m in low for m in auth_failure_markers), (
        "expected an auth-failure marker for the bogus key, found none — "
        f"transcript tail: {transcript[-2000:]}"
    )
