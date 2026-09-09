"""Tests for the deterministic scope & dependency guards (Phase 5e)."""

import textwrap
from pathlib import Path

import pytest

from no_human.agent.scope_guard import (
    SCRATCH_DIR,
    ScopeGuardHook,
    check_dependency_diff,
    check_forbidden_imports,
    check_scope,
    commit_time_checks,
    is_agent_owned,
    is_outside_repo,
    parse_plan_files,
    scratch_redirect,
)


# ── parse_plan_files ──────────────────────────────────────────────────── #


def test_parse_plan_files_basic():
    plan = textwrap.dedent("""\
        ## FILES TO CHANGE/CREATE
        - `src/no_human/core/orchestrator.py` — add scope guard wiring
        - `src/no_human/agent/scope_guard.py` — new module
        - tests/test_scope_guard.py — tests

        ## TEST PLAN
        ...
    """)
    result = parse_plan_files(plan)
    assert "src/no_human/core/orchestrator.py" in result
    assert "src/no_human/agent/scope_guard.py" in result
    assert "tests/test_scope_guard.py" in result


def test_parse_plan_files_no_section():
    assert parse_plan_files("no plan here") == set()


def test_parse_plan_files_empty_section():
    plan = "## FILES TO CHANGE/CREATE\n\n## TEST PLAN\n"
    assert parse_plan_files(plan) == set()


def test_parse_plan_files_table_and_extensionless():
    """Regression: the planner sometimes emits this section as a markdown
    table, and extension-less filenames (Jenkinsfile, Makefile) must not be
    dropped — this was silently missing `Jenkinsfile` entirely, causing
    every legitimate edit to it to fire a false [SCOPE] warning."""
    plan = textwrap.dedent("""\
        ## FILES TO CHANGE/CREATE

        | File | Change |
        |------|--------|
        | `Jenkinsfile` | Add integration test stage |
        | `.no_human/project.yml` | Add required credential |

        ---

        ## TEST PLAN
        ...
    """)
    result = parse_plan_files(plan)
    assert result == {"Jenkinsfile", ".no_human/project.yml"}


# ── check_scope ───────────────────────────────────────────────────────── #


def test_check_scope_in_plan():
    plan_files = {"src/foo.py", "src/bar.py"}
    assert check_scope("src/foo.py", plan_files) is None


def test_check_scope_out_of_plan():
    plan_files = {"src/foo.py"}
    result = check_scope("src/other.py", plan_files)
    assert result is not None
    assert "[SCOPE]" in result
    assert "other.py" in result


def test_check_scope_empty_plan():
    assert check_scope("src/foo.py", set()) is None


def test_check_scope_relative_path():
    plan_files = {"src/foo.py"}
    assert check_scope("./src/foo.py", plan_files) is None


def test_check_scope_basename_match():
    """Fuzzy: if basename matches a plan file, allow it."""
    plan_files = {"src/core/foo.py"}
    assert check_scope("/repo/src/core/foo.py", plan_files, "/repo") is None


# ── check_forbidden_imports ───────────────────────────────────────────── #


def test_check_forbidden_imports(tmp_path):
    py_file = tmp_path / "bad.py"
    py_file.write_text("import torch\nimport os\n")
    result = check_forbidden_imports([py_file])
    assert len(result) == 1
    assert result[0][2] == "import torch"


def test_check_forbidden_imports_clean(tmp_path):
    py_file = tmp_path / "good.py"
    py_file.write_text("import os\nimport json\n")
    assert check_forbidden_imports([py_file]) == []


def test_check_forbidden_imports_skips_non_python(tmp_path):
    js_file = tmp_path / "app.js"
    js_file.write_text("import torch from 'torch';\n")
    assert check_forbidden_imports([js_file]) == []


# ── agent-owned paths (D18) ───────────────────────────────────────────── #


def test_agent_owned_dirs_are_all_excluded_from_every_git_diff():
    """Both guards rest on one premise: nothing in these dirs can reach a commit.
    That is enforced by `vcs/git.py::_EPHEMERAL`, a separate hand-maintained list.
    Deriving AGENT_OWNED_DIRS from it is not safe (it also excludes `.env`, and a
    bug there would commit a secret), so lock the two together with a test: add a
    dir to _EPHEMERAL without adding it here and edits to it start counting as
    scope violations and edit-loops again — the bug that killed task 61406d02."""
    from no_human.agent.scope_guard import AGENT_OWNED_DIRS, AGENT_OWNED_FILES
    from no_human.vcs.git import GitRepo

    excluded = " ".join(GitRepo._EPHEMERAL)
    for d in AGENT_OWNED_DIRS:
        assert f"**/{d}/**" in excluded, f"{d!r} is not excluded by _EPHEMERAL"
    for f in AGENT_OWNED_FILES:
        assert f"**/{f}" in excluded, f"{f!r} is not excluded by _EPHEMERAL"


