"""The coding-backend seam, and the second backend behind it.

TWO THINGS ARE UNDER TEST HERE and they are not the same thing.

  1. THE SEAM. That ``agent/backend.py`` describes what the orchestrator
     actually needs, that the default resolves to the incumbent Claude path
     with identical arguments, and that the four Claude-pinned roles cannot be
     moved by config. This half is the acceptance criterion of the change:
     an operator who edits nothing must get exactly what they had.

  2. THE CODEX BACKEND's event translation, guard evaluation, token
     arithmetic and refusal behaviour, driven over a FAKE ``codex`` process.

WHAT THESE TESTS DO NOT ESTABLISH, stated here rather than left to be
discovered: no test in this file reaches OpenAI, and none runs the real
``codex`` binary. The JSONL fixtures below are written against the documented
``codex exec --json`` envelope; if the real CLI's schema differs, these tests
pass and the backend still fails in production. What they DO pin is everything
downstream of the parse — the guard wiring, the cached-token subtraction, the
NULL-vs-zero output rule, and the refusal paths — which is where the logic
lives. See ``_ITEM_TOOL_NAMES`` for the one table a schema change would move.

No credential is read or written anywhere in this file: the fake env carries
the literal string "not-a-real-key".
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import stat
import shutil
import subprocess
from pathlib import Path

import pytest

from no_human.agent import backend as seam
from no_human.agent import codex_backend as cx
from no_human.agent.backend import (
    AgentEvent,
    AgentResult,
    BackendCapabilities,
    BackendUnavailable,
    CodingBackend,
    make_backend,
    resolve_backend_name,
)
from no_human.agent.claude_backend import ClaudeBackend

FAKE_ENV = {"OPENAI_API_KEY": "not-a-real-key", "PATH": "/usr/bin:/bin"}

#: `codex exec --help` text for the ACTUALLY installed CLI, verified live
#: (see docs/BACKENDS.md): codex-cli 0.149.0 does NOT expose
#: `--ask-for-approval` on `exec` (only the root `codex` command has it), but
#: does expose `-c, --config <key=value>`. This is the default `_stub_cli`
#: shape so the whole suite exercises the modern/real code path unless a test
#: explicitly asks for the legacy one.
_MODERN_HELP_TEXT = (
    "codex-exec\n\nUSAGE:\n    codex exec [OPTIONS] [PROMPT]\n\nOPTIONS:\n"
    "    --json                       Print events as JSONL\n"
    "    --cd <DIR>                   Set the working directory\n"
    "    --model <MODEL>              Model to use\n"
    "    --sandbox <MODE>             read-only | workspace-write | danger-full-access\n"
    "    -c, --config <key=value>     Override a config value\n"
    "    --dangerously-bypass-approvals-and-sandbox\n"
    "                                 Skip both approvals and the sandbox\n"
)

#: `codex exec --help` text for an OLDER CLI that still has the flag this
#: backend used to hardcode — used to exercise `approval_args`'s first
#: (preferred) branch.
_LEGACY_HELP_TEXT = (
    "codex-exec\n\nOPTIONS:\n"
    "    --json\n    --cd <DIR>\n    --model <MODEL>\n    --sandbox <MODE>\n"
    "    --ask-for-approval <POLICY>  never | on-failure | untrusted\n"
    "    -c, --config <key=value>\n"
)


#: `codex exec resume --help` text for the ACTUALLY installed CLI, verified
#: live: neither `--cd` nor `--sandbox` is documented on the resume
#: subcommand — a resumed thread inherits both from the session it is
#: resuming. Deliberately narrower than `_MODERN_HELP_TEXT` so a test that
#: checks resume argv against this fixture would fail if `_command` ever
#: emitted either flag on the resume branch.
_MODERN_RESUME_HELP_TEXT = (
    "codex-exec-resume\n\nUSAGE:\n    codex exec resume [OPTIONS] [THREAD_ID]\n\n"
    "OPTIONS:\n"
    "    --json                       Print events as JSONL\n"
    "    --model <MODEL>              Model to use\n"
    "    -c, --config <key=value>     Override a config value\n"
    "    --dangerously-bypass-approvals-and-sandbox\n"
    "                                 Skip both approvals and the sandbox\n"
)


def _stub_cli(monkeypatch, *, help_text=_MODERN_HELP_TEXT,
              resume_help_text=_MODERN_RESUME_HELP_TEXT,
              version="codex-cli 0.149.0", cli="/bin/codex",
              login_status=None):
    """Stub the read-only CLI probes `_command` (and, via
    `assert_api_key_billing_path`, `run`) make, so no test in this file
    spawns a real subprocess for them. Defaults mirror the installed CLI
    this backend was fixed against, live, in this repo.

    `codex_exec_help` is keyed by `resume` because the real CLI documents a
    DIFFERENT, narrower flag surface for `codex exec resume --help` than for
    `codex exec --help` — a stub that ignored the kwarg would let a resume-only
    flag regression pass by validating against the wrong help text.

    `login_status` defaults to a present, api_key-backed session — the
    verdict `_child_env_api_key`'s billing gate needs to let a fake run
    proceed to the subprocess-spawn point at all. Tests that exercise the
    gate itself, or subscription mode, pass an explicit `CodexSessionStatus`
    (or monkeypatch `codex_login_status` themselves without going through
    this helper, as every subscription-mode test in this file already does)."""
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: cli)
    monkeypatch.setattr(
        cx, "codex_exec_help",
        lambda path, resume=False, timeout=10.0: (
            resume_help_text if resume else help_text
        ),
    )
    monkeypatch.setattr(cx, "codex_version",
                        lambda path, timeout=10.0: version)
    status = (login_status if login_status is not None
              else cx.CodexSessionStatus(True, "api_key", "stub"))
    monkeypatch.setattr(
        cx, "codex_login_status",
        lambda cli_path=None, timeout_s=10.0, *, env_overrides=None: status,
    )


# --------------------------------------------------------------------------- #
# 1. The seam                                                                  #
# --------------------------------------------------------------------------- #

@pytest.mark.real_backend
def test_the_default_backend_is_still_the_claude_agent_sdk_path():
    """THE acceptance criterion. No config, empty config, and an explicit
    "claude" all produce the incumbent class."""
    for cfg in (None, {}, {"worker": {}}, {"worker": {"backend": "claude"}}):
        be = make_backend(model="claude-sonnet-5", config=cfg)
        assert isinstance(be, ClaudeBackend), cfg
        assert be.model == "claude-sonnet-5"


@pytest.mark.real_backend
def test_the_default_backend_receives_the_same_arguments_it_always_did():
    """Not just the same CLASS — the same construction. A factory that silently
    dropped `never_push_to` would return a ClaudeBackend that pushes to main."""
    be = make_backend(
        model="claude-sonnet-5",
        config={},
        forbidden_paths=[".env", "custom/"],
        never_push_to=["main", "trunk"],
        readonly=True,
        permission_mode="acceptEdits",
        tool_result_caps={"Bash": 11},
    )
    assert be.forbidden_paths == [".env", "custom/"]
    assert be.never_push_to == ["main", "trunk"]
    assert be.readonly is True
    assert be.permission_mode == "acceptEdits"
    assert be.tool_result_caps == {"Bash": 11}


def test_only_the_coder_role_can_be_moved_off_claude():
    """The review gate and all four model tiers are pinned by constraint. The
    2026-08-01 amendment sanctioned a second CODING backend and moved neither,
    so a `worker.backend` that silently relocated the reviewer would be a
    constraint change wearing the costume of a feature."""
    cfg = {"worker": {"backend": "codex"}}
    assert resolve_backend_name(cfg, role="coder") == "codex"
    for role in seam.CLAUDE_PINNED_ROLES:
        assert resolve_backend_name(cfg, role=role) == "claude", role
    # An unrecognised role defaults to the SAFE side, not to the config value —
    # this is what makes the tuple above documentation rather than the gate.
    assert resolve_backend_name(cfg, role="some-future-role") == "claude"


@pytest.mark.real_backend
def test_a_pinned_role_gets_a_claude_backend_even_under_codex_config():
    be = make_backend(model="claude-opus-5", role="reviewer",
                      config={"worker": {"backend": "codex"}}, readonly=True)
    assert isinstance(be, ClaudeBackend)
    assert be.model == "claude-opus-5"


def test_an_unknown_backend_name_is_refused_by_name():
    with pytest.raises(BackendUnavailable) as exc:
        make_backend(model="m", config={"worker": {"backend": "gemini"}})
    assert "gemini" in str(exc.value)
    assert "claude" in str(exc.value)


@pytest.mark.real_backend
def test_both_backends_satisfy_the_protocol_the_orchestrator_is_typed_against():
    assert isinstance(ClaudeBackend(model="claude-sonnet-5"), CodingBackend)
    assert isinstance(cx.CodexBackend(), CodingBackend)


@pytest.mark.real_backend
def test_both_backends_declare_capabilities_and_they_differ_where_they_must():
    claude = ClaudeBackend(model="claude-sonnet-5").capabilities
    codex = cx.CodexBackend().capabilities
    assert isinstance(claude, BackendCapabilities)
    assert isinstance(codex, BackendCapabilities)
    # The mismatches the report names. Asserted so a future "fix" that flips one
    # of these to True has to say why, in a diff, next to the evidence.
    assert claude.blocks_tool_calls and not codex.blocks_tool_calls
    assert claude.post_tool_hooks and not codex.post_tool_hooks
    assert claude.skills and not codex.skills
    assert claude.subagents and not codex.subagents
    assert claude.incremental_usage and not codex.incremental_usage
    assert claude.cache_creation_accounting and not codex.cache_creation_accounting


def test_the_seam_types_are_the_same_objects_the_old_import_path_yields():
    """~50 sites do `from ...claude_backend import AgentEvent`. Moving the
    dataclasses to the seam must not create a second class: an `isinstance`
    against the other copy would silently start returning False."""
    from no_human.agent import claude_backend as cb

    assert cb.AgentEvent is AgentEvent
    assert cb.AgentResult is AgentResult


def test_the_codex_model_is_its_own_config_key_not_a_claude_tier(monkeypatch):
    """The Claude tier IDs are fixed by constraint. Forwarding `claude-sonnet-5` to
    OpenAI would be both meaningless and a silent tier change."""
    be = make_backend(model="claude-sonnet-5",
                      config={"worker": {"backend": "codex"},
                              "llm": {"codex_model": "gpt-5-codex-mini"}})
    assert isinstance(be, cx.CodexBackend)
    assert be.model == "gpt-5-codex-mini"
    # With no override it takes the module default, never the Claude tier.
    be2 = make_backend(model="claude-sonnet-5",
                       config={"worker": {"backend": "codex"}})
    assert be2.model == seam.DEFAULT_CODEX_MODEL
    assert not be2.model.startswith("claude")


def test_the_network_grant_is_wired_through_make_backend_and_defaults_on(
        monkeypatch):
    """`llm.codex_network_access` is the operator's opt-out (default True: an
    operator who changes nothing gets the network grant this ticket adds).
    Covers the full path config.py -> make_backend -> CodexBackend, not just
    the constructor default in isolation."""
    be_default = make_backend(model="claude-sonnet-5",
                              config={"worker": {"backend": "codex"}})
    assert be_default.network_access is True

    be_off = make_backend(model="claude-sonnet-5", config={
        "worker": {"backend": "codex"},
        "llm": {"codex_network_access": False},
    })
    assert be_off.network_access is False
    _stub_cli(monkeypatch)
    cmd = be_off._command(Path("/repo"), effort=None, resume=None)
    assert "network_access" not in " ".join(cmd)

    be_on = make_backend(model="claude-sonnet-5", config={
        "worker": {"backend": "codex"},
        "llm": {"codex_network_access": True},
    })
    assert be_on.network_access is True

    # The two shapes that used to resolve to the OPPOSITE of what was written.
    # Asserted through make_backend, NOT against the coercion helper directly:
    # reverting the call site to `bool(cfg.get(...))` left every helper-level
    # test green, so a helper test does not pin this wiring.
    be_quoted = make_backend(model="claude-sonnet-5", config={
        "worker": {"backend": "codex"},
        "llm": {"codex_network_access": "false"},   # quoted in YAML
    })
    assert be_quoted.network_access is False, (
        "a quoted `false` silently kept the sandbox's network on — "
        "bool('false') is True, so the operator's opt-out was ignored")

    be_null = make_backend(model="claude-sonnet-5", config={
        "worker": {"backend": "codex"},
        "llm": {"codex_network_access": None},      # `null` in YAML
    })
    assert be_null.network_access is True, (
        "`null` must mean THE DEFAULT, as it does for `codex_model` and "
        "`codex_cli_path`; bool(None) used to turn the grant off instead")


def test_the_default_config_grants_network_to_the_codex_coder():
    """The actual shipped default (`config.py`'s `DEFAULT_CONFIG`, not a test
    fixture) is `True` — an operator running an unmodified install gets the
    fix, not just a documented opt-in."""
    from no_human import config as cfgmod

    assert cfgmod.DEFAULT_CONFIG["llm"]["codex_network_access"] is True


# --------------------------------------------------------------------------- #
# 2. Codex: command construction and the legality gate                         #
# --------------------------------------------------------------------------- #

def test_the_command_forces_api_key_auth_and_never_offers_a_login(monkeypatch):
    """`llm.codex_auth_mode: "api_key"` is the DEFAULT (unchanged since before
    the 2026-08-22 amendment that added the sibling "subscription" mode).
    `preferred_auth_method="apikey"` is still emitted here (positive control
    for the source-text guards below, and belt-and-braces for CLI versions
    that honour it), so it stays pinned in this argv — but it is NOT what
    stops the CLI falling back to a ChatGPT login that happens to be on the
    machine: codex-cli 0.149.0 silently ignores it (confirmed live,
    2026-08-25 — see `codex_backend.py`'s module docstring and
    `assert_api_key_billing_path`). The real gate runs earlier, in
    `_child_env_api_key`, before this argv is ever built: it demands the CLI
    itself report an api_key-backed session against a no_human-owned
    `CODEX_HOME`. This test only pins argv shape, not the gate; the gate is
    covered by its own tests below.
    (Subscription mode's own command construction, which omits this flag
    entirely, is covered separately below.)

    Approval flag: stubbed with the MODERN help text (the actually installed
    codex-cli 0.149.0, verified live), which has no `--ask-for-approval` on
    `exec` — so the `--config approval_policy="never"` fallback is what must
    appear. See `test_approval_args_*` for the flag-selection ladder itself."""
    _stub_cli(monkeypatch)
    cmd = cx.CodexBackend()._command(Path("/repo"), effort="high", resume=None)
    assert 'preferred_auth_method="apikey"' in cmd
    joined = " ".join(cmd)
    assert "login" not in joined
    assert "chatgpt" not in joined.lower()
    # Never unsandboxed: without a PreToolUse veto the sandbox is the only real
    # boundary left.
    assert "--dangerously-bypass-approvals-and-sandbox" not in joined
    assert "--sandbox" in cmd and "workspace-write" in cmd
    assert "--ask-for-approval" not in cmd
    assert 'approval_policy="never"' in cmd
    assert 'model_reasoning_effort="high"' in cmd
    # The coder's sandbox is granted network by default (see
    # tests/test_codex_sandbox_network.py for the measured proof of why this
    # is needed and why it is safe) — never `danger-full-access`.
    assert "--config" in cmd and "sandbox_workspace_write.network_access=true" in cmd
    assert "danger-full-access" not in joined
    # AC1's own check, inline: every flag this test's cmd emits appears in the
    # help text it was resolved against — proves the stub isn't lying to itself.
    for flag in cx.emitted_flags(cmd):
        assert flag in _MODERN_HELP_TEXT, flag


def test_the_command_uses_ask_for_approval_when_the_installed_cli_still_has_it(
        monkeypatch):
    """The other branch of the ladder: an older CLI that still documents
    `--ask-for-approval` on `exec` gets that flag, not the --config fallback —
    proving the choice is read from the CLI, not hardcoded either way."""
    _stub_cli(monkeypatch, help_text=_LEGACY_HELP_TEXT)
    cmd = cx.CodexBackend()._command(Path("/repo"), effort=None, resume=None)
    assert "--ask-for-approval" in cmd and "never" in cmd
    assert 'approval_policy="never"' not in " ".join(cmd)


def test_a_readonly_codex_session_gets_the_sandbox_that_enforces_it(monkeypatch):
    _stub_cli(monkeypatch)
    cmd = cx.CodexBackend(readonly=True)._command(
        Path("/repo"), effort=None, resume=None)
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"


def test_a_readonly_codex_session_is_never_granted_network(monkeypatch):
    """`--sandbox read-only` has no `sandbox_workspace_write` table for
    `network_access` to attach to — emitting the key anyway would be inert at
    best and a false signal at worst. `network_access=True` is still the
    constructor default here (readonly wins unconditionally), so this proves
    the guard is `not self.readonly`, not merely "nobody passed the flag"."""
    _stub_cli(monkeypatch)
    cmd = cx.CodexBackend(readonly=True, network_access=True)._command(
        Path("/repo"), effort=None, resume=None)
    joined = " ".join(cmd)
    assert "network_access" not in joined


def test_resume_continues_a_thread(monkeypatch):
    _stub_cli(monkeypatch)
    cmd = cx.CodexBackend()._command(Path("/repo"), effort=None, resume="th_1")
    assert cmd[1:4] == ["exec", "resume", "th_1"]
    # THE repro for this ticket's Blocker 1: `codex exec resume --help`
    # (verified live) documents neither flag — a resumed thread inherits its
    # cwd and its sandbox from the session it is resuming. Before the fix,
    # `_command` emitted both unconditionally and every resumed attempt died
    # at launch with "unexpected argument", rc=2.
    assert "--cd" not in cmd
    assert "--sandbox" not in cmd
    # Everything else non-resume attempts also get is still present.
    assert "--json" in cmd
    assert "--model" in cmd
    # The network grant is NOT gated on `is_resume`: a resumed thread inherits
    # its sandbox MODE from the session it is resuming (that's why `--sandbox`
    # itself is absent above), but `-c`/`--config` overrides are layered
    # in-memory on every invocation, not stored once at session start — so
    # dropping this on resume would silently lose the grant from turn 2 on.
    assert "--config" in cmd and "sandbox_workspace_write.network_access=true" in cmd
    for flag in cx.emitted_flags(cmd):
        assert flag in _MODERN_RESUME_HELP_TEXT, flag


#: The two argv shapes that would hand a codex session an unsandboxed or
#: fully-open filesystem/network boundary. Neither may ever appear in a
#: built command, under ANY constructor/`_command` combination — this is
#: the safety floor the network-access fix (`sandbox_workspace_write.
#: network_access=true`, always paired with an explicit `--sandbox
#: workspace-write`) must never be confused with or degrade into. Named as
#: a module constant, not inlined per-test, so every configuration below is
#: swept against the SAME list — adding a new dangerous flag here protects
#: every case at once instead of requiring N separate edits.
_FORBIDDEN_SANDBOX_BYPASS_TOKENS = (
    "--dangerously-bypass-approvals-and-sandbox",
    "danger-full-access",
)


@pytest.mark.parametrize(
    "ctor_kwargs,call_kwargs",
    [
        pytest.param({}, {"effort": None, "resume": None}, id="default"),
        pytest.param({}, {"effort": "high", "resume": None}, id="with-effort"),
        pytest.param({}, {"effort": None, "resume": "th_1"}, id="resume"),
        pytest.param(
            {"readonly": True}, {"effort": None, "resume": None}, id="readonly"),
        pytest.param(
            {"network_access": False}, {"effort": None, "resume": None},
            id="network-off"),
        pytest.param(
            {"readonly": True, "network_access": True},
            {"effort": None, "resume": None}, id="readonly-network-requested"),
        pytest.param(
            {"auth_mode": "subscription"}, {"effort": None, "resume": None},
            id="subscription-auth"),
        pytest.param(
            {"auth_mode": "subscription", "readonly": True},
            {"effort": "high", "resume": "th_2"}, id="subscription-readonly-resume"),
    ],
)
def test_the_built_argv_never_bypasses_the_sandbox(
        monkeypatch, ctor_kwargs, call_kwargs):
    """Property sweep, not a spot check: whatever combination of readonly /
    network_access / auth_mode / resume `_command` is asked to build, the
    two flags that would remove the sandbox boundary entirely
    (`--dangerously-bypass-approvals-and-sandbox`, `danger-full-access`)
    must never appear in the resulting argv. `CodexBackend` never passes
    either — there is no code path that emits them — so this is a permanent
    regression guard against a future edit (e.g. "just make it faster/less
    naggy") reaching for one of them instead of the measured, narrower
    `sandbox_workspace_write.network_access=true` grant."""
    _stub_cli(monkeypatch)
    resume = call_kwargs.pop("resume")
    cmd = cx.CodexBackend(**ctor_kwargs)._command(
        Path("/repo"), resume=resume, **call_kwargs)
    joined = " ".join(cmd)
    for token in _FORBIDDEN_SANDBOX_BYPASS_TOKENS:
        assert token not in joined, (ctor_kwargs, call_kwargs, cmd)


#: Every `--config KEY=VALUE` key `_command` is ALLOWED to emit. This list is
#: hand-written from a deliberate decision about each key, NOT read back out of
#: `codex_backend.py` — a list derived from the code under test would admit
#: whatever that code grew next and guard nothing.
#:
#: WHY AN ALLOWLIST AND NOT A DENYLIST. The sibling sweep above forbids two
#: named tokens. An independent review defeated it in one line: this change is
#: the FIRST to emit `--config sandbox_workspace_write.*`, and nothing
#: constrained what else could ride that channel. Adding
#: `sandbox_workspace_write.writable_roots=["/"]` next to the network grant left
#: all 98 tests GREEN, and against the real CLI it granted write access to the
#: WHOLE FILESYSTEM. `emitted_flags()` cannot see it either: it validates flag
#: NAMES against `--help`, and `--config` is a perfectly valid name whatever
#: follows it. A denylist of two strings cannot cover a channel that accepts
#: arbitrary keys, so the guard is inverted here.
_ALLOWED_CONFIG_KEYS = frozenset({
    "approval_policy",
    "preferred_auth_method",
    "model_reasoning_effort",
    "sandbox_workspace_write.network_access",
})


@pytest.mark.parametrize(
    "ctor_kwargs,call_kwargs",
    [
        pytest.param({}, {"effort": None, "resume": None}, id="default"),
        pytest.param({}, {"effort": "high", "resume": None}, id="with-effort"),
        pytest.param({}, {"effort": None, "resume": "th_1"}, id="resume"),
        pytest.param(
            {"readonly": True}, {"effort": None, "resume": None}, id="readonly"),
        pytest.param(
            {"network_access": False}, {"effort": None, "resume": None},
            id="network-off"),
        pytest.param(
            {"readonly": True, "network_access": True},
            {"effort": None, "resume": None}, id="readonly-network-requested"),
        pytest.param(
            {"auth_mode": "subscription"}, {"effort": None, "resume": None},
            id="subscription-auth"),
        pytest.param(
            {"auth_mode": "subscription", "readonly": True},
            {"effort": "high", "resume": "th_2"}, id="subscription-readonly-resume"),
    ],
)
def test_command_emits_no_config_key_outside_the_allowlist(
        monkeypatch, ctor_kwargs, call_kwargs):
    """The `--config` channel this change opened may carry ONLY known keys.

    Sweeps the same constructor/call matrix as the bypass-token test. Any
    `--config` key `_command` learns to emit that is not in the allowlist fails
    here, which is what a denylist of dangerous spellings structurally cannot
    do. In particular `sandbox_workspace_write.writable_roots` — the mutation
    that granted `/`-wide write with the whole suite green — is caught by this
    and by nothing else.
    """
    _stub_cli(monkeypatch)
    resume = call_kwargs.pop("resume")
    cmd = cx.CodexBackend(**ctor_kwargs)._command(
        Path("/repo"), resume=resume, **call_kwargs)

    seen = [cmd[i + 1] for i, a in enumerate(cmd)
            if a == "--config" and i + 1 < len(cmd)]
    # Control: this sweep must actually observe the channel it guards, or a
    # future refactor that stops emitting --config would make it vacuously green.
    assert seen, (
        f"no --config pair in the built argv at all; this guard is inspecting "
        f"nothing: {cmd}")

    for pair in seen:
        key = pair.split("=", 1)[0]
        assert key in _ALLOWED_CONFIG_KEYS, (
            f"_command emitted an unapproved --config key {key!r}. If this is "
            f"deliberate, add it to _ALLOWED_CONFIG_KEYS with a reason - do not "
            f"widen the sandbox by way of a key nothing reviews. "
            f"argv={cmd}")


def test_a_missing_openai_key_refuses_rather_than_finding_other_auth():
    """api_key mode (the default) demands ITS OWN credential rather than
    silently trying the other mode. Reworded 2026-08-22: the message used to
    assert "no subscription path" existed at all; now it names the sibling
    "subscription" mode as the alternative, since that mode is sanctioned
    too — it just is not what api_key mode itself will fall back to."""
    with pytest.raises(cx.CodexAuthError) as exc:
        cx.CodexBackend(env={"PATH": "/bin"})._child_env()
    msg = str(exc.value)
    assert "OPENAI_API_KEY" in msg
    assert "llm.codex_auth_mode" in msg
    assert "subscription" in msg
    assert "config.yaml" in msg  # the key never lives there
    assert "prohibit" not in msg.lower()  # the unsourced claim is withdrawn
    assert "lawyer" in msg.lower()  # names the uncertainty honestly


def test_the_claude_credential_is_not_exported_into_the_codex_subprocess(monkeypatch):
    """It could not bill anything through `codex`, but any command the agent
    runs could read it. Not exporting it is free."""
    _stub_cli(monkeypatch)  # api_key billing gate must not shell out to the real CLI
    env = cx.CodexBackend(env={
        **FAKE_ENV,
        "CLAUDE_CODE_OAUTH_TOKEN": "not-a-real-token",
        "ANTHROPIC_API_KEY": "not-a-real-key-either",
    })._child_env()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["OPENAI_API_KEY"] == "not-a-real-key"


def test_a_missing_codex_cli_is_an_absence_with_a_name(monkeypatch):
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: None)
    with pytest.raises(BackendUnavailable) as exc:
        cx.CodexBackend()._command(Path("/repo"), effort=None, resume=None)
    assert "npm install -g @openai/codex" in str(exc.value)


def test_api_key_gate_timeout_refuses_the_run(monkeypatch, tmp_path):
    """`assert_api_key_billing_path` has no dedicated direct test elsewhere in
    this file — every other exercise of it goes through `_child_env_api_key`
    with `_stub_cli`'s passing default. Call it directly here, with a hung
    `codex login status` (TimeoutExpired), and confirm the gate fails CLOSED:
    a probe that cannot report a verdict must never be treated as api_key
    session evidence, which is exactly the defect this whole ticket is about
    — a silent unverified fallback billing the wrong account."""
    cx.reset_probe_caches()

    def _timeout(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd=["codex", "login", "status"], timeout=10.0)

    monkeypatch.setattr(cx.subprocess, "run", _timeout)
    home = tmp_path / "codex-home"
    home.mkdir()
    with pytest.raises(cx.CodexAuthError) as exc:
        cx.assert_api_key_billing_path("/bin/codex", home, timeout_s=1.0)
    assert str(home) in str(exc.value)


# --------------------------------------------------------------------------- #
# 2b. CLI compatibility ladder is probed, never assumed                        #
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(shutil.which("codex") is None,
                     reason="codex CLI is not installed on PATH")
def test_every_flag_we_emit_is_accepted_by_the_installed_codex_cli():
    """THE behavioural test the bug needed: not a fixture of what the CLI's
    help text says, but the REAL installed binary's help text. This is what
    would have caught `--ask-for-approval` being dropped from `exec` in
    codex-cli 0.149.0 before it ever reached a live attempt — the whole
    fixture-based suite around this test passed throughout that regression,
    because a fixture only pins what someone remembered to update.

    Checked for BOTH launch shapes: a fresh attempt (`resume=None`) and a
    resumed one (`resume=<thread-id>`) each build a different argv and
    `codex exec resume --help` documents a narrower flag surface than
    `codex exec --help` (no `--cd`, no `--sandbox`) — a CLI that accepts one
    argv can still reject the other, which is exactly this ticket's
    Blocker 1."""
    cx.reset_probe_caches()
    cli = cx.find_codex_cli()
    assert cli is not None  # shutil.which already confirmed presence
    for resume in (None, "th_1"):
        help_text = cx.codex_exec_help(cli, resume=bool(resume))
        assert help_text, (
            f"codex exec {'resume ' if resume else ''}--help produced no "
            "output to check against"
        )
        cmd = cx.CodexBackend(env=FAKE_ENV)._command(
            Path("/repo"), effort=None, resume=resume)
        flags = cx.emitted_flags(cmd)
        assert flags, "vacuous: _command emitted no flags to check"
        for flag in flags:
            assert flag in help_text, (
                f"{flag!r} is not accepted by the installed codex CLI "
                f"({cx.codex_version(cli)}) per "
                f"`codex exec {'resume ' if resume else ''}--help`")


def test_approval_args_prefers_ask_for_approval_when_the_cli_documents_it():
    assert cx.approval_args(_LEGACY_HELP_TEXT, "codex-cli 0.42.0") == \
        ["--ask-for-approval", "never"]


def test_approval_args_falls_back_to_config_when_ask_for_approval_is_gone():
    """The modern (installed, verified) shape: no `--ask-for-approval` on
    `exec`, but `-c/--config` survives, so the equivalent is expressed through
    it rather than the sandbox-dropping escape hatch."""
    result = cx.approval_args(_MODERN_HELP_TEXT, "codex-cli 0.149.0")
    assert result == ["--config", 'approval_policy="never"']


def test_approval_args_never_reaches_for_the_sandbox_dropping_flag():
    """`--dangerously-bypass-approvals-and-sandbox` also suppresses approval
    prompts, and it is ALWAYS present when `-c/--config` is — a ladder that
    preferred it over --config would silently trade away the sandbox, this
    backend's only real safety boundary. It must never be chosen."""
    help_text = _MODERN_HELP_TEXT
    assert "--dangerously-bypass-approvals-and-sandbox" in help_text  # sanity
    result = cx.approval_args(help_text, "codex-cli 0.149.0")
    assert "--dangerously-bypass-approvals-and-sandbox" not in result


def test_approval_args_refuses_rather_than_hang_on_an_approval_prompt():
    """Neither flag documented anywhere in --help: raise, naming the CLI
    version, rather than launch a session that can hang forever waiting for
    someone at a keyboard that nobody is sitting at."""
    bare_help = "codex-exec\n\nOPTIONS:\n    --json\n    --model <MODEL>\n"
    with pytest.raises(BackendUnavailable) as exc:
        cx.approval_args(bare_help, "codex-cli 0.99.0")
    assert "codex-cli 0.99.0" in str(exc.value)


def test_approval_args_refuses_on_a_missing_or_empty_help_text_too():
    with pytest.raises(BackendUnavailable) as exc:
        cx.approval_args(None, "unknown version")
    assert "unknown version" in str(exc.value)


def test_codex_exec_help_and_version_are_probed_once_per_cli_path(monkeypatch):
    """`_command` runs once per attempt, but a single process launches many
    attempts; re-spawning `codex --help`/`--version` on every attempt would be
    a needless subprocess per turn. Cache by resolved CLI path instead."""
    cx.reset_probe_caches()
    calls = []
    real_run = subprocess.run

    def _counting_run(argv, **kw):
        calls.append(tuple(argv))
        return real_run(["true"], **{**kw, "capture_output": True, "text": True})

    monkeypatch.setattr(cx.subprocess, "run", _counting_run)
    cx.codex_exec_help("/bin/codex")
    cx.codex_exec_help("/bin/codex")
    cx.codex_version("/bin/codex")
    cx.codex_version("/bin/codex")
    help_calls = [c for c in calls if c[1:] == ("exec", "--help")]
    version_calls = [c for c in calls if c[1:] == ("--version",)]
    assert len(help_calls) == 1, help_calls
    assert len(version_calls) == 1, version_calls


def test_codex_exec_help_returns_none_when_the_cli_cannot_be_spawned():
    cx.reset_probe_caches()
    assert cx.codex_exec_help("/no/such/codex/binary") is None


def test_codex_version_falls_back_to_a_placeholder_when_unspawnable():
    cx.reset_probe_caches()
    assert cx.codex_version("/no/such/codex/binary") == "unknown version"


def test_exec_help_timeout_makes_approval_args_refuse_to_launch(monkeypatch):
    """`codex_exec_help`'s except clause names `OSError,
    subprocess.TimeoutExpired, UnicodeDecodeError` together, so a plain
    unspawnable-binary test (above) exercises the tuple but never proves the
    TimeoutExpired member specifically fires. A CLI that hangs past the
    timeout must degrade exactly like one that was never found — `None` help
    text, which then makes `approval_args` refuse rather than launch a
    session with no known way to suppress its approval prompt."""
    cx.reset_probe_caches()

    def _timeout(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd=["codex", "exec", "--help"], timeout=10.0)

    monkeypatch.setattr(cx.subprocess, "run", _timeout)
    help_text = cx.codex_exec_help("/bin/codex")
    assert help_text is None
    with pytest.raises(BackendUnavailable):
        cx.approval_args(help_text, "codex-cli 0.149.0")


def test_codex_version_timeout_degrades_to_the_placeholder(monkeypatch):
    """Same TimeoutExpired-specific proof for `codex_version`: a hang must
    degrade to the placeholder string, not raise or hang the caller."""
    cx.reset_probe_caches()

    def _timeout(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd=["codex", "--version"], timeout=10.0)

    monkeypatch.setattr(cx.subprocess, "run", _timeout)
    assert cx.codex_version("/bin/codex") == "unknown version"


# --------------------------------------------------------------------------- #
# 2c. The legal wording is sourced, honest, and behaviour-free                 #
# --------------------------------------------------------------------------- #

def _shipped_src_and_docs(root: Path) -> list[Path]:
    """The git-TRACKED ``.py`` under ``src/`` and ``.md`` under ``docs/`` — the
    files that can actually ship. An ``rglob`` over the filesystem also sweeps up
    untracked local working notes (a dev's scratch plan under docs/), which a
    "no *shipped* file …" guard must never judge and which no clean checkout or
    CI run even has — only what git tracks reaches the export.
    """
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "src", "docs"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [
        root / p
        for p in out.split("\0")
        if p and ((p.startswith("src/") and p.endswith(".py"))
                  or (p.startswith("docs/") and p.endswith(".md")))
    ]


def test_no_shipped_file_asserts_an_unsourced_openai_prohibition():
    """AC1: no shipped file under src/ or docs/ asserts, as fact, that
    OpenAI's terms prohibit using ChatGPT to power a third-party service —
    that claim was never sourced (see the module docstring's withdrawal)."""
    banned = re.compile(r"(terms )?prohibit\w*\s+(using\s+)?ChatGPT", re.I)
    literal = "power third-party services"

    # Positive control: the scanner catches the retired sentence itself, and
    # the literal substring it used to assert.
    needle = "OpenAI's terms prohibit using ChatGPT to power third-party services."
    assert banned.search(needle)
    assert literal in needle

    root = Path(cx.__file__).resolve().parents[3]
    scanned = _shipped_src_and_docs(root)
    assert scanned, "the scan must not be scanning nothing"
    # Second positive control: this file is in the scanned set and carries a
    # known-present token, so an empty/misdirected scan cannot pass silently.
    assert any(
        p.name == "codex_backend.py" and "preferred_auth_method" in p.read_text(encoding="utf-8")
        for p in scanned
    )

    offenders = []
    for path in scanned:
        text = path.read_text(encoding="utf-8")
        if banned.search(text) or literal in text:
            offenders.append(str(path.relative_to(root)))
    assert not offenders, f"unsourced OpenAI prohibition claim in: {offenders}"


def test_the_api_key_comment_names_the_real_enforcement_and_not_the_ignored_flag():
    """AC2: the module docstring's ``"api_key"`` bullet must name the REAL
    enforcement path (`assert_api_key_billing_path`, `CODEX_HOME`,
    `_child_env_api_key`) rather than resting on the false claim that
    `preferred_auth_method` alone stops a silent ChatGPT fallback —
    codex-cli 0.149.0 silently ignores that flag (see the module docstring
    and `assert_api_key_billing_path`'s own docstring for the live evidence).

    Retired claim, verbatim as it shipped before this fix: 'so the CLI cannot
    silently fall back to a ChatGPT credential that happens to be on the
    machine' — adjacent to `preferred_auth_method` in the same bullet. That
    sentence must be gone from every shipped .py/.md file."""
    doc = " ".join(cx.__doc__.split())
    for name in ("assert_api_key_billing_path", "CODEX_HOME", "_child_env_api_key"):
        assert name in doc, f"module docstring is missing: {name!r}"

    retired = re.compile(
        r"preferred_auth_method.{0,80}cannot silently fall back"
        r"|cannot silently fall back.{0,80}preferred_auth_method",
        re.I | re.S,
    )
    # Positive control: the scanner itself catches the retired sentence.
    needle = (
        'passes ``preferred_auth_method=apikey`` so the CLI cannot silently '
        "fall back to a ChatGPT credential that happens to be on the machine."
    )
    assert retired.search(needle), "the scanner's own regex is broken"

    root = Path(cx.__file__).resolve().parents[3]
    scanned = _shipped_src_and_docs(root)
    assert scanned, "the scan must not be scanning nothing"
    assert any(
        p.name == "codex_backend.py" and "preferred_auth_method" in p.read_text(encoding="utf-8")
        for p in scanned
    ), "positive control failed — preferred_auth_method not found anywhere scanned"

    offenders = [
        str(p.relative_to(root))
        for p in scanned
        if retired.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"retired false claim still shipped in: {offenders}"


_TIMEOUT_GUARD_NAMES = {"TimeoutExpired", "SubprocessError", "Exception", "BaseException"}


def _handler_names(expr):
    """Every dotted/bare name an `except` clause's type expression names,
    e.g. `except (OSError, subprocess.TimeoutExpired):` -> {"OSError",
    "TimeoutExpired"}. Only the LAST attribute segment is kept, so both
    `subprocess.TimeoutExpired` and a bare `TimeoutExpired` (post
    `from subprocess import TimeoutExpired`) match the same guard name."""
    if isinstance(expr, ast.Tuple):
        names = set()
        for elt in expr.elts:
            names |= _handler_names(elt)
        return names
    if isinstance(expr, ast.Name):
        return {expr.id}
    if isinstance(expr, ast.Attribute):
        return {expr.attr}
    return set()


def _unguarded_subprocess_run_lines(source: str) -> list[int]:
    """AC3: return the line number of every `subprocess.run(...)` call in
    `source` that is NOT lexically inside a `try` whose `except` handlers
    name `TimeoutExpired` (or a class that already catches it —
    `SubprocessError`, `Exception`, `BaseException`, or a bare `except:`).

    A PROPERTY check over the whole syntax tree, not a remembered line-number
    list — it covers every `subprocess.run` call written today and any added
    to either file later, per the standing rule that an enumeration in a
    clear-list reproduces the very bug an exhaustive check would catch."""
    tree = ast.parse(source)
    offenders = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            self.try_stack = []

        def _guarded(self):
            for handlers in self.try_stack:
                for h in handlers:
                    if h.type is None:  # bare `except:`
                        return True
                    if _handler_names(h.type) & _TIMEOUT_GUARD_NAMES:
                        return True
            return False

        def visit_Try(self, node):
            self.try_stack.append(node.handlers)
            self.generic_visit(node)
            self.try_stack.pop()

        def visit_Call(self, node):
            func = node.func
            is_subprocess_run = (
                isinstance(func, ast.Attribute) and func.attr == "run"
                and isinstance(func.value, ast.Name) and func.value.id == "subprocess"
            )
            if is_subprocess_run and not self._guarded():
                offenders.append(node.lineno)
            self.generic_visit(node)

    _Visitor().visit(tree)
    return offenders


def test_every_auth_path_subprocess_probe_catches_TimeoutExpired():
    """AC3's durable regression proof: every `subprocess.run` call in the
    Codex auth-probe surface (`codex_backend.py`) and its caller
    (`doctor.py`) must sit inside a `try` that would catch a hang, not just
    the OSError-shaped absences the fixture-based tests above already cover.
    A future probe added to either file without a matching `except` would be
    exactly this ticket's class of defect: a silent path that never fails
    closed on a hang.

    Proved non-vacuous with three synthetic sources before trusting it on
    the real files: an unguarded call (must be flagged), an OSError-only
    guard (must still be flagged — OSError alone does not catch
    TimeoutExpired), and a TimeoutExpired-guarded call (must NOT be
    flagged)."""
    unguarded = "import subprocess\nsubprocess.run(['x'])\n"
    assert _unguarded_subprocess_run_lines(unguarded) == [2]

    wrong_guard = (
        "import subprocess\n"
        "try:\n"
        "    subprocess.run(['x'])\n"
        "except OSError:\n"
        "    pass\n"
    )
    assert _unguarded_subprocess_run_lines(wrong_guard) == [3]

    right_guard = (
        "import subprocess\n"
        "try:\n"
        "    subprocess.run(['x'])\n"
        "except (OSError, subprocess.TimeoutExpired):\n"
        "    pass\n"
    )
    assert _unguarded_subprocess_run_lines(right_guard) == []

    root = Path(cx.__file__).resolve().parents[3]
    targets = [
        root / "src" / "no_human" / "agent" / "codex_backend.py",
        root / "src" / "no_human" / "doctor.py",
    ]
    for target in targets:
        assert target.is_file(), f"scan target missing: {target}"
        offenders = _unguarded_subprocess_run_lines(
            target.read_text(encoding="utf-8"))
        assert not offenders, (
            f"{target.relative_to(root)}: subprocess.run at line(s) "
            f"{offenders} not guarded against TimeoutExpired"
        )


def test_the_module_docstring_cites_its_primary_source_and_both_halves():
    """AC2 + AC3: the honest replacement names its primary source (both
    URLs, the fetch date, all three quotes' distinguishing fragments, and
    the unfavourable programmatic-workflow guidance) and names the
    third-party sign-in question as OPEN rather than resolved, citing
    discussion #8338 and what it did and did not answer."""
    doc = " ".join(cx.__doc__.split())  # collapse line-wrap whitespace
    for fragment in (
        "two ways for a person to sign in",
        "both sign-in methods for local work",
        "programmatic Codex CLI workflows",
        "developers.openai.com/codex/auth",
        "learn.chatgpt.com/docs/auth",
        "2026-08-22",
        "CI/CD",
        "API key",
        "8338",
        "licensing",
        "lawyer",
    ):
        assert fragment in doc, f"module docstring is missing: {fragment!r}"
    assert "third-party" in doc or "third party" in doc
    assert "unresolved" in doc or "unanswered" in doc


def test_the_only_codex_login_argv_built_anywhere_is_login_status():
    """The one invariant the deleted `test_the_wording_change_added_no_
    subscription_machinery` covered that nothing else does: this codebase
    never builds a bare `codex login` (which would pop a browser) or a
    `login_chatgpt` subcommand — every quoted `"login"` token that appears in
    the Codex feature's own files is immediately followed by `"status"`, the
    one read-only existence probe `codex_login_status` runs (see
    `[cli, "login", "status"]` above). Scoped to the files this feature
    actually touches (`codex_backend.py`, `config.py`, `agent/backend.py`,
    `doctor.py`) rather than all of `src/no_human`, because unrelated code
    (GitHub API `login` fields, the unrelated `brain login` CLI command) also
    spells the bare word `"login"` and would otherwise false-positive.

    POSITIVE CONTROL: `preferred_auth_method` (known present in
    codex_backend.py) proves the scan reaches real content, and the scan
    must actually find at least one quoted `"login"` token (the real
    `[cli, "login", "status"]` call) — so an empty offenders list means
    'checked and clean', not 'the regex never matched anything'."""
    codex_files = [
        Path(cx.__file__),
        Path(cx.__file__).resolve().parents[1] / "config.py",
        Path(cx.__file__).resolve().parent / "backend.py",
        Path(cx.__file__).resolve().parents[1] / "doctor.py",
    ]
    for p in codex_files:
        assert p.is_file(), f"expected file missing: {p}"

    login_token_re = re.compile(r'''["']login["']''')
    login_status_re = re.compile(r'''["']login["']\s*,\s*["']status["']''')

    positive_control_hit = False
    login_tokens_found = 0
    offenders: list[str] = []
    for path in codex_files:
        text = path.read_text(encoding="utf-8")
        if "preferred_auth_method" in text:
            positive_control_hit = True
        if "login_chatgpt" in text:
            offenders.append(f"{path}: login_chatgpt")
        for m in login_token_re.finditer(text):
            login_tokens_found += 1
            window = text[m.start():m.start() + 40]
            if not login_status_re.match(window):
                line_no = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path}:{line_no}: 'login' not followed by \"status\"")

    assert positive_control_hit, (
        "positive control failed — 'preferred_auth_method' was not found in "
        "any of the scanned files, so the scan cannot be trusted"
    )
    assert login_tokens_found > 0, (
        "the scan found no quoted 'login' token at all — it is scanning the "
        "wrong files, not confirming a real absence"
    )
    assert not offenders, (
        "a bare `codex login` (or `login_chatgpt`) argv was built where only "
        "`codex login status` is sanctioned:\n" + "\n".join(offenders)
    )


