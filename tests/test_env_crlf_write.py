"""Regression tests: the CLI must never WRITE a CRLF-terminated .env or
config.yaml, even when the write happens on Windows.

Background: on a Windows install, ``.env``/``config.yaml`` lines came back
CRLF-terminated even though every writer's *content* argument only ever
contains ``"\\n"``. The cause was Python's universal-newline text mode: with
``newline=None`` (the default), any ``"\\n"`` written is translated to
``os.linesep``, which is ``"\\r\\n"`` on Windows. A CRLF ``.env`` then made the
desktop app's JS line-splitting readers (``tokenStore.mjs``'s ``parseEnv`` and
friends) silently DROP the credential line outright — see
``desktop/tokenStore.test.mjs`` and ``desktop/crlfParsers.test.mjs`` for that
half of the fix.

This file pins the WRITER half: :func:`atomic_write_0600` and
:func:`_atomic_write_text` (and everything that funnels through them —
:func:`upsert_env_var`, :func:`set_profile_token`) must pass ``newline="\\n"``
so the translation never fires, regardless of host OS.

Fixing only the JS reader would leave every Windows install writing CRLF
forever — a fresh ``.env``/``config.yaml`` would still come out CRLF on the
NEXT save, and any other consumer that (correctly, per the file's own
historical convention) does not special-case CRLF would break. Fixing only the
Python writer would leave every install that already HAS a CRLF file (from a
build before this fix, or from a user's own editor) broken until they
happened to trigger a rewrite. Both sides are required.

Two of the tests below (`..._disables_newline_translation`,
`..._config_yaml_is_lf_only`) monkeypatch the underlying `os.fdopen` /
`Path.write_text` call to assert the `newline="\\n"` KWARG was actually passed,
rather than only checking the resulting bytes: this host's `os.linesep` is
already `"\\n"`, so the on-disk bytes would be identical with or without the
fix here — the regression is Windows-only and otherwise unobservable in CI.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from no_human import config as cfg  # noqa: E402

# Reuse the existing Windows-branch fixture/fake rather than re-inventing it —
# pytest recognises a fixture function by name in the importing module's
# namespace, whether it was defined there or imported.
from tests.test_windows_credential_file import (  # noqa: E402,F401
    _FakeIcacls,
    _ICACLS_OWNER_ONLY,
    as_windows,
)


def test_set_profile_token_writes_lf_only_bytes(tmp_path):
    """End-to-end: `set_profile_token`'s .env bytes carry no `\\r` anywhere."""
    env_path = tmp_path / ".env"
    key = cfg.set_profile_token("default", "sk-ant-oat01-abc123", env_path=env_path)
    raw = env_path.read_bytes()
    assert b"\r" not in raw, f"CRLF byte found in .env: {raw!r}"
    assert raw.endswith(b"\n")
    assert (env_path.stat().st_mode & 0o777) == 0o600
    assert cfg._read_env_file(env_path)[key] == "sk-ant-oat01-abc123"


def test_set_profile_token_writes_lf_bytes_on_windows(tmp_path, monkeypatch, as_windows):
    """Taking the Windows ACL branch must not change the newline behaviour.

    `_IS_WINDOWS` only gates the icacls dance in `atomic_write_0600`; the
    `os.fdopen(..., newline="\\n")` call beneath it is unconditional. This
    proves the two do not interact badly (e.g. some Windows-only code path
    re-opening the file in default text mode after the ACL step).
    """
    monkeypatch.setattr(cfg, "_run_icacls", _FakeIcacls(_ICACLS_OWNER_ONLY))
    env_path = tmp_path / ".env"
    key = cfg.set_profile_token("default", "sk-ant-oat01-winbranch", env_path=env_path)
    raw = env_path.read_bytes()
    assert b"\r" not in raw, f"CRLF byte found in .env written via the Windows branch: {raw!r}"
    assert cfg._read_env_file(env_path)[key] == "sk-ant-oat01-winbranch"


def test_atomic_write_0600_disables_newline_translation(tmp_path, monkeypatch):
    """`atomic_write_0600` must call `os.fdopen(..., newline="\\n")`.

    Without this argument, Windows' text-mode translation turns every `"\\n"`
    written into `os.linesep` (`"\\r\\n"` there) — this host's `os.linesep` is
    already `"\\n"`, so the resulting BYTES would be identical whether or not
    the fix is present. The kwarg itself is what must be pinned.
    """
    calls = []
    real_fdopen = os.fdopen

    def _spy(fd, mode="r", *args, **kwargs):
        calls.append(kwargs.get("newline", "<absent>"))
        return real_fdopen(fd, mode, *args, **kwargs)

    monkeypatch.setattr(cfg.os, "fdopen", _spy)
    cfg.atomic_write_0600(tmp_path / ".env", "CLAUDE_CODE_OAUTH_TOKEN=sk-tok\n")

    assert calls, "atomic_write_0600 must open its temp file via os.fdopen"
    assert calls[0] == "\n", (
        f"os.fdopen must be called with newline='\\n', got {calls[0]!r} — "
        "without it, a CRLF .env silently drops credential lines for the "
        "desktop app's line-splitting readers")


def test_atomic_write_text_config_yaml_is_lf_only(tmp_path, monkeypatch):
    """`_atomic_write_text` must call `Path.write_text(..., newline="\\n")`.

    Same rationale as the `os.fdopen` test above: pin the kwarg directly,
    since the byte-level effect is unobservable on a POSIX CI host.
    """
    calls = []
    real_write_text = Path.write_text

    def _spy(self, data, *args, **kwargs):
        calls.append(kwargs.get("newline", "<absent>"))
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _spy)
    target = tmp_path / "config.yaml"
    cfg._atomic_write_text(target, "llm:\n  auth_mode: subscription\n")

    assert calls, "_atomic_write_text must write via Path.write_text"
    assert calls[0] == "\n", (
        f"Path.write_text must be called with newline='\\n', got {calls[0]!r} "
        "— without it, a saved config.yaml regresses to CRLF on Windows")
    assert target.read_bytes() == b"llm:\n  auth_mode: subscription\n"


def test_upsert_env_var_preserves_other_lines_without_crlf(tmp_path):
    """Rewriting one key of an existing CRLF `.env` must not re-emit CRLF.

    `upsert_env_var` reads via `Path.read_text` (universal newlines: `\\r\\n`
    is already normalised to `\\n` on read — this is why `_read_env_file` and
    this read path are correctly out of scope for a reader fix), but writes
    via `atomic_write_0600`. Seeding a CRLF file and checking the OUTPUT bytes
    isolates the writer half of the fix from the (already-correct) reader half.
    """
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"FOO=bar\r\nBAR=old\r\n")
    cfg.upsert_env_var(env_path, "BAR", "new")

    raw = env_path.read_bytes()
    assert b"\r" not in raw, f"CRLF byte found after upsert_env_var: {raw!r}"
    entries = cfg._read_env_file(env_path)
    assert entries["FOO"] == "bar", "the untouched line must survive the rewrite"
    assert entries["BAR"] == "new"


def test_read_env_file_still_parses_a_crlf_file(tmp_path):
    """Regression control: the Python READER was never the bug.

    `Path.read_text`'s universal-newline mode already normalises `\\r\\n` to
    `\\n` before `_read_env_file` ever sees the text, so a CRLF `.env` —
    however it got that way (a pre-fix build, a user's own editor) — must keep
    parsing correctly. This is the reason `_read_env_file` is explicitly out
    of scope for this change: it does not need one.
    """
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-crlf\r\n")
    entries = cfg._read_env_file(env_path)
    assert entries["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-crlf"
