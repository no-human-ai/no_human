"""W3.4 knowledge triggers: a tagged memory injects only when relevant."""

from no_human.learning.triggers import (
    filter_triggered,
    memory_is_triggered,
    playbook_is_triggered,
    select_playbook,
)


def test_playbook_without_trigger_never_auto_matches():
    # unlike a memory (no tags = always inject), an untriggered playbook stays
    # manual-only — a heavy procedure must not attach to unrelated tasks.
    assert playbook_is_triggered({"trigger_keywords": "[]"}, "anything") is False
    assert playbook_is_triggered({"trigger_keywords": None}, "anything") is False


def test_playbook_matches_on_keyword_substring():
    pb = {"trigger_keywords": '["stripe", "payment"]'}
    assert playbook_is_triggered(pb, "Add Stripe support") is True
    assert playbook_is_triggered(pb, "fix a logging bug") is False


def test_select_playbook_returns_first_match_or_none():
    pbs = [
        {"title": "A", "trigger_keywords": '["kafka"]'},
        {"title": "B", "trigger_keywords": '["stripe"]'},
    ]
    assert select_playbook(pbs, "add stripe webhook")["title"] == "B"
    assert select_playbook(pbs, "unrelated task") is None


def test_untagged_memory_always_injects():
    """Backward compatibility: no tags = unconditional, exactly as before."""
    m = {"title": "always", "content": "x", "tags": None}
    assert memory_is_triggered(m, "anything at all") is True
    assert memory_is_triggered({"title": "y", "tags": "[]"}, "z") is True


def test_tagged_memory_injects_only_on_match():
    m = {"title": "kafka rule", "tags": '["kafka", "broker"]'}
    assert memory_is_triggered(m, "Fix the Kafka topic creation") is True   # case-insensitive
    assert memory_is_triggered(m, "Update the UI button color") is False


def test_tags_accept_list_or_json_or_junk():
    assert memory_is_triggered({"tags": ["mtls"]}, "fix mTLS cert") is True
    assert memory_is_triggered({"tags": "not json"}, "anything") is True  # junk → unconditional
    assert memory_is_triggered({"tags": '["  "]'}, "anything") is True    # blank tag → unconditional


def test_filter_triggered_partitions_the_set():
    mems = [
        {"title": "global", "tags": None},
        {"title": "kafka", "tags": '["kafka"]'},
        {"title": "clickhouse", "tags": '["clickhouse"]'},
    ]
    out = filter_triggered(mems, "debug the kafka consumer lag")
    titles = {m["title"] for m in out}
    assert titles == {"global", "kafka"}  # clickhouse held back


def test_provenance_tags_are_filter_only_not_triggers():
    # vocab.py: ORIGIN_* tags name where a lesson came from, not what it is
    # about — "address the review comments" must not summon every
    # review-origin lesson in the store.
    m = {"title": "digest pinning", "tags": '["review", "container"]'}
    assert memory_is_triggered(m, "address the review comments on the docs") is False
    assert memory_is_triggered(m, "pin the docker image digest") is True


def test_a_provenance_only_memory_never_auto_injects():
    # No topical tag = nothing to match a task against. It stays visible in
    # `nh learnings` (filterable by producer); it does not ride every prompt.
    m = {"title": "origin only", "tags": '["review"]'}
    assert memory_is_triggered(m, "review the code") is False
    assert memory_is_triggered(m, "anything else") is False
    sup = {"title": "sup only", "tags": '["supervisor"]'}
    assert memory_is_triggered(sup, "the supervisor said so") is False


def test_generic_aliases_do_not_trigger():
    # "path"/"env"/"json"/"request" appear in half the queue's task text; a
    # lesson tagged environment/api fires on its specific terms, not those.
    env_lesson = {"title": "venv trap", "tags": '["environment"]'}
    assert memory_is_triggered(env_lesson, "update the api request path handling") is False
    assert memory_is_triggered(env_lesson, "the venv was built against main") is True
    api_lesson = {"title": "endpoint 500", "tags": '["api"]'}
    assert memory_is_triggered(api_lesson, "fix the json parsing in the config loader") is False
    assert memory_is_triggered(api_lesson, "the endpoint returns 500") is True


# --------------------------------------------------------------------------- #
# S2. `last_used_at` — which confirmed rules have ever actually done anything   #
#                                                                              #
# The confirm queue can say what a human approved. Nothing could say what any   #
# of it ever DID: 53 confirmed rules in the operator's own install, no record   #
# of a single injection. These tests pin the stamp and, more importantly, the   #
# trap that makes a naive version of it lie.                                    #
# --------------------------------------------------------------------------- #

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task

_ORCH_SRC = (
    Path(__file__).resolve().parents[1]
    / "src" / "no_human" / "core" / "orchestrator.py"
).read_text(encoding="utf-8")