# --------------------------------------------------------------------------- #
# 3. Codex: usage arithmetic                                                   #
# --------------------------------------------------------------------------- #

def test_cached_input_is_subtracted_out_of_the_fresh_total():
    """OpenAI reports `input_tokens` INCLUDING `cached_input_tokens`; Anthropic
    reports it EXCLUDING cache reads, and `core.pricing` weights the two
    classes at 1.0 and 0.1. Adding the raw figure would charge every cached
    token twice and fire the budget gate early on exactly the long sessions
    where caching matters most."""
    u = cx._Usage.parse({"input_tokens": 10_000, "cached_input_tokens": 9_000,
                         "output_tokens": 500})
    assert u.cache_read_tokens == 9_000
    assert u.tokens_used == 1_000 + 500       # NOT 10_000 + 500
    assert u.output_tokens == 500


def test_a_usage_block_that_reports_nothing_is_absent_not_zero():
    assert cx._Usage.parse({"input_tokens": 0, "output_tokens": 0}) is None
    assert cx._Usage.parse(None) is None
    assert cx._Usage.parse("nonsense") is None


def test_a_cached_share_larger_than_the_input_clamps_instead_of_going_negative():
    u = cx._Usage.parse({"input_tokens": 100, "cached_input_tokens": 900,
                         "output_tokens": 7})
    assert u.tokens_used == 7  # 0 fresh, not -793