def test_agent_authored_files_are_never_scope_violations():
    """`_EPHEMERAL` drops `**/PLAN.md` and `**/HANDOVER.md` from every diff, so a
    coder that writes one gets a scope warning for a file it can never commit —
    the D18 shape, one directory over."""
    plan_files = {"src/foo.py"}
    assert check_scope("PLAN.md", plan_files) is None
    assert check_scope("docs/HANDOVER.md", plan_files) is None
    assert is_agent_owned("/repo/PLAN.md", "/repo")
    nudge = scratch_redirect("PLAN.md")
    assert nudge is not None and SCRATCH_DIR in nudge
    # …and an ordinary markdown file is still policed.
    assert check_scope("docs/README.md", plan_files) is not None


def test_is_agent_owned_relative_and_absolute():
    assert is_agent_owned(".no_human/draft.groovy")
    assert is_agent_owned("/repo/.no_human/draft.groovy")
    assert is_agent_owned("/repo/.claude/settings.json")
    assert not is_agent_owned("/repo/src/foo.py")


def test_is_agent_owned_relative_to_repo_root():
    """A repo living under an agent-owned dir must not read as wholly owned."""
    assert not is_agent_owned("/home/u/.claude/repo/src/foo.py", "/home/u/.claude/repo")


def test_check_scope_exempts_agent_owned_dirs():
    """D18: the coder drafted in `.no_human/` and got '[SCOPE] … revert' on every
    write. `.no_human/` never reaches a diff, so it cannot be out of scope."""
    plan_files = {"src/foo.py"}
    assert check_scope(".no_human/ci_gate_stage_draft.groovy", plan_files) is None
    assert check_scope("/repo/.no_human/draft.groovy", plan_files, "/repo") is None
    # …while a genuinely unplanned source file is still flagged.
    assert check_scope("src/other.py", plan_files) is not None


def test_scratch_redirect_points_at_the_scratch_dir_and_never_says_revert():
    msg = scratch_redirect("/repo/.no_human/draft.groovy", "/repo")
    assert msg is not None
    assert SCRATCH_DIR in msg
    assert "revert" not in msg.lower()


def test_scratch_redirect_silent_inside_the_scratch_dir():
    assert scratch_redirect(f"{SCRATCH_DIR}/notes.md") is None
    assert scratch_redirect(f"/repo/{SCRATCH_DIR}/notes.md", "/repo") is None


def test_scratch_redirect_silent_on_the_repro_manifest():
    """The implement prompt tells the coder to write the repro manifest at
    exactly this agent-owned path and the REQUIRED repro gate reads it from the
    working tree — so the nudge must not tell the coder to move it. Task
    89db42ea lost a 99-turn attempt to the contradiction."""
    from no_human.agent import scope_guard
    from no_human.testing.repro_gate import MANIFEST
    assert MANIFEST in scope_guard.HARNESS_READ_FILES, "constants drifted apart"
    assert scope_guard.scratch_redirect(MANIFEST) is None
    assert scope_guard.scratch_redirect(f"/repo/{MANIFEST}", "/repo") is None
    # and a sibling file in the same dir still gets the nudge
    assert scope_guard.scratch_redirect(".no_human/notes.md") is not None


def test_scratch_redirect_ignores_ordinary_paths():
    assert scratch_redirect("src/foo.py") is None


# ── is_outside_repo ───────────────────────────────────────────────────── #

def test_is_outside_repo_tmp_path_is_outside(tmp_path):
    """Task 0ab78498 attempt 1: 15 edits of `/tmp/dcrace/harness.mjs`, a
    harness script written NEXT TO the repo, not in it."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    assert is_outside_repo("/tmp/dcrace/harness.mjs", str(repo_root))


def test_is_outside_repo_linked_worktree_file_is_inside(tmp_path):
    """A linked git worktree is passed in as the repo root itself, so any
    file inside it — including one with a `.no_human` path component, the
    shape `~/.no_human/worktrees/<task_id>` takes — is inside."""
    worktree = tmp_path / ".no_human" / "worktrees" / "task-1"
    worktree.mkdir(parents=True)
    src = worktree / "src" / "calc.py"
    src.parent.mkdir()
    src.write_text("x = 1\n")
    assert not is_outside_repo(str(src), str(worktree))


def test_is_outside_repo_relative_and_unknown_root_count_as_inside():
    # No known repo root: nothing can be classified as outside.
    assert not is_outside_repo("/tmp/dcrace/harness.mjs")
    assert not is_outside_repo("/tmp/dcrace/harness.mjs", "")
    # Relative paths are relative to the worktree cwd, i.e. already inside.
    assert not is_outside_repo("calc.py", "/repo")
    assert not is_outside_repo("src/calc.py", "/repo")


def test_a_symlink_from_outside_into_the_repo_cannot_escape(tmp_path):
    """A symlink planted outside the repo that points AT a real repo file
    resolves under the root via `os.path.realpath` and still counts as
    inside — a symlink must not be usable to escape the tier either way."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    real_file = repo_root / "calc.py"
    real_file.write_text("x = 1\n")
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    link = outside_dir / "calc.py"
    link.symlink_to(real_file)
    assert not is_outside_repo(str(link), str(repo_root))


