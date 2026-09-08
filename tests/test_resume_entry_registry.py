"""Every transition back into a CLAIMABLE status must have a stated disposition.

🔴 THIS TEST EXISTS BECAUSE TEN CONSECUTIVE REVIEW ROUNDS DREW THE BOUNDARY IN
THE WRONG PLACE. Each round scoped the invariant to *writers of `resume_from`* —
"three writers", then "four", then "eight", then "six that carry a checkpoint and
two that do not". Every one of those counts was wrong, and the misses were always
the same shape: a path that re-enters the loop while writing NOTHING inherits the
previous actor's verdict just as surely as one that writes the wrong thing.

The zero-diff honesty gate (`_is_own_partial`) reads `resume_from.sha` together
with `resume_from.by` and credits work already ahead of base only when a HUMAN
gated it. So the invariant is not about who writes that key. It is:

    for EVERY transition that puts a task back into a claimable status,
    `resume_from` must describe THAT re-entry — or be absent.

which is a property of `set_status(..., PENDING | IMPLEMENTING)`, not of
`merge_context({"resume_from": ...})`.

So this test enumerates the call sites from the SOURCE, by AST, and fails when
one appears that nobody has classified. A new re-entry path cannot be added
without writing down which of the four dispositions it takes. Counts in prose
cannot fail; this one can.

AST, not grep, deliberately: `nh unblock` passes a VARIABLE target
(`set_status(t, target, ...)`), so any text search for `TaskStatus.IMPLEMENTING`
misses it — and `nh unblock` is exactly where the ninth defect lived.
"""
from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "no_human"

# disposition -> why it is safe.
STAMPS = "stamps provenance for the actor performing this re-entry"
CLEARS = "clears resume_from: a fresh run must not inherit a checkpoint"
INHERITS = "deliberately inherits — executes a decision another actor already made"
INTERNAL = "within-run transition, not a re-entry from a parked/terminal state"
INHERITS_ELSE_STAMPS = (
    "inherits a HUMAN's gated branch point; otherwise stamps the machine's own "
    "provenance with the sha of the attempt that died")

REGISTRY: dict[tuple[str, str], str] = {
    # --- human re-entries: stamp `by: "human"` via resume_provenance ---
    ("api/app.py", "send_back"): STAMPS,
    ("api/app.py", "resume_task"): STAMPS,
    ("api/app.py", "reply_task"): STAMPS,
    ("cli/commands.py", "task_resume._go"): STAMPS,
    ("cli/commands.py", "reply._go"): STAMPS,
    ("cli/commands.py", "reject._go"): STAMPS,
    # `unblock` passes a VARIABLE target — invisible to any text search.
    ("cli/commands.py", "unblock._go"): STAMPS,
    # --- machine re-entries ---
    ("blockers/wake.py", "WakeWatcher._resume"): STAMPS,   # by: "wake"
    # --- fresh runs: clear, so nothing is inherited ---
    ("api/app.py", "retry_task"): CLEARS,
    ("cli/commands.py", "task_retry._go"): CLEARS,
    # --- deliberate inheritance, argued in the code ---
    # Requeues an attempt that was already in flight when the process died.
    # Stamping over a HUMAN's gated sha would fail their resume as fabrication,
    # so theirs is inherited untouched. Everything else is re-stamped, and both
    # halves of that are load-bearing. Inheriting NOTHING inherits nothing: the
    # requeue re-enters at attempt_n == 1, where `handoff.wip_sha` is ignored,
    # so the dead attempt's committed work was redone from base (11 attempts
    # burned over three restarts, 2026-08-10). And inheriting the sweep's OWN
    # previous stamp is what made restarts 2 and 3 of that same incident resume
    # onto restart 1's sha. The stamp is `by: "orphan_recovery"` — machine
    # provenance, so the zero-diff gate stays armed — over the sha of the
    # attempt that actually died (the one still `in_progress`), never one a
    # CLEARS path above deliberately removed.
    ("core/scheduler.py", "Scheduler._recover_orphans"): INHERITS_ELSE_STAMPS,
    # A graceful server stop mid-session: the same rule as the orphan sweep,
    # one restart earlier. A HUMAN's gated sha is inherited untouched (the
    # WIP commit is still named on the attempt row, so it is findable);
    # otherwise the coder's [WIP-PARTIAL] is stamped `by: "server_stop"` —
    # machine provenance in MACHINE_REQUEUE_PROVENANCE, so the zero-diff
    # gate stays armed. At a cheap boundary (no session open) it stamps
    # nothing at all: there is no in-flight work to preserve.
    ("core/orchestrator.py", "Orchestrator._honor_server_stop"): INHERITS_ELSE_STAMPS,
    # --- internal, within a run the loop already entered ---
    ("core/orchestrator.py", "Orchestrator._run_attempt"): INTERNAL,
    # _advance_after_review's target is a plain variable (TESTING or
    # AWAITING_APPROVAL at its two call sites, both post-review), so the
    # opaque-target scan flags it; it never moves a task back to a
    # parked/terminal state, same as _run_attempt itself. Incident 6408aba0.
    ("core/orchestrator.py", "Orchestrator._advance_after_review"): INTERNAL,
    # _land_no_changes_needed's second set_status call passes a plain
    # variable (`target`, always TaskStatus.AWAITING_APPROVAL on this route),
    # so the opaque-target scan flags it; it never moves a task back to a
    # parked/terminal state, exactly like _advance_after_review — it lands a
    # send-back-resume round that produced zero diff, going forward through
    # TESTING (always legal from here) to AWAITING_APPROVAL.
    ("core/orchestrator.py", "Orchestrator._land_no_changes_needed"): INTERNAL,
}