# --------------------------------------------------------------------------- #
# 4. Codex: a whole run over a fake `codex` process                            #
# --------------------------------------------------------------------------- #

def _fake_codex(lines: list, *, returncode: int = 0, stderr: bytes = b""):
    """Monkeypatch target for ``asyncio.create_subprocess_exec``.

    A real subprocess is deliberately avoided: the point of these tests is the
    NORMALIZER, and shelling out would make them depend on a binary this
    machine does not have. ``stdout`` IS a real ``asyncio.StreamReader``
    (not a hand-rolled list-popper): ``_read_jsonl_line`` calls
    ``readuntil``/``readexactly``, not ``readline``, and only a real
    ``StreamReader`` implements those — this also makes the fake honour
    whatever ``limit=`` the production code passes, exactly like a real pipe.

    Each item in ``lines`` is a dict (JSON-encoded) or a raw ``str``/``bytes``
    line, written verbatim — for banner/malformed-JSON fixtures that must
    not be run through ``json.dumps``.
    """
    parts = []
    for line in lines:
        if isinstance(line, bytes):
            parts.append(line if line.endswith(b"\n") else line + b"\n")
        elif isinstance(line, str):
            parts.append(line.encode() + b"\n")
        else:
            parts.append(json.dumps(line).encode() + b"\n")
    payload = b"".join(parts)

    class _Stdin:
        def write(self, _data): pass
        async def drain(self): pass
        def close(self): pass

    class _Reader:
        def __init__(self, data: bytes):
            self._lines = data.splitlines(keepends=True)
        async def readline(self) -> bytes:
            return self._lines.pop(0) if self._lines else b""
        async def read(self) -> bytes:
            return b"".join(self._lines)

    class _Proc:
        def __init__(self, stdout):
            self.stdin, self.stdout = _Stdin(), stdout
            self.stderr = _Reader(stderr)
            self.returncode = None
            self.killed = False
        def kill(self): self.killed = True
        async def wait(self):
            self.returncode = returncode
            return returncode

    async def _spawn(*_args, **kwargs):
        # Built HERE, not eagerly at `_fake_codex()` call time: constructing
        # an `asyncio.StreamReader` with no explicit `loop=` needs a running
        # loop, and `_fake_codex()` is called from plain sync test code
        # before `asyncio.run()` starts one.
        reader = asyncio.StreamReader(limit=kwargs.get("limit", 65536))
        reader.feed_data(payload)
        reader.feed_eof()
        proc = _Proc(reader)
        _spawn.proc = proc
        return proc

    return _spawn


