"""`nh onboard` recon: deterministic derivation, agentic-deriver parsing, and
REAL end-to-end proving on two different ecosystems (Python+pytest and Node)
with no code path differing between them — the Phase-4 DoD."""

import io
import json
import shutil

import pytest
from rich.console import Console

from no_human.onboard import (
    AgentDeriver,
    DeclarationDeriver,
    OnboardEngine,
    ProveOutcome,
)
from no_human.profile import PROFILE_RELPATH, ProjectProfile

# --------------------------------------------------------------------------- #
# fixtures: tiny but real repos in tmp dirs                                    #
# --------------------------------------------------------------------------- #


def _python_repo(root, *, passing=True):
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.0.0'\n\n[tool.ruff]\nline-length = 100\n"
    )
    body = "def test_ok():\n    assert 1 + 1 == 2\n" if passing else \
           "def test_bad():\n    assert 1 + 1 == 3\n"
    (root / "test_demo.py").write_text(body)
    return root


def _node_repo(root):
    (root / "package.json").write_text(json.dumps({
        "name": "demo", "version": "0.0.0",
        "scripts": {"test": 'node -e "process.exit(0)"', "lint": 'node -e "process.exit(0)"'},
    }))
    return root


# --------------------------------------------------------------------------- #
# DeclarationDeriver — reads the repo's own declarations                       #
# --------------------------------------------------------------------------- #


def test_derive_python_pytest(tmp_path):
    d = DeclarationDeriver().derive(_python_repo(tmp_path))
    assert d.ecosystem == "python-pytest"
    tests = [c.command for c in d.of_kind("test")]
    assert tests == ["pytest -q"]            # no lockfile → plain pytest
    assert any(c.command == "ruff check ." for c in d.of_kind("lint"))


def test_derive_python_uv_lock(tmp_path):
    _python_repo(tmp_path)
    (tmp_path / "uv.lock").write_text("# lock\n")
    d = DeclarationDeriver().derive(tmp_path)
    assert [c.command for c in d.of_kind("install")] == ["uv sync"]
    assert [c.command for c in d.of_kind("test")] == ["uv run pytest -q"]
    assert any(c.command == "uv run ruff check ." for c in d.of_kind("lint"))


def test_derive_python_uv_lock_with_xdist_parallelizes(tmp_path):
    """The onboarded test_cmd is what the orchestrator actually runs — the
    profile overrides runner.detect_command — so the serial/parallel choice
    made HERE, at onboard time, is the one that governs every attempt. A
    serial derivation for an xdist-declaring repo re-creates the 2026-08-10
    zero-throughput incident on the next onboard."""
    _python_repo(tmp_path)
    (tmp_path / "uv.lock").write_text('# lock\nname = "pytest-xdist"\n')
    d = DeclarationDeriver().derive(tmp_path)
    assert [c.command for c in d.of_kind("test")] == ["uv run pytest -q -n 4"]


def test_derive_node_scripts(tmp_path):
    d = DeclarationDeriver().derive(_node_repo(tmp_path))
    assert d.ecosystem == "node"
    assert [c.command for c in d.of_kind("install")] == ["npm install"]
    assert [c.command for c in d.of_kind("test")] == ["npm test"]
    assert d.of_kind("test")[0].source == "package.json:scripts.test"


def test_derive_node_lockfile_npm_ci(tmp_path):
    _node_repo(tmp_path)
    (tmp_path / "package-lock.json").write_text("{}")
    d = DeclarationDeriver().derive(tmp_path)
    assert [c.command for c in d.of_kind("install")] == ["npm ci"]


def test_derive_ci_and_human_gates(tmp_path):
    _python_repo(tmp_path)
    (tmp_path / ".gitlab-ci.yml").write_text("stages: [test]\n")
    (tmp_path / "Jenkinsfile").write_text("pipeline {}\n")
    d = DeclarationDeriver().derive(tmp_path)
    assert d.ci == {"backend": "gitlab"}
    assert any("Jenkins" in s for s in d.human_gated_steps)


def test_derive_makefile_fallback(tmp_path):
    # A repo whose only declaration is a Makefile with a test target.
    (tmp_path / "Makefile").write_text("test:\n\techo hi\ninstall:\n\techo dep\n")
    d = DeclarationDeriver().derive(tmp_path)
    assert d.ecosystem == "make"
    assert [c.command for c in d.of_kind("test")] == ["make test"]


