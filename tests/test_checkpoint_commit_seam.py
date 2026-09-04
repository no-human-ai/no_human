"""Every checkpoint commit must go through ONE shared seam.

PR #541 fixed only the ``[WIP-BLOCKED]`` path (``_checkpoint_wip``) to route
through ``commit_with_manifest_repair``. At PR #541's head, 7 other sites in
``src/no_human/core/orchestrator.py`` still called ``repo.commit_all(...)``
raw, bypassing the manifest-repair seam and hitting the live pre-commit
manifest gate directly — silently losing checkpoint commits on refusal:
``_honor_server_stop`` [WIP-PARTIAL], attempt-timeout, StuckAbort,
stuck-detect, challenged-blocker, and the linked-repo commit.

This file pins the fix: ONE ``_checkpoint_commit(repo, message, *,
paths=None)`` helper (routing through ``commit_with_manifest_repair`` and
draining ``repaired`` into a ``manifest_repaired`` event exactly as the
normal path and PR #541 do) is the only place in ``orchestrator.py`` that
ever calls a raw ``.commit_all(``/``.commit_paths(``/``.commit(`` with a
``[WIP-`` message — enforced statically (AST registry test, in the idiom of
``tests/test_server_stop_checkpoint.py::test_every_coder_sink_session_has_a_stated_stop_disposition``)
— and behaviourally, for the server-stop checkpoint path.

An independent review of the first cut of this fix (task 86d747b9, PR #541)
found an EIGHTH raw ``[WIP-PARTIAL]`` site outside ``orchestrator.py``
entirely: ``core.worktree._salvage_one`` (the hard-kill startup salvage
path, ``fe3bc09f8`` / PR #555 — added after this fix's own plan was
written), which committed via a raw ``repo.commit_all(...)`` and hit the
same live pre-commit manifest gate. The registry test below therefore scans
BOTH ``orchestrator.py`` and ``worktree.py`` — the latter has no
``_checkpoint_commit`` seam of its own (it is not an `Orchestrator` method),
so its sanctioned form is calling `commit_with_manifest_repair` directly
(via `asyncio.to_thread`, mirroring the async pattern used elsewhere in
`orchestrator.py`) rather than a raw `.commit_all(`/`.commit_paths(`/
`.commit(`.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import no_human.core.orchestrator as orch_mod
import no_human.core.worktree as worktree_mod
from no_human.blockers import SERVER_STOP_REASON
from no_human.core.task import TaskStatus

from tests.test_infra_not_work import (  # noqa: F401 — fixtures re-exported on purpose
    _git,
    _incident_result,
    _run_one_attempt,
    bare_repo,
)

# Every module a `[WIP-` checkpoint commit can be made from. Adding a NEW
# `[WIP-` commit site to any other module must add it here too, or that
# module's raw commit calls go unchecked.
_SCANNED_MODULES = (orch_mod, worktree_mod)


def _head(cwd) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


async def _implementing(store, task):
    from no_human.core.task import TaskStatus as _TS
    await store.set_status(task, _TS.CONTEXT)
    await store.set_status(task, _TS.PLANNING)
    await store.set_status(task, _TS.IMPLEMENTING)
    return await store.get_task(task.id)


def _wip_message_prefix(node: ast.AST) -> str | None:
    """Return the literal leading text of a message argument/keyword if it is
    a plain string or an f-string whose FIRST piece is a plain string
    constant (the shape every ``[WIP-...] {...}`` call site uses); else None.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def test_no_checkpoint_commits_bypass_the_single_seam():
    """Registry test: every raw `.commit_all(`/`.commit_paths(`/`.commit(`
    call anywhere in `_SCANNED_MODULES` (orchestrator.py + worktree.py), on
    any receiver, with a `[WIP-` message, must be `self._checkpoint_commit(
    ...)` — the ONE seam in orchestrator.py. On pre-fix code this finds
    offenders (the sites listed in the module docstring above, including the
    worktree.py hard-kill salvage site) and FAILS.

    A non-`[WIP-` raw commit call is still allowed ONLY if it is the seam's
    own sanctioned bypass fallback — `repo.commit_all(msg, bypass_gate=True)`
    in `_checkpoint_wip`, which runs *after* `_checkpoint_commit` has already
    raised on a hard gate refusal and is itself guarded by `is_gate_refusal`;
    `GitRepo.commit_all` separately refuses `bypass_gate=True` for any
    message not starting `[WIP-` (git.py, out of scope here). This is
    permitted by *structure* — the literal `bypass_gate=True` keyword — not
    by a name waiver on the enclosing function, per the design in
    `.no_human/PLAN.md` (\"The AST test permits it by structure … not by a
    name waiver\"). Every other raw commit call, `[WIP-` or not, is flagged,
    since the seam is meant to be the sole commit path for checkpoints and
    the linked-repo checkpoint alike. `worktree.py` has no `_checkpoint_commit`
    method of its own (it is not an `Orchestrator`); its sanctioned form is
    calling `commit_with_manifest_repair` directly rather than any raw
    `.commit_all(`/`.commit_paths(`/`.commit(`, so it passes this scan as
    long as it makes no such raw call at all (it has no legitimate use for
    `bypass_gate=True` either — that kwarg only exists on `GitRepo.commit_all`
    and is meaningless without the seam's own hard-refusal handling).
    """
    offenders: list[tuple[str, str, int, str]] = []

    for mod in _SCANNED_MODULES:
        src_path = Path(mod.__file__)
        tree = ast.parse(src_path.read_text())

        class _Visitor(ast.NodeVisitor):
            def __init__(self):
                self.fn_stack: list[str] = []

            def _enclosing(self) -> str:
                return self.fn_stack[-1] if self.fn_stack else "<module>"

            def visit_FunctionDef(self, node):
                self.fn_stack.append(node.name)
                self.generic_visit(node)
                self.fn_stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                func = node.func
                if (isinstance(func, ast.Attribute)
                        and func.attr in {"commit_all", "commit_paths", "commit"}):
                    fn_name = self._enclosing()
                    # The seam's OWN sanctioned bypass fallback is permitted by
                    # STRUCTURE — a literal `bypass_gate=True` keyword on a call
                    # to `repo.<commit-like>(...)` — not by a name waiver on the
                    # enclosing function (PLAN.md: "permits it by structure …
                    # not by a name waiver").
                    has_bypass_gate_true = any(
                        kw.arg == "bypass_gate"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True
                        for kw in node.keywords
                    )
                    is_sanctioned = (
                        has_bypass_gate_true
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "repo"
                    )
                    if not is_sanctioned:
                        offenders.append((mod.__name__, fn_name, node.lineno,
                                           ast.unparse(node)))
                self.generic_visit(node)

        _Visitor().visit(tree)

    assert offenders == [], (
        "raw commit call(s) outside the single `_checkpoint_commit` seam — "
        f"route through `self._checkpoint_commit(...)` (or, outside "
        f"orchestrator.py, `commit_with_manifest_repair` directly) instead: "
        f"{offenders}")