def _run(backend, monkeypatch, lines, *, max_turns=40, on_event=None, **kw):
    spawn = _fake_codex(lines, **kw)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    _stub_cli(monkeypatch)
    result = asyncio.run(backend.run(
        "do the thing", cwd=Path("/repo"), max_turns=max_turns,
        on_event=on_event))
    return result, spawn.proc


_HAPPY = [
    {"type": "thread.started", "thread_id": "th_42"},
    {"type": "item.completed", "item": {"id": "i0", "type": "reasoning",
                                        "text": "thinking about it"}},
    {"type": "item.started", "item": {"id": "i1", "type": "command_execution",
                                      "command": ["pytest", "-q"]}},
    {"type": "item.completed", "item": {"id": "i1", "type": "command_execution",
                                        "aggregated_output": "5 passed",
                                        "exit_code": 0}},
    {"type": "item.started", "item": {"id": "i2", "type": "file_change",
                                      "changes": {"src/a.py": {"kind": "update"}}}},
    {"type": "item.completed", "item": {"id": "i2", "type": "file_change",
                                        "changes": {"src/a.py": {"kind": "update"}}}},
    {"type": "item.completed", "item": {"id": "i3", "type": "agent_message",
                                        "text": "done: fixed the bug"}},
    {"type": "turn.completed", "usage": {"input_tokens": 12_000,
                                         "cached_input_tokens": 11_000,
                                         "output_tokens": 900}},
]


def test_a_successful_run_produces_the_result_the_orchestrator_reads(monkeypatch):
    events: list[AgentEvent] = []
    result, _ = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, _HAPPY,
                     on_event=events.append)
    assert result.is_error is False
    assert result.final_text == "done: fixed the bug"
    assert result.session_id == "th_42"
    assert result.stop_reason == "end_turn"
    assert result.tokens_used == 1_000 + 900
    assert result.cache_read_tokens == 11_000
    assert result.cache_creation_tokens == 0
    assert result.output_tokens == 900
    assert result.num_turns == 2  # one command, one file change
    kinds = [e.kind for e in events]
    assert kinds[-1] == "result"
    assert "thinking" in kinds and "text" in kinds
    assert "tool_use" in kinds and "tool_result" in kinds and "usage" in kinds


def test_tool_use_events_carry_the_vocabulary_the_orchestrator_switches_on(
        monkeypatch):
    """`_agent_sink` keys the doom-loop detector and, critically, the
    committed-file set on `tool_name in ("Write", "Edit", ...)` and
    `tool_input["file_path"]`. A backend that reported `file_change` /
    `changes` instead would commit nothing the agent wrote."""
    events: list[AgentEvent] = []
    _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, _HAPPY,
         on_event=events.append)
    uses = [e for e in events if e.kind == "tool_use"]
    assert [e.tool_name for e in uses] == ["Bash", "Write"]
    assert uses[0].tool_input == {"command": "pytest -q"}
    assert uses[1].tool_input == {"file_path": "src/a.py"}
    assert all(e.meta.get("tool_use_id") for e in uses)


def test_one_patch_touching_several_files_is_several_guard_checks(monkeypatch):
    """A per-path policy checked once per ITEM passes a patch whose SECOND hunk
    rewrites .env. One event per path is the whole reason `_tool_inputs`
    returns a list."""
    lines = [
        {"type": "item.started", "item": {
            "id": "i1", "type": "file_change",
            "changes": {"src/a.py": {}, "src/b.py": {}, "src/c.py": {}}}},
    ]
    events: list[AgentEvent] = []
    _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, lines,
         on_event=events.append)
    paths = [e.tool_input["file_path"] for e in events if e.kind == "tool_use"]
    assert sorted(paths) == ["src/a.py", "src/b.py", "src/c.py"]


def test_output_tokens_stays_null_when_no_usage_was_ever_reported(monkeypatch):
    """0 asserts "this run emitted no output"; None says "never reported". The
    distinction is load-bearing all the way to the DB column."""
    result, _ = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, [
        {"type": "item.completed",
         "item": {"id": "i", "type": "agent_message", "text": "hi"}},
    ])
    assert result.output_tokens is None
    assert result.tokens_used == 0


# --------------------------------------------------------------------------- #
# 5. Codex: the guard, and what it can and cannot do                           #
# --------------------------------------------------------------------------- #

_PUSH_TO_MAIN = [
    {"type": "item.started", "item": {"id": "i1", "type": "command_execution",
                                      "command": ["git", "push", "origin", "main"]}},
    {"type": "item.completed", "item": {"id": "i9", "type": "agent_message",
                                        "text": "pushed"}},
]


def test_a_guard_violation_kills_the_session_and_fails_the_attempt(monkeypatch):
    events: list[AgentEvent] = []
    result, proc = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch,
                        _PUSH_TO_MAIN, on_event=events.append)
    assert result.is_error is True
    assert result.stop_reason == "guard"
    assert result.denials and "main" in result.denials[0]
    assert proc.killed is True
    # Nothing after the violation is processed — the "pushed" message never
    # reaches the transcript.
    assert not any(e.kind == "text" and "pushed" in e.text for e in events)


def test_the_denial_event_marks_itself_post_hoc(monkeypatch):
    """THE capability mismatch, made unmissable at the point of use. On the
    Claude path the call is DENIED before it runs; here the push has already
    happened when we see it. A reader (or a future dashboard) must not be able
    to confuse the two."""
    events: list[AgentEvent] = []
    _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, _PUSH_TO_MAIN,
         on_event=events.append)
    denied = [e for e in events if e.kind == "denied"]
    assert len(denied) == 1
    assert denied[0].meta["post_hoc"] is True


def test_a_forbidden_path_write_is_caught_by_the_same_pure_policy(monkeypatch):
    result, _ = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, [
        {"type": "item.started", "item": {"id": "i1", "type": "file_change",
                                          "changes": {".env": {}}}},
    ])
    assert result.is_error and result.stop_reason == "guard"
    assert ".env" in result.denials[0]


def test_the_task_scoped_guard_lists_are_mutable_like_the_claude_backends():
    """The worker pool reuses one backend instance across tasks and the
    orchestrator rewrites both lists per task. A backend with frozen lists
    would carry one repo's protections into the next repo."""
    be = cx.CodexBackend()
    be.forbidden_paths = ["custom/"]
    be.never_push_to = ["develop"]
    assert be._guard_events("Bash", {"command": "git push origin develop"})
    assert be._guard_events("Write", {"file_path": "custom/x"})
    assert be._guard_events("Write", {"file_path": ".env"}) is None  # no longer listed