def _bare_orchestrator(store):
    """An Orchestrator with only what `_load_active_memories` touches.

    `__new__` rather than `__init__` on purpose: booting a real one needs a
    config, a backend and a repo, none of which this behaviour depends on, and
    a test that drags all three in stops being run.
    """
    orch = Orchestrator.__new__(Orchestrator)
    orch.store = store
    return orch


async def test_injecting_a_rule_stamps_last_used_at(tmp_path):
    """The stamp exists at all, and only lands on rules that actually fired."""
    async with Store(tmp_path / "nh.db") as store:
        fires = await store.add_memory(
            mem_type="rule", title="kafka topic rule", content="x",
            tags=["kafka"], confirmed=True)
        holds = await store.add_memory(
            mem_type="rule", title="css rule", content="x",
            tags=["css"], confirmed=True)

        task = Task.new("Fix the kafka topic creation", repo_path="")
        all_scoped, triggered = await _bare_orchestrator(store)._load_active_memories(task)

        assert {m["id"] for m in all_scoped} == {fires, holds}, (
            "control: both rules must be FETCHED, or the trigger filter is "
            "not what is being measured here")
        assert [m["id"] for m in triggered] == [fires]

        rows = {m["id"]: m for m in await store.list_memories(confirmed=True)}
        assert rows[fires]["last_used_at"], "the injected rule was never stamped"
        assert rows[holds]["last_used_at"] is None, (
            "a rule that was fetched but did NOT trigger has not been used")


async def test_a_rule_held_by_the_term_screen_is_not_recorded_as_used(tmp_path):
    """The subtle half. `filter_triggered` returns matches; the term screen
    then withholds some of them from every prompt. Stamping the FILTER's output
    records a use that never happened — and the symptom is invisible, because a
    rule silently withheld from every task would read as healthy in `--stale`
    forever. The stamp comes off the property, which is the only thing that
    knows what survived.
    """
    from no_human.eval.vendor_terms import BANNED_TERMS

    term = BANNED_TERMS[0]
    async with Store(tmp_path / "nh.db") as store:
        held = await store.add_memory(
            mem_type="rule", title=f"The {term} deployment runner",
            content="Drain the queue before deploying.", confirmed=True)
        clean = await store.add_memory(
            mem_type="rule", title="Deployment runner conventions",
            content="Drain the queue before deploying.", confirmed=True)
        # P1 brain hygiene's WRITE-time quarantine gate (`core/db.py`'s
        # `add_memory`) uses this same banned-terms matcher, so `held` was
        # also quarantined on insert — a separate, additive control (see
        # `learning/provenance.py`'s module docstring). That would hide it
        # from `list_memories(confirmed=True, …)` entirely, before it ever
        # reaches the READ-side screen this test targets. Un-quarantine it so
        # the row reaches `all_scoped` exactly as it did before that gate
        # existed, isolating the property under test.
        await store.set_quarantine(held, False, None)

        task = Task.new("fix the deployment runner", repo_path="")
        orch = _bare_orchestrator(store)
        _, triggered = await orch._load_active_memories(task)

        assert {m["id"] for m in triggered} == {held, clean}, (
            "control: BOTH rules must pass the trigger filter, or this test "
            "is not exercising the screen at all")
        assert [m["id"] for m in orch._active_memories] == [clean], (
            "control: the screen must actually hold the banned-term rule")

        rows = {m["id"]: m for m in await store.list_memories(confirmed=True)}
        assert rows[clean]["last_used_at"], "the injected rule was never stamped"
        assert rows[held]["last_used_at"] is None, (
            "a rule the term screen withheld from every prompt was recorded "
            "as USED — `nh learnings --stale` would call it healthy forever")


def _install_sites(source: str) -> list[int]:
    """Line numbers in *source* that install a new active rule set.

    SCANNED BY ASSIGNMENT, not by the name of the function on the right-hand
    side. The first version of this guard counted `filter_triggered(` calls and
    a mutation defeated it in one line — `from ..learning.triggers import
    filter_triggered as _ft` — which is not an exotic form, it is what anyone
    avoiding a name collision writes. Whatever a path calls the filter, it must
    end up ASSIGNING `self._active_memories` for the rules to reach a prompt at
    all, so that is where the tripwire belongs.

    Parsed, not regex'd over text. This module's own prose discusses the
    attribute at length and a guard its own docstrings can trip is a guard that
    gets loosened until it stops working; `ast` cannot see inside a string or a
    comment at all. It also picks up tuple targets and `+=` for free, two of the
    nine forms that defeated the sibling guard in
    `test_active_memories_mutation_guard.py`.

    STILL NOT A PROOF. `setattr(self, "_active_memories", x)` and a `__dict__`
    write are assignments this cannot see, and that sibling's docstring is the
    record of someone actually doing it. What makes the behaviour safe is the
    two tests above, which MEASURE the stamp; this catches the ordinary
    regression early and by name.
    """
    tree = ast.parse(textwrap.dedent(source))
    hits: list[int] = []
    for node in ast.walk(tree):
        targets = list(getattr(node, "targets", []))
        if isinstance(node, ast.AugAssign):
            targets = [node.target]
        for t in targets:
            for leaf in ([*t.elts] if isinstance(t, (ast.Tuple, ast.List))
                         else [t]):
                if (isinstance(leaf, ast.Attribute)
                        and leaf.attr == "_active_memories"
                        and isinstance(leaf.value, ast.Name)
                        and leaf.value.id == "self"):
                    hits.append(node.lineno)
    return sorted(hits)