# ── ScopeGuardHook ────────────────────────────────────────────────────── #


@pytest.mark.asyncio
async def test_scope_guard_hook_warns_on_out_of_plan():
    plan = "## FILES TO CHANGE/CREATE\n- src/foo.py\n"
    events = []
    hook = ScopeGuardHook(plan, "/repo", on_event=lambda k, t: events.append((k, t)))
    result = await hook.hook(
        {"tool_name": "Edit", "tool_input": {"file_path": "src/bar.py"}},
        "id1", None,
    )
    assert "additionalContext" in result.get("hookSpecificOutput", {})
    assert len(events) == 1
    assert events[0][0] == "scope_warning"


@pytest.mark.asyncio
async def test_scope_guard_hook_nudges_once_per_agent_owned_path():
    """The nudge reaches the agent every time (it is the tool's feedback), but
    the event log records it once — five identical warnings was the D18 noise."""
    plan = "## FILES TO CHANGE/CREATE\n- src/foo.py\n"
    events = []
    hook = ScopeGuardHook(plan, "/repo", on_event=lambda k, t: events.append((k, t)))
    call = {"tool_name": "Write",
            "tool_input": {"file_path": "/repo/.no_human/draft.groovy"}}

    first = await hook.hook(call, "id1", None)
    second = await hook.hook(call, "id2", None)

    assert SCRATCH_DIR in first["hookSpecificOutput"]["additionalContext"]
    assert SCRATCH_DIR in second["hookSpecificOutput"]["additionalContext"]
    assert [k for k, _ in events] == ["scratch_redirect"]


@pytest.mark.asyncio
async def test_scope_guard_hook_silent_inside_scratch_dir():
    plan = "## FILES TO CHANGE/CREATE\n- src/foo.py\n"
    events = []
    hook = ScopeGuardHook(plan, "/repo", on_event=lambda k, t: events.append((k, t)))
    result = await hook.hook(
        {"tool_name": "Write",
         "tool_input": {"file_path": f"/repo/{SCRATCH_DIR}/notes.md"}},
        "id1", None,
    )
    assert result == {}
    assert events == []


@pytest.mark.asyncio
async def test_scope_guard_hook_silent_on_in_plan():
    plan = "## FILES TO CHANGE/CREATE\n- src/foo.py\n"
    hook = ScopeGuardHook(plan, "/repo")
    result = await hook.hook(
        {"tool_name": "Edit", "tool_input": {"file_path": "src/foo.py"}},
        "id1", None,
    )
    assert result == {}


@pytest.mark.asyncio
async def test_scope_guard_hook_ignores_reads():
    plan = "## FILES TO CHANGE/CREATE\n- src/foo.py\n"
    hook = ScopeGuardHook(plan, "/repo")
    result = await hook.hook(
        {"tool_name": "Read", "tool_input": {"file_path": "src/bar.py"}},
        "id1", None,
    )
    assert result == {}


@pytest.mark.asyncio
async def test_scope_guard_hook_no_plan():
    hook = ScopeGuardHook("no plan here", "/repo")
    result = await hook.hook(
        {"tool_name": "Edit", "tool_input": {"file_path": "src/bar.py"}},
        "id1", None,
    )
    assert result == {}


@pytest.mark.asyncio
async def test_scope_guard_hook_hands_back_the_manifest_schema_on_a_bad_write(tmp_path):
    """On a write to the repro manifest the hook runs the gate's own reader and
    returns the schema problem immediately — not the scratch nudge (wrong
    advice) and not silence (task 89db42ea learned of its wrong-shaped
    manifest only from the gate, two minutes and one attempt later)."""
    from no_human.testing.repro_gate import MANIFEST
    (tmp_path / ".no_human").mkdir()
    m = tmp_path / MANIFEST
    plan = "## FILES TO CHANGE/CREATE\n- src/foo.py\n"
    events = []
    hook = ScopeGuardHook(plan, str(tmp_path), on_event=lambda k, t: events.append((k, t)))
    call = {"tool_name": "Write", "tool_input": {"file_path": str(m)}}

    m.write_text('{"repro_tests": ["tests/t.py::t"]}')
    bad = await hook.hook(call, "id1", None)
    ctx = bad["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("[REPRO]") and '"tests"' in ctx and SCRATCH_DIR not in ctx

    m.write_text('{"tests": ["tests/t.py::t"]}')
    good = await hook.hook(call, "id2", None)
    assert good == {}
    assert [k for k, _ in events] == ["repro_manifest_hint"]