def test_hygiene_violation_does_not_kill_the_attempt(monkeypatch):
    """The `~/.cache/uv` false-positive this fix addresses: on the codex
    backend a guard denial used to ALWAYS kill the attempt, so a false
    "outside the worktree" venv-install verdict (advisory-shaped — bad
    install-target hygiene, not an attack) took the whole attempt down with
    it. A `GUARD_HYGIENE`-severity decision must instead fail only the one
    observed call (recorded in `denials` and as a "denied" event) and let the
    SAME already-running codex subprocess keep going — proven here by the
    later "pushed" message still reaching the transcript, which
    `test_a_guard_violation_kills_the_session_and_fails_the_attempt` (the
    GUARD_DESTRUCTIVE case, unchanged) proves does NOT happen when the
    violation is terminating."""
    def _hygiene_decision(*_a, **_kw):
        return cx.guard.GuardDecision(
            False,
            "install blocked: resolves to ~/.cache/uv, outside this "
            "session's worktree",
            severity=cx.guard.GUARD_HYGIENE,
        )
    monkeypatch.setattr(cx.guard, "evaluate", _hygiene_decision)
    events: list[AgentEvent] = []
    result, _proc = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch,
                         _PUSH_TO_MAIN, on_event=events.append)
    assert result.is_error is False
    assert result.stop_reason != "guard"
    assert result.denials and "cache/uv" in result.denials[0]
    assert any(e.kind == "text" and "pushed" in e.text for e in events)
    denied = [e for e in events if e.kind == "denied"]
    assert len(denied) == 1
    assert denied[0].meta["severity"] == cx.guard.GUARD_HYGIENE
    assert denied[0].meta["terminating"] is False


def test_destructive_violation_still_kills_the_attempt(monkeypatch):
    """The taxonomy's default direction, checked explicitly: a
    GUARD_DESTRUCTIVE (or GUARD_EXFILTRATION) decision still terminates the
    attempt exactly as every post-hoc denial did before this fix — adding the
    hygiene-class escape hatch must not soften the default."""
    def _destructive_decision(*_a, **_kw):
        return cx.guard.GuardDecision(
            False, "rm -rf outside the worktree",
            severity=cx.guard.GUARD_DESTRUCTIVE,
        )
    monkeypatch.setattr(cx.guard, "evaluate", _destructive_decision)
    events: list[AgentEvent] = []
    result, proc = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch,
                        _PUSH_TO_MAIN, on_event=events.append)
    assert result.is_error is True
    assert result.stop_reason == "guard"
    assert proc.killed is True
    assert not any(e.kind == "text" and "pushed" in e.text for e in events)
    denied = [e for e in events if e.kind == "denied"]
    assert len(denied) == 1
    assert denied[0].meta["terminating"] is True


# --------------------------------------------------------------------------- #
# 6. Codex: bounds, failures, and refusals                                     #
# --------------------------------------------------------------------------- #

def test_max_turns_is_enforced_here_because_codex_does_not_enforce_it(monkeypatch):
    lines = [
        {"type": "item.started", "item": {"id": f"i{n}", "type":
                                          "command_execution", "command": ["ls"]}}
        for n in range(10)
    ]
    result, proc = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, lines,
                        max_turns=3)
    assert result.num_turns == 3
    assert result.stop_reason == "max_turns"
    assert result.is_error is True
    assert "maximum number of turns" in result.final_text
    assert proc.killed is True


def test_a_vendor_error_becomes_a_failed_attempt_not_a_crash(monkeypatch):
    """Constraint §5: the bounded loop reads the result event. A backend that
    raised would crash the daemon instead of failing one attempt."""
    result, _ = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, [
        {"type": "turn.failed", "error": {"message": "insufficient_quota"}},
    ])
    assert result.is_error is True
    assert "insufficient_quota" in result.final_text
    # Regression guard: a quota error must NEVER be reclassified as a
    # model-not-found error — the two need different fixes (billing vs.
    # llm.codex_model) and conflating them would send an operator chasing
    # the wrong one.
    assert cx.model_error_from_failure(
        {"type": "turn.failed", "error": {"message": "insufficient_quota"}},
        "gpt-5-codex") is None


def test_model_error_from_failure_classifies_the_real_flat_404_shape():
    """The shape actually observed against the live API, confirmed in a prior
    session — NOT the nested `{"error": {...}}` shape originally assumed. A
    normalizer written only against the assumed shape would silently pass
    this straight through as an opaque `codex reported an error`."""
    msg = {
        "type": "error",
        "message": (
            "Reconnecting... 2/5 (unexpected status 404 Not Found: "
            "Model not found gpt-5-codex, url: "
            "https://api.openai.com/v1/responses, ...)"
        ),
    }
    exc = cx.model_error_from_failure(msg, "gpt-5-codex")
    assert exc is not None
    assert isinstance(exc, cx.CodexModelUnavailable)
    text = str(exc)
    assert "gpt-5-codex" in text
    assert "llm.codex_model" in text
    assert "/v1/responses" in text


def test_model_error_from_failure_also_classifies_the_documented_nested_shape():
    """The originally-documented vendor shape — kept as a second case so a
    future vendor change back to it (or a different endpoint using it) does
    not silently stop being caught."""
    msg = {"type": "turn.failed",
           "error": {"status": 404, "message": "Model not found: gpt-5-codex"}}
    exc = cx.model_error_from_failure(msg, "gpt-5-codex")
    assert exc is not None
    assert "gpt-5-codex" in str(exc)


def test_model_error_from_failure_classifies_the_not_supported_on_chatgpt_shape():
    """A third real vendor shape (this ticket's send-back review): a bad
    model id on a ChatGPT/subscription session is not a 404 at all — it is a
    `turn.failed`, status 400, `invalid_request_error`, with the message
    "The '<model>' model is not supported when using Codex with a ChatGPT
    account". Without this pattern, this shape fell through as an opaque
    `codex reported an error` instead of naming `llm.codex_model` as the
    thing to fix."""
    msg = {"type": "turn.failed", "error": {
        "status": 400,
        "type": "invalid_request_error",
        "message": (
            "The 'gpt-5-codex' model is not supported when using Codex "
            "with a ChatGPT account"
        ),
    }}
    exc = cx.model_error_from_failure(msg, "gpt-5-codex")
    assert exc is not None
    assert isinstance(exc, cx.CodexModelUnavailable)
    text = str(exc)
    assert "gpt-5-codex" in text
    assert "llm.codex_model" in text


def test_model_error_from_failure_leaves_unrelated_errors_alone():
    for msg in (
        {"type": "turn.failed", "error": {"message": "insufficient_quota"}},
        {"type": "turn.failed", "error": {"message": "rate_limit_exceeded"}},
        {"type": "error", "message": "network timeout"},
        {"type": "turn.failed"},
        {},
    ):
        assert cx.model_error_from_failure(msg, "gpt-5-codex") is None


def test_a_model_not_found_run_surfaces_the_typed_error_end_to_end(monkeypatch):
    """AC3, over the real `run()` path with the real flat-shape JSONL: the
    bounded loop reads `result.final_text`/`result.api_error_status`, not an
    exception, so the classification has to survive the trip through
    `stream()`'s error branch and into the result event's meta."""
    result, _ = _run(cx.CodexBackend(env=FAKE_ENV, model="gpt-5-codex"),
                     monkeypatch, [{
        "type": "error",
        "message": ("unexpected status 404 Not Found: Model not found "
                    "gpt-5-codex, url: https://api.openai.com/v1/responses"),
    }])
    assert result.is_error is True
    assert "gpt-5-codex" in result.final_text
    assert "llm.codex_model" in result.final_text
    assert result.api_error_status == 404


def test_a_nonzero_exit_with_no_json_still_yields_a_result_event(monkeypatch):
    result, _ = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, [],
                     returncode=2, stderr=b"codex: boom")
    assert result.is_error is True
    assert "boom" in result.final_text


def test_unparseable_and_unknown_records_are_skipped_not_fatal(monkeypatch):
    """A schema drift must degrade, not read as a code failure to the bounded
    loop — which would retry three times and escalate against the wrong cause."""
    # A banner line, a record of a type nobody here has seen, and a line
    # that isn't even JSON — all ahead of the one real event — must all be
    # skipped, not fatal.
    spawn = _fake_codex([
        b"Reading prompt from stdin...\n",
        b'{"type":"some.future.event","x":1}\n',
        b"{not json\n",
        {"type": "item.completed", "item": {
            "id": "i", "type": "agent_message", "text": "ok"}},
    ])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    _stub_cli(monkeypatch)
    result = asyncio.run(cx.CodexBackend(env=FAKE_ENV).run(
        "p", cwd=Path("/repo"), max_turns=9))
    assert result.is_error is False
    assert result.final_text == "ok"


@pytest.mark.parametrize("kwargs,needle", [
    ({"supervisor_hook": object()}, "supervisor_hook"),
    ({"lint_hook": object()}, "lint_hook"),
    ({"skills": ["verify"]}, "skills"),
    ({"agents": {"researcher": object()}}, "agents"),
])
def test_an_unsupported_control_is_refused_never_silently_dropped(
        monkeypatch, kwargs, needle):
    """A supervisor that never fires is worse than no supervisor, because the
    orchestrator reports that it supervised. Refusing is the only honest
    behaviour available to a backend that cannot run the check."""
    _stub_cli(monkeypatch)
    result = asyncio.run(cx.CodexBackend(env=FAKE_ENV).run(
        "p", cwd=Path("/repo"), max_turns=5, **kwargs))
    assert result.is_error is True
    assert result.stop_reason == "unsupported"
    assert needle in result.final_text


def test_an_exception_raised_by_on_event_propagates_out_of_run(monkeypatch):
    """The orchestrator's three abort controls — CancelRequested (nh task
    pause), BudgetAbort (the mid-attempt spend watch) and StuckAbort
    (doom-loop) — are ALL raised from inside `_agent_sink`. A backend that
    swallowed a callback exception would silently disable all three, and the
    only visible symptom would be attempts that never stop."""
    class _Abort(Exception):
        pass

    def _sink(event):
        if event.kind == "tool_use":
            raise _Abort("stop now")

    spawn = _fake_codex(_HAPPY)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    _stub_cli(monkeypatch)
    with pytest.raises(_Abort):
        asyncio.run(cx.CodexBackend(env=FAKE_ENV).run(
            "p", cwd=Path("/repo"), max_turns=40, on_event=_sink))
    # …and the subprocess is killed on the way out, or a cancelled attempt
    # leaves a live `codex` writing into the tree about to be committed.
    assert spawn.proc.killed is True


# --------------------------------------------------------------------------- #
# 7. The credential rules (config.py)                                          #
# --------------------------------------------------------------------------- #
#
# No real credential appears below. Every value is a literal placeholder.

def test_the_openai_key_is_refused_in_config_yaml_like_the_anthropic_one(
        tmp_path):
    """The mode may live in config; the key never does. A guard that named only
    the first vendor would have missed the second — which is the whole reason
    the second vendor's key is named in the same breath."""
    from no_human import config as cfgmod

    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "openai_api_key"):
        with pytest.raises(cfgmod.AuthError) as exc:
            cfgmod._reject_api_key_in_config({"llm": {key: "placeholder"}})
        assert key.upper() in str(exc.value)
        assert ".env" in str(exc.value)
    # A key-shaped VALUE is not a key-shaped NAME; this guard is about names.
    cfgmod._reject_api_key_in_config({"worker": {"backend": "codex"}})


def test_codex_api_key_mode_refuses_without_the_key_and_names_the_subscription_alternative(
        tmp_path, monkeypatch):
    """Reworded 2026-08-22: this used to assert the message says there is NO
    subscription path at all. Since the amendment sanctioning
    `llm.codex_auth_mode: subscription`, api_key mode's refusal instead NAMES
    that sibling mode as the alternative — kept byte-identical in signature
    and control flow (`assert_codex_api_key_mode`, see its docstring); only
    this message text changed."""
    from no_human import config as cfgmod

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    empty = tmp_path / ".env"
    empty.write_text("")
    with pytest.raises(cfgmod.AuthError) as exc:
        cfgmod.assert_codex_api_key_mode(empty)
    msg = str(exc.value)
    assert "OPENAI_API_KEY" in msg
    assert "llm.codex_auth_mode" in msg
    assert "subscription" in msg
    assert "ChatGPT" in msg           # names the reason, not just the rule
    assert "config.yaml" in msg
    assert "prohibit" not in msg.lower()  # the unsourced claim is withdrawn
    assert "lawyer" in msg.lower()  # names the uncertainty honestly


def test_codex_mode_scrubs_the_routings_that_would_move_the_bill(
        tmp_path, monkeypatch):
    """`assert_codex_api_key_mode` is the OpenAI half of "exactly one billing
    path per vendor": the key the operator chose stays, every redirect to
    another endpoint or another account goes."""
    from no_human import config as cfgmod

    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=placeholder-not-a-real-key\n")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for var in cfgmod.CODEX_ALTERNATE_ROUTING_VARS:
        monkeypatch.setenv(var, "placeholder")

    report = cfgmod.assert_codex_api_key_mode(env_file)

    assert sorted(report.removed) == sorted(cfgmod.CODEX_ALTERNATE_ROUTING_VARS)
    import os
    for var in cfgmod.CODEX_ALTERNATE_ROUTING_VARS:
        assert var not in os.environ
    assert os.environ["OPENAI_API_KEY"] == "placeholder-not-a-real-key"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_the_openai_key_is_not_added_to_the_anthropic_scrub_list():
    """METERED_AUTH_VARS runs on EVERY start, including runs that use no
    OpenAI at all. Widening it would delete an operator's OPENAI_API_KEY out of
    their shell for a Claude-only run — a different program's setting, removed
    by us, silently."""
    from no_human import config as cfgmod

    assert cfgmod.CODEX_API_KEY_VAR not in cfgmod.METERED_AUTH_VARS
    assert not set(cfgmod.CODEX_ALTERNATE_ROUTING_VARS) & set(
        cfgmod.METERED_AUTH_VARS)


def test_the_default_config_still_selects_claude():
    """The acceptance criterion, at the config layer."""
    from no_human.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["worker"]["backend"] == "claude"
    assert resolve_backend_name(DEFAULT_CONFIG) == "claude"
    # And the Codex keys exist with inert defaults, so reading them never
    # depends on a key the operator has not written.
    assert DEFAULT_CONFIG["llm"]["codex_reasoning_effort"] is None
    assert DEFAULT_CONFIG["llm"]["codex_cli_path"] is None
    # The four Claude tiers are untouched by this change.
    assert DEFAULT_CONFIG["llm"]["primary_model"] == "claude-sonnet-5"
    assert DEFAULT_CONFIG["llm"]["review_model"] == "claude-opus-4-8"
    assert DEFAULT_CONFIG["llm"]["planner_model"] == "claude-opus-5"
    assert DEFAULT_CONFIG["llm"]["supervisor_model"] == "claude-sonnet-5"
    assert DEFAULT_CONFIG["llm"]["utility_model"] == "claude-haiku-4-5"


