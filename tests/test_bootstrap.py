"""Tests for scripts/bootstrap.sh: dry-run shape, config preservation,
missing-tool messaging, and idempotency. Shells out to the real script with a
hermetic fake PATH + temp HOME so nothing touches the real ~/.no_human/."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "bootstrap.sh"
TEMPLATE = REPO_ROOT / "scripts" / "config.yaml.template"
# NOTE: the script always `cd`s to the repo root before doing anything, so
# any .venv it touches lands at REPO_ROOT/.venv — NOT under the fake HOME
# used by fake_env. This repo checkout already has a real .venv (created by
# `uv sync`/`uv run` outside of these tests), so its mere *existence* can't
# be asserted on; step-banner absence ("[2/5]" not in stdout) is what proves
# the venv-touching step never ran.


def _write_stub(bin_dir: Path, name: str, output: str = "") -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\necho '{output}'\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


# The tools these tests stub, add, and delete. Whether one of them is on PATH
# is the entire subject of the missing-tool tests, so the host's copies must
# never be reachable — see system_bin.
STUBBED_TOOLS = ("python3", "uv", "claude", "nh")


@pytest.fixture(scope="session")
def system_bin(tmp_path_factory):
    """A stand-in for /usr/bin:/bin that cannot leak a stubbed tool.

    bootstrap.sh needs ordinary system utilities (dirname, awk, cut, cp,
    basename, git...), so the fake PATH cannot be the stub bin/ alone. But
    putting the real /usr/bin on it broke what these tests mean by "missing":
    deleting the stub stopped implying the tool was absent, because the host
    still had one. Ubuntu ships /usr/bin/python3 at 3.12, which quietly
    satisfied the very check test_missing_python_without_uv_* exists to
    exercise; that test passed on macOS only because /usr/bin/python3 there is
    3.9, too old to satisfy it. Nothing about the script was wrong — the
    precondition was never established on Linux.

    So mirror the system directories one symlink at a time and drop every name
    the fixture stubs. Absence is then absence on any host. Symlinking rather
    than listing an allowlist keeps this working when the script (or the real
    `nh doctor` it runs at step 5) reaches for another utility.
    """
    mirror = tmp_path_factory.mktemp("system-bin")
    for src_dir in (Path("/usr/bin"), Path("/bin")):
        if not src_dir.is_dir():
            continue
        for entry in src_dir.iterdir():
            # python3.12, python3.13, ... would satisfy nothing here (the
            # script only calls `python3`), but drop the whole family so the
            # rule is "no interpreter the fixture did not put there".
            if entry.name in STUBBED_TOOLS or entry.name.startswith("python"):
                continue
            link = mirror / entry.name
            if link.exists() or link.is_symlink():
                continue  # /bin is a symlink to /usr/bin on merged-usr Linux
            link.symlink_to(entry)
    return mirror


@pytest.fixture
def fake_env(tmp_path, system_bin):
    """A temp bin/ with stub python3/uv/claude/nh, plus isolated HOME."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "python3", "Python 3.12.4")
    _write_stub(bin_dir, "uv", "uv 0.4.0")
    _write_stub(bin_dir, "claude", "1.0.0")
    _write_stub(bin_dir, "nh", "nh doctor: ok")

    home = tmp_path / "home"
    home.mkdir()
    nh_home = home / ".no_human"

    env = {
        "PATH": f"{bin_dir}:{system_bin}",
        "HOME": str(home),
        "NO_HUMAN_HOME": str(nh_home),
    }
    return {"env": env, "bin_dir": bin_dir, "home": home, "nh_home": nh_home}


def _which(fake_env, tool: str) -> str | None:
    """Resolve `tool` exactly the way bootstrap.sh's `command -v` does."""
    result = subprocess.run(
        ["/bin/sh", "-c", f"command -v {tool}"],
        capture_output=True, text=True, timeout=30, env=fake_env["env"],
    )
    return result.stdout.strip() or None