# --------------------------------------------------------------------------- #
# AgentDeriver — parses a fenced JSON block, never proves                      #
# --------------------------------------------------------------------------- #


class _FakeBackend:
    def __init__(self, text):
        self._text = text

    async def run(self, prompt, *, cwd, max_turns, effort=None, **kw):
        class _R:
            final_text = self._text
        return _R()


def test_agent_deriver_parses_json_block():
    blob = (
        "Here is what I found:\n```json\n"
        + json.dumps({
            "ecosystem": "rust",
            "ci": {"backend": "github_actions"},
            "human_gated_steps": ["release gated"],
            "candidates": [
                {"kind": "test", "command": "cargo test", "source": "Cargo.toml"},
                {"kind": "bogus", "command": "x", "source": "y"},   # dropped
            ],
        })
        + "\n```\n"
    )
    d = AgentDeriver.parse(blob)
    assert d.ecosystem == "rust"
    assert [c.command for c in d.candidates] == ["cargo test"]   # bogus kind dropped
    assert d.ci == {"backend": "github_actions"}


def test_agent_deriver_no_block_is_empty():
    assert AgentDeriver.parse("no json here").candidates == []


@pytest.mark.asyncio
async def test_agent_deriver_runs_readonly_backend(tmp_path):
    backend = _FakeBackend('```json\n{"candidates": [{"kind": "test", '
                           '"command": "make check", "source": "Makefile"}]}\n```')
    d = await AgentDeriver(backend).derive(tmp_path)
    assert [c.command for c in d.candidates] == ["make check"]


# --------------------------------------------------------------------------- #
# OnboardEngine — DoD: prove TWO ecosystems with the SAME code path            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_onboard_python_pytest_end_to_end(tmp_path):
    repo = _python_repo(tmp_path)
    result = await OnboardEngine().onboard(repo)
    prof = result.profile
    assert prof.ecosystem == "python-pytest"
    assert prof.test_cmd == "pytest -q"
    assert prof.proven.get("test_cmd") is True          # actually ran, exit 0
    # not usable until a human confirms — proof alone is not trust.
    assert prof.is_usable is False
    prof.confirmed = True
    assert prof.is_usable is True
    assert any(p.kind == "test" and p.ok for p in result.proofs)


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
@pytest.mark.asyncio
async def test_onboard_node_end_to_end(tmp_path):
    repo = _node_repo(tmp_path)
    result = await OnboardEngine().onboard(repo)
    prof = result.profile
    assert prof.ecosystem == "node"
    assert prof.test_cmd == "npm test"
    assert prof.proven.get("test_cmd") is True
    assert prof.install_cmd == "npm install"
    assert prof.proven.get("install_cmd") is True


@pytest.mark.asyncio
async def test_onboard_does_not_fake_a_failing_test(tmp_path):
    # A repo whose test FAILS must not be marked proven — no faking.
    repo = _python_repo(tmp_path, passing=False)
    result = await OnboardEngine().onboard(repo)
    prof = result.profile
    assert prof.proven.get("test_cmd") is not True
    assert prof.is_usable is False
    assert any(p.kind == "test" and not p.ok for p in result.proofs)


@pytest.mark.asyncio
async def test_onboard_writes_yaml_and_round_trips(tmp_path):
    repo = _python_repo(tmp_path)
    result = await OnboardEngine().onboard(repo)
    path = result.profile.save()
    assert path == tmp_path / PROFILE_RELPATH
    loaded = ProjectProfile.load(tmp_path)
    assert loaded.test_cmd == "pytest -q"
    assert loaded.proven.get("test_cmd") is True


# --------------------------------------------------------------------------- #
# A failed proving candidate must say WHY (KI-4)                               #
# --------------------------------------------------------------------------- #


def _failed(output: str) -> ProveOutcome:
    return ProveOutcome("test", "pytest -q", False, 1, output, "pyproject.toml")


def test_a_passing_candidate_stays_quiet():
    """A clean prove run must read exactly as it did before this existed."""
    passed = ProveOutcome("test", "pytest -q", True, 0, "42 passed", "pyproject.toml")
    assert passed.failure_tail() == ""