@pytest.mark.real_backend
def test_every_boolean_capability_is_true_for_claude():
    """THE MECHANISM by which the Claude path is unchanged, asserted by
    DISCOVERY rather than by a hand-written list.

    The orchestrator gates each optional feature on `caps.<field>`, so the
    Claude path is byte-for-byte the old behaviour precisely because every one
    of those fields is True. A future capability field added as False-for-Claude
    would silently switch a feature off for the DEFAULT backend, and an
    enumerated check could not catch it — the field it would need to name is the
    one that does not exist yet.
    """
    import dataclasses

    caps = ClaudeBackend(model="claude-sonnet-5").capabilities
    false_fields = [
        f.name for f in dataclasses.fields(caps)
        if f.type in ("bool", bool) and getattr(caps, f.name) is not True
    ]
    assert not false_fields, (
        f"the DEFAULT backend declares {false_fields} as not-True. Every "
        "orchestrator feature gate reads these, so a False here turns a "
        "feature off for every operator who changed nothing.")
    # …and the check is not vacuous: it really did look at fields.
    assert sum(1 for f in dataclasses.fields(caps)
               if f.type in ("bool", bool)) >= 8


# --------------------------------------------------------------------------- #
# 8. Codex: the "subscription" auth mode                                      #
# --------------------------------------------------------------------------- #
#
# No real credential appears below, and no test in this section shells out to
# a real `codex` binary or reads `~/.codex/auth.json` — `subprocess.run` and
# `codex_login_status` are both monkeypatched.

def test_default_codex_model_is_split_per_mode():
    """The api_key and subscription defaults are DIFFERENT ids, per the
    operator's own same-day measurement recorded above `DEFAULT_CODEX_MODEL`
    in `agent/backend.py`: a live ChatGPT session refuses the codex-branded
    ids the api_key default uses."""
    assert seam.default_codex_model("api_key") == seam.DEFAULT_CODEX_MODEL
    assert seam.default_codex_model("subscription") == seam.DEFAULT_CODEX_MODEL_SUBSCRIPTION
    assert seam.DEFAULT_CODEX_MODEL != seam.DEFAULT_CODEX_MODEL_SUBSCRIPTION
    # Anything else falls to the api_key default rather than raising — the
    # fail-loud check on an unrecognised mode string lives in
    # `config.codex_auth_mode`, not duplicated here.
    assert seam.default_codex_model("nonsense") == seam.DEFAULT_CODEX_MODEL


def test_make_backend_resolves_the_model_from_the_configured_auth_mode():
    """The seam acceptance criterion for AC's model-default requirement: a
    config that selects subscription mode and sets no explicit
    `llm.codex_model` gets the SUBSCRIPTION default, not the api_key one."""
    be = make_backend(model="claude-sonnet-5", config={
        "worker": {"backend": "codex"},
        "llm": {"codex_auth_mode": "subscription"},
    })
    assert isinstance(be, cx.CodexBackend)
    assert be.model == seam.DEFAULT_CODEX_MODEL_SUBSCRIPTION
    assert be.auth_mode == "subscription"

    be2 = make_backend(model="claude-sonnet-5", config={
        "worker": {"backend": "codex"},
    })
    assert be2.model == seam.DEFAULT_CODEX_MODEL
    assert be2.auth_mode == "api_key"

    # An explicit llm.codex_model still wins over EITHER per-mode default.
    be3 = make_backend(model="claude-sonnet-5", config={
        "worker": {"backend": "codex"},
        "llm": {"codex_auth_mode": "subscription", "codex_model": "gpt-5.5"},
    })
    assert be3.model == "gpt-5.5"


def test_subscription_mode_omits_the_api_key_forcing_flag(monkeypatch):
    """The mirror image of `test_the_command_forces_api_key_auth_and_never_
    offers_a_login` above: in subscription mode there is no key to force, and
    forcing `preferred_auth_method="apikey"` here would make the CLI refuse
    the very ChatGPT session this mode exists to use."""
    _stub_cli(monkeypatch)
    cmd = cx.CodexBackend(auth_mode="subscription")._command(
        Path("/repo"), effort=None, resume=None)
    joined = " ".join(cmd)
    assert "preferred_auth_method" not in joined
    # Still never an unsandboxed run, and still never offers to log in itself.
    assert "--sandbox" in cmd and "workspace-write" in cmd
    assert "login" not in joined
    # The network grant does not depend on which auth mode pays for the run.
    assert "sandbox_workspace_write.network_access=true" in joined


def test_subscription_child_env_holds_no_openai_credential(monkeypatch):
    """`_child_env_subscription` scrubs every var in
    `CODEX_SUBSCRIPTION_SCRUB_VARS` from the CHILD env — both spellings of the
    key plus every alternate routing — after a session check reports a live
    ChatGPT session."""
    monkeypatch.setattr(cx, "codex_login_status",
                        lambda cli_path=None: cx.CodexSessionStatus(True, "chatgpt"))
    env_in = {"PATH": "/bin", "OPENAI_API_KEY": "not-a-real-key",
              "CODEX_API_KEY": "also-not-real", "OPENAI_BASE_URL": "https://evil"}
    env = cx.CodexBackend(auth_mode="subscription", env=env_in)._child_env()
    from no_human import config as cfgmod
    for var in cfgmod.CODEX_SUBSCRIPTION_SCRUB_VARS:
        assert var not in env
    # Literal-name pins: iterating CODEX_SUBSCRIPTION_SCRUB_VARS above proves
    # nothing if a var is deleted from that list — assert the specific names
    # directly so a shrunk list still fails this test.
    assert "OPENAI_API_KEY" not in env
    assert "CODEX_API_KEY" not in env
    assert "OPENAI_BASE_URL" not in env
    assert env["PATH"] == "/bin"


def test_subscription_mode_refuses_when_no_chatgpt_session_is_found(monkeypatch):
    monkeypatch.setattr(cx, "codex_login_status",
                        lambda cli_path=None: cx.CodexSessionStatus(False, "none"))
    with pytest.raises(cx.CodexAuthError) as exc:
        cx.CodexBackend(auth_mode="subscription", env={"PATH": "/bin"})._child_env()
    msg = str(exc.value)
    assert "codex login" in msg
    assert "llm.codex_auth_mode" in msg and "api_key" in msg


def test_subscription_mode_refuses_an_api_key_backed_session(monkeypatch):
    """A session `codex login status` reports as api_key-backed is not the
    plan this mode exists to spend — refused the same as no session."""
    monkeypatch.setattr(cx, "codex_login_status",
                        lambda cli_path=None: cx.CodexSessionStatus(True, "api_key"))
    with pytest.raises(cx.CodexAuthError, match="codex login"):
        cx.CodexBackend(auth_mode="subscription", env={"PATH": "/bin"})._child_env()


def test_subscription_mode_accepts_an_unrecognised_but_present_session(monkeypatch):
    monkeypatch.setattr(cx, "codex_login_status",
                        lambda cli_path=None: cx.CodexSessionStatus(True, "unknown"))
    env = cx.CodexBackend(auth_mode="subscription",
                          env={"PATH": "/bin"})._child_env()
    # `_child_env` also stamps the agent-session mark (session_mark.py) onto
    # every Codex subprocess env now, so the accepted-session path returns
    # PATH plus exactly the two mark vars — not a bare passthrough anymore.
    from no_human.agent.session_mark import mark_env
    assert env == {"PATH": "/bin", **mark_env("codex")}


# --------------------------------------------------------------------------- #
# 9. Codex: codex_login_status — the read-only session probe                  #
# --------------------------------------------------------------------------- #

def _fake_run(returncode=0, stdout="", stderr=""):
    class _CP:
        pass
    cp = _CP()
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    def _run(*_args, **_kwargs):
        return cp
    return _run


def test_login_status_absent_cli_reports_not_present(monkeypatch):
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: None)
    status = cx.codex_login_status()
    assert status.present is False and status.via == "none"


def test_login_status_recognises_a_chatgpt_session(monkeypatch):
    import subprocess
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", _fake_run(
        returncode=0, stdout="Logged in using ChatGPT"))
    status = cx.codex_login_status()
    assert status.present is True and status.via == "chatgpt"


def test_login_status_recognises_an_api_key_session(monkeypatch):
    import subprocess
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", _fake_run(
        returncode=0, stdout="Logged in using an API key"))
    status = cx.codex_login_status()
    assert status.present is True and status.via == "api_key"


def test_login_status_unrecognised_wording_still_counts_as_present(monkeypatch):
    import subprocess
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", _fake_run(
        returncode=0, stdout="Session: ok, account: acct_123"))
    status = cx.codex_login_status()
    assert status.present is True and status.via == "unknown"


def test_login_status_nonzero_exit_reports_not_present(monkeypatch):
    import subprocess
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    monkeypatch.setattr(subprocess, "run", _fake_run(
        returncode=1, stderr="Not logged in"))
    status = cx.codex_login_status()
    assert status.present is False


def test_login_status_timeout_or_oserror_never_raises(monkeypatch):
    import subprocess
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: "/bin/codex")

    def _timeout(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="codex", timeout=10.0)
    monkeypatch.setattr(subprocess, "run", _timeout)
    status = cx.codex_login_status()
    assert status.present is False and "timed out" in status.detail

    def _oserror(*_a, **_kw):
        raise FileNotFoundError("no such file")
    monkeypatch.setattr(subprocess, "run", _oserror)
    status = cx.codex_login_status()
    assert status.present is False


def test_login_status_timeout_is_absent_not_present(monkeypatch):
    """The test above proves a timeout never raises; this one pins the
    specific verdict it must degrade to. `status.via` matters here, not just
    `status.present`: `assert_api_key_billing_path` refuses on `present is
    False` OR `via != "api_key"`, so either a wrong-but-truthy `via` or a
    `present=True` on a hang would both be silent-fallback-shaped defects
    that `test_login_status_timeout_or_oserror_never_raises` alone would not
    catch, since it never inspects `via` on the timeout branch."""
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: "/bin/codex")

    def _timeout(*_a, **_kw):
        raise subprocess.TimeoutExpired(cmd="codex", timeout=10.0)
    monkeypatch.setattr(subprocess, "run", _timeout)
    status = cx.codex_login_status()
    assert status.present is False
    assert status.via == "none"
    assert "timed out" in status.detail