def _run(fake_env, extra_args=None):
    args = [str(SCRIPT)] + (extra_args or [])
    return subprocess.run(
        args, capture_output=True, text=True, timeout=30, env=fake_env["env"],
        cwd=str(REPO_ROOT),
    )


def test_dry_run_output_shape(fake_env):
    result = _run(fake_env, ["--dry-run"])
    assert result.returncode == 0, result.stderr
    out = result.stdout
    for n in range(1, 6):
        assert f"[{n}/5]" in out, f"missing step banner [{n}/5]:\n{out}"
    order = [out.index(f"[{n}/5]") for n in range(1, 6)]
    assert order == sorted(order), "step banners out of order"
    assert "DRY-RUN" in out
    assert not fake_env["nh_home"].exists(), "--dry-run must not create ~/.no_human"


def test_dry_run_with_existing_config_does_not_touch_it(fake_env):
    fake_env["nh_home"].mkdir(parents=True)
    config_path = fake_env["nh_home"] / "config.yaml"
    sentinel = "# sentinel: do not touch\nfoo: bar\n"
    config_path.write_text(sentinel)

    result = _run(fake_env, ["--dry-run"])
    assert result.returncode == 0, result.stderr
    assert config_path.read_text(encoding="utf-8") == sentinel
    assert "leaving untouched" in result.stdout
    assert "DRY-RUN: would run: cp" not in result.stdout


def test_existing_config_untouched(fake_env):
    fake_env["nh_home"].mkdir(parents=True)
    config_path = fake_env["nh_home"] / "config.yaml"
    sentinel = "# sentinel: do not touch\nfoo: bar\n"
    config_path.write_text(sentinel)

    result = _run(fake_env)
    assert result.returncode == 0, result.stderr
    assert config_path.read_text(encoding="utf-8") == sentinel
    assert "leaving untouched" in result.stdout


def test_template_created_when_absent(fake_env):
    result = _run(fake_env)
    assert result.returncode == 0, result.stderr
    config_path = fake_env["nh_home"] / "config.yaml"
    assert config_path.exists()
    assert config_path.read_text(encoding="utf-8") == TEMPLATE.read_text(encoding="utf-8")


def test_fake_path_resolves_tools_only_from_the_stub_bin(fake_env):
    """Deleting a stub has to mean the tool is gone. It did not: the fake PATH
    ended in /usr/bin:/bin, so the host's python3/uv/git-shipped claude were
    still one lookup away, and a missing-tool test silently tested nothing on
    any machine that had the real thing. This is the guard for that."""
    for tool in STUBBED_TOOLS:
        assert _which(fake_env, tool) == str(fake_env["bin_dir"] / tool)
        (fake_env["bin_dir"] / tool).unlink()
        assert _which(fake_env, tool) is None, (
            f"{tool} is still on PATH after its stub was deleted — the host's "
            f"copy leaked through, so 'missing {tool}' tests prove nothing"
        )
    # ...and the utilities the script genuinely needs are still reachable.
    for util in ("dirname", "awk", "cut", "cp", "mkdir", "cat", "basename"):
        assert _which(fake_env, util) is not None, util