CLAIMABLE = {"PENDING", "IMPLEMENTING"}


def _reentry_sites() -> set[tuple[str, str]]:
    """Every `set_status` call whose target is claimable, or is a variable."""
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        stack: list[str] = []

        class V(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append(node.name); self.generic_visit(node); stack.pop()
            visit_AsyncFunctionDef = visit_FunctionDef
            def visit_ClassDef(self, node):
                stack.append(node.name); self.generic_visit(node); stack.pop()
            def visit_Call(self, node):
                fn = node.func
                if isinstance(fn, ast.Attribute) and fn.attr == "set_status":
                    target = node.args[1] if len(node.args) > 1 else None
                    text = ast.unparse(target) if target is not None else ""
                    claimable = any(text.endswith(f".{s}") for s in CLAIMABLE)
                    # A non-literal target (a variable) is treated as claimable:
                    # it MIGHT be, and that is exactly the case that hid a bug.
                    opaque = bool(target) and not isinstance(target, ast.Attribute)
                    if claimable or opaque:
                        found.add((str(path.relative_to(SRC)), ".".join(stack)))
                self.generic_visit(node)

        V().visit(tree)
    return found


def test_every_reentry_into_the_loop_has_a_stated_disposition():
    sites = _reentry_sites()

    # Positive control: the instrument must actually find things. If this ever
    # trips, the AST walk broke and the test below would pass vacuously.
    assert len(sites) >= 10, (
        f"the AST walk found only {len(sites)} re-entry sites — the instrument "
        "is broken, so its 'no unregistered paths' result means nothing")

    unregistered = sorted(sites - set(REGISTRY))
    assert not unregistered, (
        "a path re-enters the loop with no stated disposition:\n  "
        + "\n  ".join(f"{f}::{fn}" for f, fn in unregistered)
        + "\n\nThe zero-diff honesty gate reads `resume_from.sha` WITH "
          "`resume_from.by`. Decide which this path is and add it to REGISTRY:\n"
          "  STAMPS   — a human or machine DECIDED to re-enter; write provenance\n"
          "             via `blockers.resume_provenance(checkpoint, by)`\n"
          "  CLEARS   — a FRESH run; write {'resume_from': None} so nothing is\n"
          "             inherited (branches from base)\n"
          "  INHERITS — it merely EXECUTES a decision another actor made; say so\n"
          "             in the code, because silence here has cost ten rounds\n"
          "  INTERNAL — a transition inside a run the loop already entered")


def _drops_checkpoint(v: ast.AST) -> bool:
    """Does this `resume_from` value carry NO sha?

    Both spellings, because the orphan sweep cannot tell them apart: a literal
    ``None`` (RFC 7396 — deletes the key) and ``resume_provenance(None, ...)``
    (writes ``sha: None``) both leave a context that reads exactly like a task
    which never had a checkpoint. A `resume_from` built from a VARIABLE
    checkpoint may well carry a sha and is deliberately not matched.
    """
    if isinstance(v, ast.Constant) and v.value is None:
        return True
    return (isinstance(v, ast.Call)
            and getattr(v.func, "id", getattr(v.func, "attr", "")) == "resume_provenance"
            and bool(v.args)
            and isinstance(v.args[0], ast.Constant)
            and v.args[0].value is None)


def _drop_sites_and_closers() -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(functions that drop the checkpoint, functions that retire the rows).

    Two spellings are matched, because both are live in `src/`: the key inside a
    dict literal (`merge_context(t.id, {"resume_from": None})`) and an assignment
    to a subscript of a patch built up first (`patch["resume_from"] = None`, as at
    `api/app.py:2637` and `blockers/wake.py:474`). Missing the second is the exact
    shape of miss this file exists to stop — the twin of one that got fixed.

    NOT matched, and deliberately so rather than silently: the keyword spelling
    `dict(resume_from=None)`, `patch.setdefault("resume_from", None)`, a subscript
    `AugAssign`, and any patch assembled through a variable the walk would have to
    follow. Matching those needs dataflow analysis, not an AST shape; a new writer
    in one of those spellings would be invisible here.
    """
    drops: set[tuple[str, str]] = set()
    closes: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        rel = str(path.relative_to(SRC))
        stack: list[str] = []

        class V(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append(node.name); self.generic_visit(node); stack.pop()
            visit_AsyncFunctionDef = visit_FunctionDef
            def visit_ClassDef(self, node):
                stack.append(node.name); self.generic_visit(node); stack.pop()
            def visit_Dict(self, node):
                for k, v in zip(node.keys, node.values):
                    if (isinstance(k, ast.Constant) and k.value == "resume_from"
                            and _drops_checkpoint(v)):
                        drops.add((rel, ".".join(stack)))
                self.generic_visit(node)
            def visit_Assign(self, node):
                # `patch["resume_from"] = None` — no ast.Dict to see.
                for tgt in getattr(node, "targets", None) or [node.target]:
                    if (isinstance(tgt, ast.Subscript)
                            and isinstance(tgt.slice, ast.Constant)
                            and tgt.slice.value == "resume_from"
                            and _drops_checkpoint(node.value)):
                        drops.add((rel, ".".join(stack)))
                self.generic_visit(node)
            visit_AnnAssign = visit_Assign
            def visit_Call(self, node):
                if getattr(node.func, "attr", "") == "close_open_attempts":
                    closes.add((rel, ".".join(stack)))
                self.generic_visit(node)

        V().visit(tree)
    return drops, closes


def test_every_path_that_drops_the_checkpoint_also_retires_the_attempt_rows():
    """🔴 CLEARING THE CONTEXT IS ONLY HALF OF A CLEAR.

    `Scheduler._recover_orphans` re-derives a checkpoint from the attempt row
    still `in_progress`, and a crashed worker leaves exactly that row behind.
    So every one of these paths promised "a fresh run from base" and the first
    orphan sweep afterwards put the dropped checkpoint straight back — from a
    run that had already failed. The cure is `Store.close_open_attempts` at the
    clear, because the sweep has nothing left to read: a context with no
    checkpoint looks identical whether one was deliberately dropped or never
    existed.

    Enumerated from the source rather than listed in prose, and by AST rather
    than grep, for the reason the rest of this file exists: the count of these
    sites has been written down wrong at least three times, and the ones that
    got missed were always the twin of one that got fixed (`nh task retry`
    behind `POST /retry`, `nh reject` behind Send back).
    """
    drops, closes = _drop_sites_and_closers()
    # Positive control — a broken walk must not pass vacuously. Was 6 before
    # the LeadAgent sub-task-retry and _unblock_ready writers were removed
    # 2026-08-12 (operator decision A1); 4 remain: `POST /tasks/{id}/retry`,
    # `nh task retry`, and both "send back" twins.
    assert len(drops) >= 4, (
        f"the AST walk found only {len(drops)} checkpoint-dropping writers "
        f"({sorted(drops)}) — the instrument is broken")

    missing = sorted(drops - closes)
    assert not missing, (
        "these paths drop the resume checkpoint without retiring the attempt "
        "rows the orphan sweep re-derives it from, so the next crash "
        "resurrects it:\n  "
        + "\n  ".join(f"{f}::{fn}" for f, fn in missing)
        + "\n\nAdd `await store.close_open_attempts(<task_id>)` BEFORE the "
          "context write (ordered so a crash between the two leaves the rows "
          "closed rather than the checkpoint cleared with the rows open).")


def test_the_registry_has_no_entries_for_paths_that_no_longer_exist():
    """A registry that outlives its code rots into a lie — the same failure as
    the prose counts this test replaces."""
    stale = sorted(set(REGISTRY) - _reentry_sites())
    assert not stale, (
        "REGISTRY lists paths that no longer re-enter the loop; remove them:\n  "
        + "\n  ".join(f"{f}::{fn}" for f, fn in stale))


# --------------------------------------------------------------------------- #
# The pending-stop twin of the invariant above.                                #
#                                                                              #
# `cancel_requested` is the one signal a running orchestrator observes         #
# (`_pending_cancel` at the top of `_drive` and of the attempt loop, the coder #
# sink). A stop the worker never got to honour — killed, crashed, or the pause #
# landed during review — survives in the column. Whether the NEXT re-entry     #
# honours it or withdraws it is a decision, and like `resume_from` it has to   #
# be stated per site, not assumed: a human acting again (answer, retry,        #
# resume, unblock, reject, send back) supersedes their own earlier stop, so    #
# those withdraw it; a machine re-entry (a wake timer, an orphan requeue)      #
# executes no new human decision, so it KEEPS the stop and `_drive` parks on   #
# turn zero — the user's last instruction, honoured late rather than           #
# discarded. Board Pause → Cancel → Retry once re-parked the fresh run on       #
# exactly this (PR #504); `unblock` and `reject` were the two human paths      #
# still missing the withdrawal.                                                #
#                                                                              #
# The check is PLACEMENT, not presence: the clear must sit in the SAME         #
# statement block as the claimable `set_status`, BEFORE it. A clear on a       #
# sibling branch (`unblock --fail` parks a possibly-LIVE task, where the flag  #
# is the pause its worker is about to honour) or after the transition is the  #
# defect, and a presence test passed it green. Calls inside a nested def are   #
# attributed to the enclosing registered function, so a KEEPS site cannot      #
# withdraw through a helper.                                                   #
# --------------------------------------------------------------------------- #

WITHDRAWS = "human re-entry: withdraws a pending stop (clear_cancel_request in the same block, before set_status)"
KEEPS = ("machine re-entry: keeps a pending stop — no new human decision; "
         "_drive honours it on turn zero and parks")
STOP_INTERNAL = "within-run transition: the stop is honoured by _drive/_honor_cancel, not here"

STOP_REGISTRY: dict[tuple[str, str], str] = {
    ("api/app.py", "send_back"): WITHDRAWS,
    ("api/app.py", "resume_task"): WITHDRAWS,
    ("api/app.py", "reply_task"): WITHDRAWS,
    ("api/app.py", "retry_task"): WITHDRAWS,
    ("cli/commands.py", "task_resume._go"): WITHDRAWS,
    ("cli/commands.py", "reply._go"): WITHDRAWS,
    ("cli/commands.py", "reject._go"): WITHDRAWS,
    ("cli/commands.py", "unblock._go"): WITHDRAWS,
    ("cli/commands.py", "task_retry._go"): WITHDRAWS,
    ("blockers/wake.py", "WakeWatcher._resume"): KEEPS,
    ("core/scheduler.py", "Scheduler._recover_orphans"): KEEPS,
    # A graceful server stop is a machine requeue: it executes no human
    # decision, so a human's pending pause survives it and `_drive` parks on
    # turn zero at the next start. (`_pending_cancel` already lets a human
    # cancel outrank the stop while the process lives.)
    ("core/orchestrator.py", "Orchestrator._honor_server_stop"): KEEPS,
    ("core/orchestrator.py", "Orchestrator._run_attempt"): STOP_INTERNAL,
    ("core/orchestrator.py", "Orchestrator._advance_after_review"): STOP_INTERNAL,
    ("core/orchestrator.py", "Orchestrator._land_no_changes_needed"): STOP_INTERNAL,
}


def _is_claimable_set_status(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "set_status"):
        return False
    target = node.args[1] if len(node.args) > 1 else None
    text = ast.unparse(target) if target is not None else ""
    return (any(text.endswith(f".{s}") for s in CLAIMABLE)
            or (bool(target) and not isinstance(target, ast.Attribute)))


def _calls_in(stmt: ast.AST, attr: str) -> bool:
    return any(isinstance(n, ast.Call) and getattr(n.func, "attr", "") == attr
               for n in ast.walk(stmt))


def _stop_placement() -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(sites whose claimable set_status has a clear_cancel_request EARLIER IN
    THE SAME BLOCK, sites that call clear_cancel_request ANYWHERE — nested
    defs attributed to the enclosing registered function)."""
    placed: set[tuple[str, str]] = set()
    anywhere: set[tuple[str, str]] = set()
    registered = set(STOP_REGISTRY)
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC))
        tree = ast.parse(path.read_text())
        stack: list[str] = []

        def owner() -> tuple[str, str]:
            # the innermost enclosing name that is registered, else the full path
            for i in range(len(stack), 0, -1):
                key = (rel, ".".join(stack[:i]))
                if key in registered:
                    return key
            return (rel, ".".join(stack))

        class V(ast.NodeVisitor):
            def _scan_blocks(self, node):
                for field_ in ("body", "orelse", "finalbody", "handlers"):
                    block = getattr(node, field_, None)
                    if not isinstance(block, list):
                        continue
                    for i, stmt in enumerate(block):
                        if any(_is_claimable_set_status(n) for n in ast.walk(stmt)):
                            if any(_calls_in(prev, "clear_cancel_request") for prev in block[:i]):
                                placed.add(owner())
            def visit_FunctionDef(self, node):
                stack.append(node.name)
                self._scan_blocks(node)
                self.generic_visit(node)
                stack.pop()
            visit_AsyncFunctionDef = visit_FunctionDef
            def visit_ClassDef(self, node):
                stack.append(node.name); self.generic_visit(node); stack.pop()
            def generic_visit(self, node):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                    self._scan_blocks(node)
                if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "clear_cancel_request":
                    anywhere.add(owner())
                super().generic_visit(node)
        V().visit(tree)
    return placed, anywhere