def test_login_status_scrubs_its_own_subprocess_env(monkeypatch):
    """A stray `OPENAI_API_KEY` on the machine running the probe must not
    make the CLI answer 'logged in with an API key' and slip a key-backed
    session past the subscription-mode gate — the probe scrubs its own
    child env before asking."""
    import subprocess
    monkeypatch.setattr(cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-reach-the-child")
    seen_env = {}

    def _run(*_args, **kwargs):
        seen_env.update(kwargs.get("env") or {})
        class _CP:
            returncode = 0
            stdout = "Logged in using ChatGPT"
            stderr = ""
        return _CP()
    monkeypatch.setattr(subprocess, "run", _run)
    cx.codex_login_status()
    from no_human import config as cfgmod
    for var in cfgmod.CODEX_SUBSCRIPTION_SCRUB_VARS:
        assert var not in seen_env
    # Literal-name pin: the loop above proves nothing if OPENAI_API_KEY is
    # deleted from CODEX_SUBSCRIPTION_SCRUB_VARS — assert the actual
    # credential this test sets is gone, by name.
    assert "OPENAI_API_KEY" not in seen_env


# --------------------------------------------------------------------------- #
# 10. Codex: the vendor "wrong account type for this model" refusal           #
# --------------------------------------------------------------------------- #

def test_a_model_account_mismatch_is_classified_not_left_as_a_raw_failure(
        monkeypatch):
    """The vendor phrase observed 2026-08-22 under a live ChatGPT session,
    refusing a codex-branded model id — surfaced as a typed
    CodexModelUnsupportedError via `stop_reason == "model_unsupported"`,
    not just another opaque failed-attempt string. Uses api_key mode here so
    the fixture exercises only the classification wiring, not the separate
    session-check gate covered by the subscription-mode tests above."""
    result, _ = _run(
        cx.CodexBackend(env=FAKE_ENV, model="gpt-5.3-codex"),
        monkeypatch,
        [{"type": "turn.failed", "error": {
            "message": "The 'gpt-5.3-codex' model is not supported when "
                       "using Codex with a ChatGPT account."}}],
    )
    assert result.is_error is True
    assert result.stop_reason == "model_unsupported"
    assert "gpt-5.3-codex" in result.final_text
    assert "llm.codex_auth_mode" in result.final_text or "llm.codex_model" in result.final_text


def test_an_unrelated_vendor_failure_is_not_misclassified(monkeypatch):
    """`_classify_vendor_error` is a substring match on one specific vendor
    phrase — anything else must fall through as a plain failure rather than
    being mislabelled as a model/account mismatch."""
    result, _ = _run(cx.CodexBackend(env=FAKE_ENV), monkeypatch, [
        {"type": "turn.failed", "error": {"message": "insufficient_quota"}},
    ])
    assert result.is_error is True
    assert result.stop_reason != "model_unsupported"


def test_the_bad_key_401_shapes_are_not_misclassified_as_model_unsupported():
    """Negative control, personally confirmed live this ticket against a
    bogus `OPENAI_API_KEY`: an invalid key produces a 401-shaped vendor
    error, worded either 'Missing bearer or basic authentication' or 'auth
    error code: invalid_api_key' — NEITHER of which is a model/account
    mismatch. Both `model_error_from_failure` (the 404 "model not found"
    classifier) and `_classify_vendor_error` (the "wrong account type for
    this model" classifier) must fall through and return None for both, or
    an operator debugging a bad key would be misdirected at
    `llm.codex_model`/`llm.codex_auth_mode` instead of `~/.no_human/.env`."""
    for message in (
        "Missing bearer or basic authentication",
        "auth error code: invalid_api_key",
    ):
        assert cx.model_error_from_failure(
            {"type": "turn.failed", "error": {"message": message}},
            "gpt-5.3-codex",
        ) is None
        assert cx._classify_vendor_error(
            message, mode="api_key", model="gpt-5.3-codex") is None


# --------------------------------------------------------------------------- #
# 11. AC2's repo-wide guarantee: nothing SHIPPED ever names the credential    #
#     file, in code OR in the comments/docstrings/messages explaining why    #
#     not — a docstring that itself spells out the path is one `.format()`   #
#     typo away from becoming a read.                                       #
# --------------------------------------------------------------------------- #

def test_no_source_file_touches_the_chatgpt_credential_file():
    """`rglob("*.py")` over `src/no_human`, textually — not just AST/code —
    because the property under test is "the string never appears", and a
    docstring explaining what we don't do is exactly the kind of place a
    stray literal path creeps in. A POSITIVE CONTROL
    (`preferred_auth_method`, which src/no_human/agent/codex_backend.py and
    src/no_human/config.py both use for real, live code) proves the scanner
    can find text that IS there — without it, an empty result would be
    equally consistent with "safe" and "the scanner never ran"."""
    src_root = Path(cx.__file__).resolve().parents[1]
    assert src_root.name == "no_human"
    py_files = sorted(src_root.rglob("*.py"))
    assert py_files, "the scanner found no files at all — path is wrong"

    forbidden = ("auth.json", "~/.codex")
    offenders: list[str] = []
    positive_control_hit = False
    for path in py_files:
        text = path.read_text(encoding="utf-8")
        if "preferred_auth_method" in text:
            positive_control_hit = True
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{path}: {needle!r}")

    assert positive_control_hit, (
        "positive control failed — 'preferred_auth_method' was not found "
        "anywhere under src/no_human, so the scanner cannot be trusted to "
        "find text that IS there"
    )
    assert not offenders, (
        "a shipped source file names the ChatGPT credential file directly "
        "— reword to describe it without the literal path:\n"
        + "\n".join(offenders)
    )


# --------------------------------------------------------------------------- #
# 12. Docs — the verified CLI version and the entitlement rule must be STATED, #
#     not just true in code, so an operator reading the docs sees the same    #
#     ground truth this file's stub help text was built from.                 #
# --------------------------------------------------------------------------- #

def test_the_docs_state_the_verified_cli_version_and_the_entitlement_rule():
    docs = (Path(__file__).resolve().parent.parent / "docs" / "BACKENDS.md"
           ).read_text(encoding="utf-8")
    assert "codex exec --help" in docs, (
        "the docs must say flags are probed from the CLI's own --help output")
    assert "codex-cli 0.149.0" in docs, (
        "the docs must name the version this backend was verified against")
    assert "/v1/responses" in docs, (
        "the docs must state model entitlement needs a billed /v1/responses "
        "call — a doctor pass is not proof the configured model works")


# --------------------------------------------------------------------------- #
# 13. The write site owns its target.                                          #
#                                                                              #
#     PROPERTY: every path `materialise_api_key_auth` writes to resolves       #
#     strictly inside no_human's own root.                                     #
#                                                                              #
#     Before this section `materialise_api_key_auth` had NO direct test of any #
#     kind, and trusted its `home` argument. `home = Path.home() / ".codex"`   #
#     passed the whole suite — including                                       #
#     `test_no_source_file_touches_the_chatgpt_credential_file`, which forbids #
#     the TEXT "auth.json" while this module assembles that filename from two  #
#     fragments. A lexical guard cannot enforce a capability; these tests       #
#     exercise the write itself.                                               #
# --------------------------------------------------------------------------- #

_CRED = "auth" + ".json"


def test_materialise_writes_a_600_credential_inside_no_human_root(tmp_path):
    """Positive control. Without it, every refusal test below would pass
    equally well against a function that refuses everything."""
    nh_root = tmp_path / ".no_human"
    home = nh_root / "codex-home"
    home.mkdir(parents=True)

    cx.materialise_api_key_auth("sk-live-key", home, base=nh_root)

    written = home / _CRED
    assert json.loads(written.read_text(encoding="utf-8")) == {
        "auth_mode": "apikey", "OPENAI_API_KEY": "sk-live-key",
    }
    assert oct(written.stat().st_mode)[-3:] == "600"


def test_materialise_refuses_the_operators_own_credential_directory(tmp_path):
    """THE ATTACK, constructed rather than described.

    This is the defect the review found: the write happens BEFORE
    `assert_api_key_billing_path`, so a later refusal cannot un-destroy the
    file. The assertion is on the victim's BYTES, not on the exception —
    raising after clobbering would still be a total failure.
    """
    fake_home = tmp_path / "home"
    nh_root = fake_home / ".no_human"
    nh_root.mkdir(parents=True)
    operators_codex = fake_home / ".codex"
    operators_codex.mkdir()
    victim = operators_codex / _CRED
    victim.write_bytes(b'{"tokens":{"access_token":"REAL-CHATGPT-SESSION"}}')
    before = victim.read_bytes()

    with pytest.raises(cx.CodexAuthError):
        cx.materialise_api_key_auth("sk-x", operators_codex, base=nh_root)

    assert victim.read_bytes() == before, (
        "materialise_api_key_auth overwrote the operator's live ChatGPT "
        "credential — the file this product is required never to read, "
        "parse, copy or overwrite, in either auth mode"
    )


def test_materialise_refuses_a_symlink_that_points_out_of_the_root(tmp_path):
    """The string-vs-capability test.

    `link` is LITERALLY under the root — any implementation comparing path
    text passes this and still destroys the victim. Only resolving both sides
    catches it.
    """
    nh_root = tmp_path / ".no_human"
    nh_root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    victim = outside / _CRED
    victim.write_bytes(b"REAL-CREDENTIAL")
    link = nh_root / "codex-home"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(cx.CodexAuthError):
        cx.materialise_api_key_auth("sk-x", link, base=nh_root)

    assert victim.read_bytes() == b"REAL-CREDENTIAL"


def test_materialise_refuses_a_parent_traversal_back_out_of_the_root(tmp_path):
    nh_root = tmp_path / ".no_human"
    (nh_root / "codex-home").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    victim = outside / _CRED
    victim.write_bytes(b"REAL-CREDENTIAL")

    escape = nh_root / "codex-home" / ".." / ".." / "elsewhere"
    with pytest.raises(cx.CodexAuthError):
        cx.materialise_api_key_auth("sk-x", escape, base=nh_root)

    assert victim.read_bytes() == b"REAL-CREDENTIAL"


def test_materialise_cannot_truncate_through_a_symlinked_temp_path(tmp_path):
    """Containment covers the DIRECTORY; this covers the final component.

    The temp open used `O_TRUNC`, which follows a planted symlink and
    truncates the victim BEFORE any rename — the directory check alone was
    not enough. This test originally asserted `pytest.raises(OSError)`, which
    pinned the MECHANISM (`O_NOFOLLOW` rejecting the link) rather than the
    property. Unlink-then-`O_EXCL` now drops our name for the link and writes
    correctly instead of raising — strictly better behaviour that the old
    assertion called a failure. The assertion is therefore on what actually
    matters: the victim's bytes, and the credential landing in the right file.
    """
    nh_root = tmp_path / ".no_human"
    home = nh_root / "codex-home"
    home.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    victim = outside / "precious"
    victim.write_bytes(b"PRECIOUS")
    (home / f".{cx._CODEX_HOME_CRED_NAME}.tmp").symlink_to(victim)

    cx.materialise_api_key_auth("sk-x", home, base=nh_root)

    assert victim.read_bytes() == b"PRECIOUS", (
        "a symlink at the temp path captured the write")
    assert json.loads((home / _CRED).read_text(encoding="utf-8"))["OPENAI_API_KEY"] == "sk-x"


# --------------------------------------------------------------------------- #
# 14. The api_key gate fails CLOSED on every observation that is not an        #
#     unambiguous api_key session.                                             #
#                                                                              #
#     The `via` values below are written out by hand, NOT read back from       #
#     `codex_login_status`. A set derived from the code under test could only  #
#     ever confirm the code agrees with itself; it could not notice a new      #
#     wording the parser starts emitting, which is exactly the case that must  #
#     refuse. The list therefore includes values the parser does not currently #
#     produce ("", "apikey", "API_KEY").                                       #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("via", [
    "chatgpt", "unknown", "none", "", "apikey", "API_KEY", "api key", "oauth",
])
def test_the_gate_refuses_every_via_that_is_not_exactly_api_key(
    monkeypatch, tmp_path, via,
):
    monkeypatch.setattr(
        cx, "codex_login_status",
        lambda *a, **k: cx.CodexSessionStatus(present=True, via=via, detail="d"),
    )
    with pytest.raises(cx.CodexAuthError):
        cx.assert_api_key_billing_path("/bin/codex", tmp_path, timeout_s=1.0)


def test_the_gate_refuses_an_absent_session_even_when_via_says_api_key(
    monkeypatch, tmp_path,
):
    """`present=False` is how a timeout, a missing CLI, a permission error and
    a non-zero exit all arrive. Each must refuse exactly as a missing key
    does — never be read as 'no evidence of a problem'."""
    monkeypatch.setattr(
        cx, "codex_login_status",
        lambda *a, **k: cx.CodexSessionStatus(
            present=False, via="api_key", detail="timeout"),
    )
    with pytest.raises(cx.CodexAuthError):
        cx.assert_api_key_billing_path("/bin/codex", tmp_path, timeout_s=1.0)


def test_the_gate_admits_a_genuine_api_key_session(monkeypatch, tmp_path):
    """Positive control for section 14 — proves the eight refusals above are
    the gate discriminating, not the gate refusing everything."""
    monkeypatch.setattr(
        cx, "codex_login_status",
        lambda *a, **k: cx.CodexSessionStatus(
            present=True, via="api_key", detail="d"),
    )
    status = cx.assert_api_key_billing_path("/bin/codex", tmp_path, timeout_s=1.0)
    assert status.via == "api_key"


def test_the_gate_never_puts_the_probe_detail_in_the_refusal(
    monkeypatch, tmp_path,
):
    """`detail` can echo account identifiers, so it must not reach the
    message an operator sees or a log captures."""
    secret = "account-id-4815162342"
    monkeypatch.setattr(
        cx, "codex_login_status",
        lambda *a, **k: cx.CodexSessionStatus(
            present=True, via="chatgpt", detail=secret),
    )
    with pytest.raises(cx.CodexAuthError) as exc:
        cx.assert_api_key_billing_path("/bin/codex", tmp_path, timeout_s=1.0)
    assert secret not in str(exc.value)


# --------------------------------------------------------------------------- #
# 15. Two escapes an independent review found AFTER section 13 was written.    #
#     Both reached an inode outside the root through a name inside it, which   #
#     is the class a path-resolution check structurally cannot see.            #
# --------------------------------------------------------------------------- #

def test_a_hard_link_at_the_temp_path_cannot_capture_the_write(tmp_path):
    """`O_NOFOLLOW` did NOT cover this, though a comment claimed it did.

    A symlink at the temp path is rejected by `O_NOFOLLOW`; a HARD LINK is
    indistinguishable from the file itself, so the write landed in a victim
    outside the root — destroying it and copying the API key into it, which
    is strictly worse than the truncation the symlink case defends against,
    under the same precondition. Unlink-then-`O_EXCL` drops our name for the
    link before opening, so the victim keeps its own.
    """
    nh_root = tmp_path / ".no_human"
    home = nh_root / "codex-home"
    home.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_bytes(b"PRECIOUS")
    os.link(victim, home / f".{_CRED}.tmp")

    cx.materialise_api_key_auth("sk-ATTACKER-KEY", home, base=nh_root)

    assert victim.read_bytes() == b"PRECIOUS", (
        "a hard link at the temp path captured the write: the victim outside "
        "the root was overwritten")
    assert b"sk-ATTACKER-KEY" not in victim.read_bytes()
    assert json.loads((home / _CRED).read_text(encoding="utf-8"))["OPENAI_API_KEY"] == "sk-ATTACKER-KEY"


def test_codex_api_key_home_refuses_before_it_chmods_anything(tmp_path):
    """The guard has to run in the CALLER too.

    `codex_api_key_home` did `mkdir(exist_ok=True)` then `chmod(0o700)` with
    no check of its own. `mkdir` succeeds on an existing symlink and `chmod`
    FOLLOWS it, so a symlinked `codex-home` re-permissioned a directory
    outside the root a full call before the write-site guard could refuse —
    the same act-then-refuse shape, moved into the caller.

    The assertion is the victim directory's MODE, not the exception: raising
    after chmodding would still be a failure.

    ON ABLATION, so a reviewer does not re-derive it: `codex_api_key_home`
    carries TWO checks that each independently prevent this — the
    `is_symlink` loop and the pre-`mkdir` containment call. Removing EITHER
    alone leaves this test green, because the other still refuses; removing
    BOTH turns it red (measured). What is pinned here is the PROPERTY
    "nothing outside the root is mutated", not either mechanism. That is
    deliberate redundancy, not two uncovered ablations — but it does mean
    this test cannot tell you which check is load-bearing, and neither can
    the suite.
    """
    nh_root = tmp_path / ".no_human"
    nh_root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    outside.chmod(0o755)
    before = stat.S_IMODE(outside.stat().st_mode)
    (nh_root / "codex-home").symlink_to(outside, target_is_directory=True)

    with pytest.raises(cx.CodexAuthError):
        cx.codex_api_key_home(base=nh_root)

    assert stat.S_IMODE(outside.stat().st_mode) == before, (
        "codex_api_key_home chmod'd a directory outside no_human's root "
        "through a symlink, before any containment check ran")


def test_codex_api_key_home_returns_a_usable_home_when_nothing_is_planted(tmp_path):
    """Positive control for the two refusals above."""
    nh_root = tmp_path / ".no_human"
    home = cx.codex_api_key_home(base=nh_root)
    assert home == (nh_root / "codex-home").resolve()
    assert home.is_dir() and not home.is_symlink()
    assert oct(home.stat().st_mode)[-3:] == "700"