def test_a_failed_candidate_that_said_nothing_stays_quiet():
    assert _failed("").failure_tail() == ""
    assert _failed("   \n\n  ").failure_tail() == ""


def test_a_failed_candidate_shows_the_reason():
    tail = _failed("E   ModuleNotFoundError: No module named 'psycopg2'").failure_tail()
    assert "ModuleNotFoundError" in tail
    assert "psycopg2" in tail


def test_the_output_is_bounded_by_line_count():
    """A 10,000-line pytest failure must not flood the terminal."""
    tail = _failed("\n".join(f"line {i}" for i in range(10_000))).failure_tail()
    rendered = [ln for ln in tail.splitlines() if ln.strip()]
    assert len(rendered) == 13                    # 12 lines + the elision note
    assert "9988 earlier line(s) not shown" in tail
    assert "line 9999" in tail                    # the tail, which is the useful end
    assert "line 0" not in tail


def test_the_output_is_bounded_by_line_width():
    """One enormous line floods just as effectively as ten thousand short ones."""
    tail = _failed("x" * 5_000).failure_tail()
    assert len(tail) < 400
    assert "(+4800 chars)" in tail


def test_what_is_dropped_is_announced_not_silently_cut():
    """Silently truncating a diagnostic is its own trap: the reader cannot tell
    a short failure from a clipped one."""
    assert "not shown" not in _failed("one\ntwo").failure_tail()
    assert "not shown" in _failed("\n".join(str(i) for i in range(50))).failure_tail()


def test_command_output_cannot_inject_console_markup():
    """The output is the command's, not ours. An unclosed Rich tag in a
    traceback would otherwise be swallowed or would raise."""
    tail = _failed("got [red]unclosed and [/] stray").failure_tail()
    assert r"\[red]" in tail
    assert r"\[/]" in tail


def test_a_line_ending_in_a_backslash_keeps_its_closing_tag():
    """A Windows path at the end of a traceback line ends in a backslash. Escaping
    only `[` would let that backslash escape our own closing tag, so the line lost
    its last character and printed a literal `[/]`."""
    tail = _failed("cannot open C:\\Users\\dev\\").failure_tail()
    assert tail.rstrip().endswith("[/]")

    console = Console(file=io.StringIO(), width=200, force_terminal=False)
    console.print(tail.strip())
    rendered = console.file.getvalue()
    assert "[/]" not in rendered
    assert rendered.rstrip().endswith("\\")


def test_a_credential_in_the_command_output_is_masked(monkeypatch):
    """A proved command can print a credential of its own. `output` is the
    command's, not the environment's, but it still must not reach a terminal."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-super-secret-value")
    tail = _failed(
        "config dump: ANTHROPIC_API_KEY=sk-ant-super-secret-value\nfailed"
    ).failure_tail()
    assert "sk-ant-super-secret-value" not in tail
    assert "failed" in tail


def test_nothing_from_the_environment_is_rendered(monkeypatch):
    """The proving subprocess inherits the process env, which by then holds the
    values loaded from ~/.no_human/.env. Only `output` may ever be printed."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-never-be-rendered")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-must-never-be-rendered")
    tail = _failed("E   assert 1 == 2").failure_tail()
    assert "must-never-be-rendered" not in tail
    assert tail.strip() == "[dim]E   assert 1 == 2[/]"


@pytest.mark.asyncio
async def test_onboard_end_to_end_surfaces_a_real_failure_reason(tmp_path):
    """The whole point, through the real engine: a missing dependency has to be
    nameable from the onboard output alone."""
    repo = _python_repo(tmp_path)
    (repo / "test_demo.py").write_text(
        "import nh_no_such_module_9f3a  # noqa: F401\n\n\ndef test_ok():\n    assert True\n"
    )
    result = await OnboardEngine().onboard(repo)

    failed = [p for p in result.proofs if p.kind == "test" and not p.ok]
    assert failed, "the fixture repo's test command was supposed to fail"
    tail = failed[0].failure_tail()
    assert "nh_no_such_module_9f3a" in tail, (
        "the reason the command failed is not recoverable from the output"
    )