def test_only_one_place_turns_a_task_into_an_active_rule_set():
    """The trap that motivated the helper. `filter_triggered` had TWO callers —
    the implement path and the review path — so a stamp added at one of them
    reports "never used" for every rule the OTHER one used, and it is wrong in
    the direction that gets a working rule deleted.
    """
    sites = _install_sites(_ORCH_SRC)
    assert len(sites) == 1, (
        f"orchestrator.py installs an active rule set at {len(sites)} sites "
        f"(orchestrator.py lines {sites}). Every one must go through "
        f"`_load_active_memories`, or `last_used_at` under-reports for "
        f"whichever path skipped it."
    )
    # ...and that ONE site is inside the helper, not somewhere that skips the
    # stamp. Compared by line RANGE off the same parse rather than by slicing
    # the text: a source fragment is not valid Python and the split-and-reparse
    # version of this raised SyntaxError instead of asserting anything.
    helpers = [n for n in ast.walk(ast.parse(_ORCH_SRC))
               if isinstance(n, ast.AsyncFunctionDef)
               and n.name == "_load_active_memories"]
    assert len(helpers) == 1, "the helper was renamed — retarget this test"
    helper = helpers[0]
    assert helper.lineno <= sites[0] <= (helper.end_lineno or helper.lineno), (
        f"the single install site (line {sites[0]}) is outside "
        f"`_load_active_memories` (lines {helper.lineno}-{helper.end_lineno}) "
        f"— it installs rules without stamping them")


def test_the_single_install_site_scan_can_actually_fail():
    """Known-positive control. A guard whose detector has never been shown to
    fire is a guard that might be matching nothing — and this one's FIRST
    version matched nothing on the mutation it exists for, which is how it was
    found. Both forms below are real: the second is the exact aliased-import
    mutant that survived the call-counting version.
    """
    assert len(_install_sites(
        "self._active_memories = a\n"
        "self._active_memories = b\n")) == 2
    assert len(_install_sites(
        "self._active_memories = (_ft(rows, haystack))\n")) == 1
    # Two of the forms that defeated the sibling guard, caught here for free
    # by parsing rather than pattern-matching a line.
    assert len(_install_sites("self._active_memories, x = a, b\n")) == 1
    assert len(_install_sites("self._active_memories += [m]\n")) == 1
    # The property's own decorator and the setter's `object.__setattr__` are
    # NOT installs and must not be counted, or the guard fires on the very
    # mechanism it protects.
    assert _install_sites(
        "@_active_memories.setter\n"
        "def _active_memories(self, mems):\n"
        "    object.__setattr__(self, self._ACTIVE_MEMORIES_RAW, mems)\n") == []
    # A comparison, a read, and the attribute on something that is not `self`
    # are all not installs.
    assert _install_sites("if self._active_memories == x:\n    pass\n") == []
    assert _install_sites("y = self._active_memories\n") == []
    assert _install_sites("other._active_memories = a\n") == []


async def test_stale_reports_never_used_and_never_archives(tmp_path):
    """`--stale`'s query: NULL counts as stale, a fresh stamp does not, and
    nothing is mutated. Confirmed rows are the operator's — `curator.py` exempts
    them from every automatic action, and this report must not be the exception.
    """
    from no_human.learning import LearningQueue

    async with Store(tmp_path / "nh.db") as store:
        never = await store.add_memory(
            mem_type="rule", title="never used", content="x", confirmed=True)
        used = await store.add_memory(
            mem_type="rule", title="just used", content="x", confirmed=True)
        pending = await store.add_memory(
            mem_type="rule", title="not confirmed", content="x")
        assert await store.touch_memories_used([used]) == 1

        stale = await LearningQueue(store).stale(days=30)
        assert [m["id"] for m in stale] == [never]
        assert pending not in {m["id"] for m in stale}, (
            "an unconfirmed proposal is not a stale rule — it has never been "
            "eligible to be used")

        # days=0 makes even the just-used row stale; the cutoff is real and
        # not an accidental always-true string comparison.
        assert {m["id"] for m in await LearningQueue(store).stale(days=0)} == {
            never, used}

        # READ-ONLY: same rows, same archived flags, afterwards.
        after = await store.list_memories(confirmed=True)
        assert {m["id"] for m in after} == {never, used}
        assert all(not m["archived"] for m in after)