def test_the_root_itself_is_not_an_acceptable_credential_home(tmp_path):
    """`strictly inside` is now literally true.

    The check used to read `resolved != root and root not in resolved.parents`,
    which ADMITTED `home == root` while three docstrings said "strictly
    inside". Nothing pinned it in either direction, so the wording and the
    code could drift apart unnoticed. `codex_api_key_home` never returns the
    root, so admitting it was latitude with no caller — removed, and pinned
    here.
    """
    nh_root = tmp_path / ".no_human"
    nh_root.mkdir()
    with pytest.raises(cx.CodexAuthError):
        cx.materialise_api_key_auth("sk-x", nh_root, base=nh_root)
    assert not (nh_root / _CRED).exists()


def test_a_symlink_loop_refuses_as_CodexAuthError_not_as_RuntimeError(tmp_path):
    """The contract is CodexAuthError; a symlink LOOP used to escape untyped.

    `Path.resolve()` reports a loop as RuntimeError on CPython <=3.12, NOT as
    OSError, so the `except OSError` clause did not catch it and the caller saw
    a bare RuntimeError from a function whose docstring promises CodexAuthError.
    Fail-closed held either way — nothing was written — but the exception type
    is part of the contract, and an untyped escape is what callers cannot
    handle. This pins the TYPE, which is the whole defect.
    """
    nh_root = tmp_path / ".no_human"
    nh_root.mkdir()
    home = nh_root / "codex-home"
    loop = nh_root / "loop-partner"
    home.symlink_to(loop)
    loop.symlink_to(home)
    # Control: this really is a loop on this interpreter, not a missing path.
    with pytest.raises(RuntimeError):
        home.resolve()
    with pytest.raises(cx.CodexAuthError):
        cx.materialise_api_key_auth("sk-x", home, base=nh_root)
    assert not (nh_root / _CRED).exists()


# --------------------------------------------------------------------------- #
# 16. The three mechanisms in the temp-file open, pinned SEPARATELY.           #
#                                                                              #
#     A prior commit message claimed "O_EXCL reverted to O_TRUNC -> RED (2)".  #
#     An independent review re-ran it flag by flag and measured GREEN: the     #
#     mutation had ALSO dropped the `unlink`, and the unlink alone was         #
#     carrying the whole defence. `O_EXCL` and `O_NOFOLLOW` were pinned by     #
#     nothing while being asserted as pinned — the worst state for a security  #
#     mechanism, because the next refactor deletes it silently.                #
#                                                                              #
#     Each test below neutralises `unlink` so the FLAG is what is under test.  #
#     A no-op unlink is not artificial: it is precisely the state a lost       #
#     unlink->open race leaves behind.                                         #
# --------------------------------------------------------------------------- #

def _neuter_unlink(monkeypatch):
    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: None)


def test_O_EXCL_alone_stops_a_hard_link_when_the_unlink_is_lost(
    tmp_path, monkeypatch,
):
    """Isolates `O_EXCL`. With the unlink neutered, only `O_EXCL` stands
    between a planted hard link and the victim's bytes."""
    nh_root = tmp_path / ".no_human"
    home = nh_root / "codex-home"
    home.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_bytes(b"PRECIOUS")
    os.link(victim, home / f".{_CRED}.tmp")
    _neuter_unlink(monkeypatch)

    with pytest.raises(OSError):
        cx.materialise_api_key_auth("sk-ATTACKER", home, base=nh_root)

    assert victim.read_bytes() == b"PRECIOUS", (
        "O_EXCL did not stop the write when the unlink was lost")
    assert b"sk-ATTACKER" not in victim.read_bytes()


def test_a_symlink_at_the_temp_path_cannot_capture_the_write(
    tmp_path, monkeypatch,
):
    """Same construction as the hard-link test, for a symlink.

    NOT named for `O_NOFOLLOW`: measured, NEITHER flag carries this test alone.
    `O_CREAT|O_EXCL` raises `FileExistsError` on a symlink and `O_NOFOLLOW`
    raises `ELOOP`, so removing EITHER one alone leaves this test green and only
    removing BOTH turns it red. The test is carried by their disjunction, and
    neither flag is individually pinned here — said explicitly because a test
    name is a coverage claim, and an earlier name claimed coverage this test
    does not have.
    """
    nh_root = tmp_path / ".no_human"
    home = nh_root / "codex-home"
    home.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_bytes(b"PRECIOUS")
    (home / f".{_CRED}.tmp").symlink_to(victim)
    _neuter_unlink(monkeypatch)

    with pytest.raises(OSError):
        cx.materialise_api_key_auth("sk-ATTACKER", home, base=nh_root)

    assert victim.read_bytes() == b"PRECIOUS", (
        "O_NOFOLLOW did not stop the write when the unlink was lost")


def test_a_symlink_planted_at_close_cannot_capture_the_mode_change(
    tmp_path, monkeypatch,
):
    """Isolates `os.fchmod` from a path-based `os.chmod`.

    The previous version of this test planted NO symlink and asserted a victim
    it could not reach — structurally incapable of failing on the mechanism its
    name claimed. An independent review measured the single-mechanism mutation
    (`os.fchmod(fh.fileno(), ...)` -> `os.chmod(tmp, ...)` after close) as
    GREEN while the commit message asserted that row was RED.

    The window is real and this closes it deterministically rather than by
    racing: the file object's `close` is wrapped so the symlink is planted at
    exactly the moment the descriptor goes away. `fchmod` runs INSIDE the
    `with`, on the descriptor, so it is already done; a path-based `chmod`
    after the `with` would follow the freshly planted link and re-permission a
    file outside the root.
    """
    nh_root = tmp_path / ".no_human"
    home = nh_root / "codex-home"
    home.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_bytes(b"PRECIOUS")
    victim.chmod(0o644)
    tmp = home / f".{_CRED}.tmp"

    real_fdopen = os.fdopen

    def planting_fdopen(fd, *a, **k):
        fh = real_fdopen(fd, *a, **k)
        real_close = fh.close

        def close_then_plant():
            real_close()
            tmp.unlink(missing_ok=True)
            tmp.symlink_to(victim)

        fh.close = close_then_plant
        return fh

    monkeypatch.setattr(os, "fdopen", planting_fdopen)

    try:
        cx.materialise_api_key_auth("sk-x", home, base=nh_root)
    except OSError:
        pass  # refusing outright is also an acceptable outcome

    assert oct(victim.stat().st_mode)[-3:] == "644", (
        "a symlink planted at close captured the mode change — the chmod "
        "followed a path instead of acting on the descriptor")
    assert victim.read_bytes() == b"PRECIOUS"


def test_the_open_mode_narrows_the_file_before_any_bytes_are_written(
    tmp_path, monkeypatch,
):
    """Pins the explicit `0o600` on `os.open`, which was load-bearing and
    unpinned while the commit message called it pinned.

    `fchmod` runs AFTER the payload is written, so without the explicit open
    mode the whole credential sits on disk at the umask default — measured at
    0755 under umask 022 — for the duration of the write. Asserting the final
    mode cannot see that; this asserts the mode observed at the moment
    `fchmod` is called, which is the last instant the earlier window is still
    visible.
    """
    nh_root = tmp_path / ".no_human"
    home = nh_root / "codex-home"
    home.mkdir(parents=True)
    seen: dict[str, int] = {}
    real_fchmod = os.fchmod

    def spy(fd, mode):
        seen["before"] = stat.S_IMODE(os.fstat(fd).st_mode)
        return real_fchmod(fd, mode)

    monkeypatch.setattr(os, "fchmod", spy)
    previous = os.umask(0o022)
    try:
        cx.materialise_api_key_auth("sk-x", home, base=nh_root)
    finally:
        os.umask(previous)

    assert seen.get("before") == 0o600, (
        f"the credential was written into a {oct(seen.get('before', 0))} file "
        "and only narrowed afterwards — the explicit mode on os.open is what "
        "prevents that window")


# --------------------------------------------------------------------------- #
# 17. A RELOCATED STORE still works.                                           #
#                                                                              #
#     `config.ensure_private_dir` documents NO_HUMAN_HOME itself being a       #
#     symlink — "the operator relocated the store to another disk" — warns and #
#     continues. Symlinking is the ONLY relocation mechanism, because          #
#     NO_HUMAN_HOME is hard-coded. A revision of this file refused a symlinked #
#     root outright, which made api_key mode UNUSABLE for such an operator and #
#     shipped with no test and no mention. This is the positive control whose  #
#     absence let that through.                                               #
# --------------------------------------------------------------------------- #

def test_a_relocated_store_root_is_accepted_not_refused(tmp_path):
    real = tmp_path / "other-disk"
    real.mkdir()
    nh_root = tmp_path / ".no_human"
    nh_root.symlink_to(real, target_is_directory=True)

    home = cx.codex_api_key_home(base=nh_root)
    cx.materialise_api_key_auth("sk-relocated", home, base=nh_root)

    assert json.loads((home / _CRED).read_text(encoding="utf-8"))["OPENAI_API_KEY"] == "sk-relocated"
    assert (real / "codex-home" / _CRED).exists(), (
        "the credential did not land inside the relocated store")
    # Discriminates `return resolved` from `return home`, which the section-15
    # control could NOT: there the expected value was built with the same
    # `resolve()` the code uses, on an already-canonical tmp_path, so both
    # spellings satisfied it. Here the root IS a symlink, so the two differ.
    assert home == home.resolve(), "codex_api_key_home returned an unresolved path"
    assert home != (nh_root / "codex-home"), (
        "the returned path still goes through the symlink — the check and the "
        "write would name different directories")


def test_the_credential_is_0600_even_under_a_hostile_umask(tmp_path):
    """Pins `fchmod`. `os.open(..., 0o600)` is a REQUEST: umask can only
    remove bits from it, so a process umask of 0o377 yields 0400 and the file
    is not even writable by its owner. Measured, not assumed — that is why the
    mode is set explicitly after the write rather than left to the open.

    The umask is process-global; it is restored in `finally` so a failure here
    cannot leak into another test.
    """
    nh_root = tmp_path / ".no_human"
    home = nh_root / "codex-home"
    home.mkdir(parents=True)
    previous = os.umask(0o377)
    try:
        cx.materialise_api_key_auth("sk-x", home, base=nh_root)
    finally:
        os.umask(previous)
    assert oct((home / _CRED).stat().st_mode)[-3:] == "600", (
        "the umask stripped the credential's mode and nothing restored it")


def test_a_stale_temp_file_from_a_crashed_run_does_not_wedge_the_write(tmp_path):
    """The ORDINARY operational case, which had no test.

    `O_EXCL` refuses to open anything that already exists, so without the
    unlink a leftover temp from a crashed or killed run would make api_key
    mode fail permanently until someone deleted the file by hand. The unlink
    is what keeps `O_EXCL` safe to use here, and this is the test that says so.
    """
    nh_root = tmp_path / ".no_human"
    home = nh_root / "codex-home"
    home.mkdir(parents=True)
    (home / f".{_CRED}.tmp").write_bytes(b"leftover from a crashed run")

    cx.materialise_api_key_auth("sk-after-crash", home, base=nh_root)

    assert json.loads((home / _CRED).read_text(encoding="utf-8"))["OPENAI_API_KEY"] == "sk-after-crash"
    assert not (home / f".{_CRED}.tmp").exists(), "the temp file was left behind"


def test_rewriting_the_same_key_is_byte_identical_and_a_rotation_replaces_it(
    tmp_path,
):
    """`materialise_api_key_auth`'s docstring claims idempotence and that a
    rotated key simply overwrites. Neither was tested; the word "idempotent"
    appeared nowhere in this file."""
    nh_root = tmp_path / ".no_human"
    home = nh_root / "codex-home"
    home.mkdir(parents=True)
    cred = home / _CRED

    cx.materialise_api_key_auth("sk-one", home, base=nh_root)
    first = cred.read_bytes()
    cx.materialise_api_key_auth("sk-one", home, base=nh_root)
    assert cred.read_bytes() == first, "re-writing the same key was not idempotent"

    cx.materialise_api_key_auth("sk-two", home, base=nh_root)
    assert json.loads(cred.read_text(encoding="utf-8"))["OPENAI_API_KEY"] == "sk-two"
    assert oct(cred.stat().st_mode)[-3:] == "600", "the rotation lost the mode"


def test_codex_child_env_drops_the_launchers_ambient_secrets_api_key_mode(monkeypatch):
    """The Codex child env is a FULL copy of the launcher's environment, so the
    same secret-shaped names the Claude child never sees (agent/child_env.py)
    are deleted here — while the OpenAI key this mode bills on, the mark and
    the operational vars survive."""
    from no_human.agent.session_mark import NO_HUMAN_AGENT_SESSION

    _stub_cli(monkeypatch)
    env = cx.CodexBackend(env={
        **FAKE_ENV,
        "GITHUB_TOKEN": "ghp_not_real", "AWS_SECRET_ACCESS_KEY": "aws_not_real",
        "AWS_PROFILE": "work", "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "JIRA_API_TOKEN": "jira_not_real", "HOME": "/home/x",
    })._child_env()
    for gone in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE",
                 "SSH_AUTH_SOCK", "JIRA_API_TOKEN"):
        assert gone not in env, gone
    assert env["OPENAI_API_KEY"] == "not-a-real-key"
    assert env["PATH"] == FAKE_ENV["PATH"]
    assert env["HOME"] == "/home/x"
    assert env[NO_HUMAN_AGENT_SESSION] == "1"


def test_codex_child_env_drops_the_launchers_ambient_secrets_subscription_mode(monkeypatch):
    from no_human.agent.session_mark import mark_env

    monkeypatch.setattr(cx, "codex_login_status",
                        lambda cli_path=None: cx.CodexSessionStatus(True, "chatgpt"))
    env = cx.CodexBackend(auth_mode="subscription", env={
        "PATH": "/bin", "GITHUB_TOKEN": "ghp_not_real", "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "OPENAI_API_KEY": "not-a-real-key",
    })._child_env()
    assert "GITHUB_TOKEN" not in env
    assert "SSH_AUTH_SOCK" not in env
    assert "OPENAI_API_KEY" not in env  # subscription mode holds no OpenAI credential
    assert env == {"PATH": "/bin", **mark_env("codex")}