def test_missing_uv_prints_instructions_and_exits_nonzero(fake_env):
    (fake_env["bin_dir"] / "uv").unlink()
    assert _which(fake_env, "uv") is None, "precondition: uv must be off PATH"
    result = _run(fake_env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "astral.sh/uv/install.sh" in result.stdout
    assert "[2/5]" not in result.stdout


def test_missing_python_without_uv_prints_instructions_and_exits_nonzero(fake_env):
    (fake_env["bin_dir"] / "python3").unlink()
    (fake_env["bin_dir"] / "uv").unlink()
    # Assert the precondition, don't assume it. Without this the test passed on
    # macOS and failed on Linux purely because of which python3 the host had in
    # /usr/bin, and the failure looked like a bug in the script's messaging.
    assert _which(fake_env, "python3") is None, "precondition: no python3 on PATH"
    assert _which(fake_env, "uv") is None, "precondition: no uv on PATH"
    result = _run(fake_env)
    assert result.returncode == 1, result.stdout + result.stderr
    # The uv guidance is not a substitute: with no interpreter and no uv to
    # provision one, the script has to say where Python itself comes from.
    assert "Install Python 3.12+" in result.stdout
    assert "python.org" in result.stdout or "brew install python@3.12" in result.stdout
    assert "[2/5]" not in result.stdout


def test_old_system_python_with_uv_available_proceeds(fake_env):
    # Stock-Mac case: system python3 is 3.10, but uv is present and can find
    # or provision 3.12 — the script must NOT refuse (send-back finding #2).
    _write_stub(fake_env["bin_dir"], "python3", "Python 3.10.9")
    result = _run(fake_env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3.10" in result.stdout
    assert "[2/5]" in result.stdout
    assert "[5/5]" in result.stdout
    assert "Next steps:" in result.stdout


def test_missing_claude_cli_prints_pointer_and_exits_nonzero(fake_env):
    (fake_env["bin_dir"] / "claude").unlink()
    result = _run(fake_env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "npm install -g @anthropic-ai/claude-code" in result.stdout
    assert "claude.com/claude-code" in result.stdout
    assert "[4/5]" not in result.stdout


def test_idempotent_rerun(fake_env):
    first = _run(fake_env)
    assert first.returncode == 0, first.stderr
    config_path = fake_env["nh_home"] / "config.yaml"
    first_bytes = config_path.read_bytes()

    second = _run(fake_env)
    assert second.returncode == 0, second.stderr
    assert "leaving untouched" in second.stdout
    assert config_path.read_bytes() == first_bytes


# --------------------------------------------------------------------------- #
# The template is an OVERLAY, not a copy of the defaults.                       #
#                                                                              #
# bootstrap.sh copies scripts/config.yaml.template to ~/.no_human/config.yaml   #
# and then NEVER overwrites it ("config.yaml exists — leaving untouched"), and  #
# load_config deep-merges that file OVER DEFAULT_CONFIG. So any model pinned in #
# the template WINS on every bootstrapped install, permanently.                 #
#                                                                              #
# That makes a stale model id in the template invisible here (this repo has no  #
# ~/.no_human/config.yaml checked in) while silently pinning every NEW install  #
# to the old model. An independent review caught exactly that: the Opus tier    #
# was moved to claude-opus-5 everywhere except the template, so bootstrapped    #
# installs would have kept running the previous reviewer forever.               #
#                                                                              #
# test_template_created_when_absent asserts only `copy == template`, which is   #
# tautological with respect to CONTENT — it passes no matter what the template  #
# says. This test is the content guard it lacks.                                #
# --------------------------------------------------------------------------- #
def test_template_model_ids_match_the_shipped_defaults():
    """Every llm.*_model the template pins must equal DEFAULT_CONFIG's value."""
    import yaml

    from no_human.config import DEFAULT_CONFIG

    template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8")) or {}
    pinned = {k: v for k, v in (template.get("llm") or {}).items()
              if k.endswith("_model")}
    # Guard the guard: if the template stops pinning models entirely this test
    # would vacuously pass, so require that it still pins the two that matter.
    assert {"primary_model", "review_model"} <= set(pinned), (
        f"template no longer pins the core model tiers: {sorted(pinned)} — "
        f"this test would silently stop protecting anything"
    )
    defaults = DEFAULT_CONFIG["llm"]
    drifted = {k: (v, defaults.get(k)) for k, v in pinned.items()
               if v != defaults.get(k)}
    assert not drifted, (
        "scripts/config.yaml.template pins model ids that no longer match "
        f"DEFAULT_CONFIG (template, default): {drifted} — every bootstrapped "
        "install would keep running the stale model, permanently"
    )