async def test_touch_is_one_statement_per_chunk_not_one_per_id(tmp_path):
    """It runs on the per-attempt hot path behind a serialized write lock, so
    N awaited UPDATEs would be N lock acquisitions between a task being picked
    up and the coder starting. Counted, because "batched" is otherwise a claim
    in a docstring."""
    async with Store(tmp_path / "nh.db") as store:
        ids = [await store.add_memory(mem_type="rule", title=f"r{i}",
                                      content="x", confirmed=True)
               for i in range(25)]
        executed: list[str] = []
        real = store.db.execute

        async def counting(sql, *a, **kw):
            executed.append(sql)
            return await real(sql, *a, **kw)

        store.db.execute = counting
        try:
            assert await store.touch_memories_used(ids) == 25
        finally:
            store.db.execute = real

        updates = [s for s in executed if "last_used_at" in s]
        assert len(updates) == 1, (
            f"{len(updates)} statements for 25 ids — the batch collapsed into "
            f"a per-id loop on a hot path")
        assert await store.touch_memories_used([]) == 0


# --------------------------------------------------------------------------- #
# File signal: the haystack was missing the changed/target files its own       #
# docstring promised, so a memory tagged by file/module could never match.     #
# --------------------------------------------------------------------------- #

async def test_a_file_tagged_rule_fires_only_because_of_the_planned_file_set(tmp_path):
    async with Store(tmp_path / "nh.db") as store:
        file_tagged = await store.add_memory(
            mem_type="rule", title="triggers.py file rule", content="x",
            tags=["src/no_human/learning/triggers.py"], confirmed=True)
        control = await store.add_memory(
            mem_type="rule", title="kafka rule", content="x",
            tags=["kafka"], confirmed=True)

        task = Task.new("Fix the injection rate", repo_path="")
        task.context = {
            "spec": {
                "files_to_change": [
                    "src/no_human/learning/triggers.py — add the file signal",
                ],
            },
        }
        all_scoped, triggered = await _bare_orchestrator(store)._load_active_memories(task)

        assert {m["id"] for m in all_scoped} == {file_tagged, control}, (
            "control: both rules must be FETCHED, or the filter — not the "
            "fetch — is what's under test here")
        assert [m["id"] for m in triggered] == [file_tagged], (
            "the file-tagged rule must fire from the planned file set alone, "
            "and the unrelated kafka rule must stay held")


async def test_the_file_signal_is_the_only_thing_that_could_have_matched(tmp_path):
    """Pins that the match above comes from `files_to_change`, not from
    incidental text — same memory and task, no spec this time."""
    async with Store(tmp_path / "nh.db") as store:
        await store.add_memory(
            mem_type="rule", title="triggers.py file rule", content="x",
            tags=["src/no_human/learning/triggers.py"], confirmed=True)

        task = Task.new("Fix the injection rate", repo_path="")
        task.context = {}
        _, triggered = await _bare_orchestrator(store)._load_active_memories(task)

        assert triggered == [], (
            "with no plan and no path token anywhere in title/description/"
            "criteria, the file-tagged rule must be held — if it still fires "
            "the previous test's match wasn't actually coming from the file "
            "signal")


def test_the_haystack_names_the_planned_files_and_says_so():
    task = Task.new("Refactor the board renderer", repo_path="")
    task.context = {
        "spec": {
            "files_to_change": [
                "web/src/board.tsx",
                "src/no_human/core/db.py",
            ],
        },
    }
    haystack = Orchestrator._trigger_haystack(task)
    assert "web/src/board.tsx" in haystack
    assert "src/no_human/core/db.py" in haystack
    # no regression on the existing signal
    assert "Refactor the board renderer" in haystack

    doc = Orchestrator._trigger_haystack.__doc__ or ""
    import no_human.learning.triggers as triggers_mod
    triggers_doc = triggers_mod.__doc__ or ""
    assert "file" in doc.lower(), (
        "_trigger_haystack's docstring must state that it includes the "
        "file signal")
    assert "file" in triggers_doc.lower(), (
        "triggers.py's module docstring must state that the haystack "
        "includes the file signal")

    src = inspect.getsource(Orchestrator._trigger_haystack)
    assert "named_paths" in src, (
        "the docstring claims a file signal but the function body never "
        "calls named_paths() — a docstring-only claim must fail this test")