def test_every_wip_checkpoint_message_routes_through_the_seam():
    """Second half of the registry: every call whose message argument starts
    literally with `[WIP-` must be `self._checkpoint_commit(...)`. Catches a
    regression where a NEW raw call is added that does not even match
    `commit_all`/`commit_paths`/`commit` by name (e.g. a re-exported alias).
    Scans `_SCANNED_MODULES` (orchestrator.py + worktree.py)."""
    offenders: list[tuple[str, int, str]] = []
    _COMMIT_LIKE = {"commit_all", "commit_paths", "commit", "_checkpoint_commit"}

    for mod in _SCANNED_MODULES:
        src_path = Path(mod.__file__)
        tree = ast.parse(src_path.read_text())

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute) and node.func.attr in _COMMIT_LIKE):
                continue
            args = list(node.args) + [kw.value for kw in node.keywords if kw.arg is None]
            # Also check any keyword literally named `message`.
            msg_candidates = args[:2] + [kw.value for kw in node.keywords if kw.arg == "message"]
            is_wip = any(
                (_wip_message_prefix(c) or "").startswith("[WIP-")
                for c in msg_candidates
            )
            if not is_wip:
                continue
            is_seam_call = (
                node.func.attr == "_checkpoint_commit"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
            )
            if not is_seam_call:
                offenders.append((mod.__name__, node.lineno, ast.unparse(node)))

    assert offenders == [], (
        "a `[WIP-...]`-prefixed commit call bypasses `self._checkpoint_commit`: "
        f"{offenders}")


async def test_server_stop_checkpoint_survives_a_repairable_manifest_refusal(
        store, bare_repo, tmp_path):
    """AC2a: a server stop mid-session ([WIP-PARTIAL]) with a stale manifest
    pin must still commit successfully after the seam's own repair-and-retry,
    and the `manifest_repaired` event must name the repaired file — proving
    `_honor_server_stop`'s checkpoint (orchestrator.py:2402) now goes through
    `_checkpoint_commit` instead of a raw `repo.commit_all(...)` that would
    hit the live gate directly and lose the WIP on refusal."""
    from no_human.core.task import Task
    from no_human.notify.slack import SlackNotifier
    from tests.test_infra_not_work import _config, _ScriptedBackend

    cfg = _config(tmp_path)
    events: list = []
    orch = orch_mod.Orchestrator(
        store, cfg.data, _ScriptedBackend(_incident_result()),
        SlackNotifier(None), event_sink=events.append)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)
    task = await _implementing(store, task)
    repo = orch_mod.GitRepo(bare_repo)
    _git(bare_repo, "checkout", "-q", "-b", "no-human/live")
    (bare_repo / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    aid = await store.create_attempt(task.id, 1)

    calls: list[tuple] = []
    real_repair = orch_mod.commit_with_manifest_repair

    def fake(repo_arg, paths, message, on_repair=None):
        calls.append((paths, message))
        if on_repair:
            on_repair(["src/pkg/mod.py"], "1 stale pin(s)")
        return repo_arg.commit_all(message)

    orch_mod.commit_with_manifest_repair = fake
    try:
        outcome = await orch._honor_server_stop(
            task, repo, "no-human/live", attempt_id=aid)
    finally:
        orch_mod.commit_with_manifest_repair = real_repair

    assert calls, "the seam was never invoked — reverting to a raw commit_all empties this"
    assert outcome.status == TaskStatus.IMPLEMENTING
    fresh = await store.get_task(task.id)
    rf = fresh.context["resume_from"]
    assert rf["by"] == "server_stop"
    assert rf["sha"] and rf["branch"] == "no-human/live"
    assert _head(bare_repo) == rf["sha"], "the WIP commit is what was stamped"

    kinds = [e.get("kind") for e in events]
    assert "checkpoint" in kinds, kinds
    repaired_events = [e for e in events if e.get("kind") == "manifest_repaired"]
    assert len(repaired_events) == 1, kinds
    assert "src/pkg/mod.py" in repaired_events[0].get("text", "")
    assert repaired_events[0].get("paths") == ["src/pkg/mod.py"]