def test_stop_registry_covers_every_reentry():
    sites = _reentry_sites()
    assert len(sites) >= 10, "site walk returned implausibly few re-entries"
    unregistered = sorted(sites - set(STOP_REGISTRY))
    assert not unregistered, (
        "re-entry path(s) with no stated pending-stop disposition — add each to "
        "STOP_REGISTRY as WITHDRAWS (human) or KEEPS (machine):\n  "
        + "\n  ".join(f"{f}: {fn}" for f, fn in unregistered))


def test_stop_registry_has_no_stale_entries():
    stale = sorted(set(STOP_REGISTRY) - _reentry_sites())
    assert not stale, f"STOP_REGISTRY lists paths that no longer re-enter: {stale}"


def test_every_human_reentry_withdraws_a_pending_stop_before_it_transitions():
    """RED before the fix on `unblock._go` and `reject._go`; RED again if the
    clear moves to a sibling branch or after `set_status`.

    KNOWN LIMIT, stated so nobody reads more into a green: a clear at the JOIN
    of an if/else, before a `set_status` whose target is a VARIABLE, satisfies
    this check even when one arm of that variable is terminal — which is
    exactly `unblock --fail` on a live task. That shape is behavioural, not
    structural, and is pinned by
    tests/test_cli_commands.py::test_unblock_fail_on_a_live_task_leaves_a_pending_stop_for_its_worker."""
    placed, _anywhere = _stop_placement()
    missing = sorted(site for site, d in STOP_REGISTRY.items()
                     if d == WITHDRAWS and site not in placed)
    assert not missing, (
        "human re-entry path(s) whose claimable set_status is not preceded, in "
        "the same block, by clear_cancel_request — the next attempt would honour "
        "a stale stop on turn zero and park straight back:\n  "
        + "\n  ".join(f"{f}: {fn}" for f, fn in missing))


def test_no_machine_reentry_withdraws_a_human_stop():
    _placed, anywhere = _stop_placement()
    keeping = sorted(site for site, d in STOP_REGISTRY.items()
                     if d == KEEPS and site in anywhere)
    assert not keeping, (
        f"machine re-entry path(s) (or a helper nested in one) withdraw a "
        f"human's stop — if deliberate, re-register as WITHDRAWS and say why: {keeping}")
