"""Fix-pair ledger (0013): "this machine hit this exact error before — and
here is what overcame it."

main-6cec2140 put 12 of 26 bench failures in the burn-then-quit class: real
work, then an escalation on a deliverable task. These tests pin the three
moving pieces: a failed attempt records its signature as open friction, a
SUCCESS terminal resolves that task's open friction into fix pairs, and a
LATER task failing on the same signature is handed the resolution as history
("evidence, not a guaranteed fix") — never as an instruction, never
load-bearing.
"""

import pytest

from no_human.config import load_config
from no_human.core.bounds import error_signature
from no_human.core.orchestrator import Orchestrator, TaskOutcome
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier


# ── store layer ─────────────────────────────────────────────────────────── #

async def test_schema_has_the_fix_pairs_table(store):
    cols = {r["name"] for r in await store._fetchall("PRAGMA table_info(fix_pairs)")}
    assert cols >= {"id", "sig", "repo_path", "error_excerpt", "task_id",
                    "resolution", "resolved_task_id", "created_at",
                    "resolved_at"}


async def test_record_friction_dedupes_open_rows_per_sig_and_task(store):
    """The SAME error failing the SAME task twice is the StuckDetector's
    business — one open row already says it. A different task hitting the
    same sig IS new friction."""
    sig = error_signature("ModuleNotFoundError: No module named 'redis'")
    first = await store.record_friction(sig=sig, task_id="t1",
                                        error_excerpt="ModuleNotFoundError")
    dup = await store.record_friction(sig=sig, task_id="t1",
                                      error_excerpt="ModuleNotFoundError")
    other = await store.record_friction(sig=sig, task_id="t2",
                                        error_excerpt="ModuleNotFoundError")
    assert first and other and dup is None
    rows = await store._fetchall("SELECT task_id FROM fix_pairs WHERE sig = ?",
                                 (sig,))
    assert {r["task_id"] for r in rows} == {"t1", "t2"}


async def test_resolution_turns_friction_into_a_findable_fix_pair(store):
    """Unresolved friction is invisible to lookup (nothing overcame it yet);
    resolution makes it findable — and never for the task that hit it."""
    sig = error_signature("error: connection refused on port 5432")
    await store.record_friction(sig=sig, task_id="t1", error_excerpt="conn refused",
                                repo_path="/repo/a")
    assert await store.find_fix_pair(sig) is None

    n = await store.resolve_friction("t1", resolution="started the local pg service")
    assert n == 1
    hit = await store.find_fix_pair(sig)
    assert hit and hit["resolution"] == "started the local pg service"
    # a task is never handed its own history as independent evidence
    assert await store.find_fix_pair(sig, exclude_task_id="t1") is None


async def test_find_fix_pair_prefers_the_same_repo(store):
    sig = error_signature("assertion failed: tamper guard tripped")
    await store.record_friction(sig=sig, task_id="tA", error_excerpt="e",
                                repo_path="/repo/a")
    await store.record_friction(sig=sig, task_id="tB", error_excerpt="e",
                                repo_path="/repo/b")
    await store.resolve_friction("tA", resolution="fix from repo a")
    await store.resolve_friction("tB", resolution="fix from repo b")

    hit = await store.find_fix_pair(sig, repo_path="/repo/b")
    assert hit["resolution"] == "fix from repo b"
    hit_other = await store.find_fix_pair(sig, repo_path="/repo/zzz")
    assert hit_other is not None    # any-repo fallback still answers


async def test_resolve_friction_fills_open_rows_only(store):
    """A resumed task's NEW friction gets its own later resolution; rows
    already resolved keep their original text (same discipline as
    memory_uses.task_outcome)."""
    sig1 = error_signature("first failure shape")
    await store.record_friction(sig=sig1, task_id="t1", error_excerpt="one")
    await store.resolve_friction("t1", resolution="first resolution")
    sig2 = error_signature("second, different failure")
    await store.record_friction(sig=sig2, task_id="t1", error_excerpt="two")
    n = await store.resolve_friction("t1", resolution="second resolution")
    assert n == 1
    rows = await store._fetchall(
        "SELECT sig, resolution FROM fix_pairs WHERE task_id = 't1'")
    by_sig = {r["sig"]: r["resolution"] for r in rows}
    assert by_sig[sig1] == "first resolution"
    assert by_sig[sig2] == "second resolution"


# ── orchestrator terminal wiring ────────────────────────────────────────── #

def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    return cfg


async def test_success_terminal_resolves_the_tasks_open_friction(store, tmp_path):
    """The same finalizer that stamps memory_uses.task_outcome fills the
    task's open friction on a SUCCESS terminal, carrying the stuck-diagnosis
    when one was recorded."""
    orch = Orchestrator(store, _config(tmp_path).data, object(),
                        SlackNotifier(None))
    t = Task.new("fix the thing", repo_path="/repo/x")
    t.context = {"stuck_hypothesis": "root cause: stale lockfile",
                 "attempt_log": ["attempt 1: tests failed"]}
    await store.create_task(t)
    sig = error_signature("tests failed: 1 failed, 3 passed")
    await store.record_friction(sig=sig, task_id=t.id, error_excerpt="tests failed",
                                repo_path="/repo/x")

    await orch._finalize_memory_use_outcome(
        t, TaskOutcome(t, status=TaskStatus.DONE, detail=""))

    hit = await store.find_fix_pair(sig)
    assert hit is not None
    assert "stale lockfile" in hit["resolution"]
    assert t.id[:8] in hit["resolution"]


async def test_failure_terminal_leaves_friction_open(store, tmp_path):
    """A FAILED terminal did not overcome anything — its friction stays open
    (and therefore unfindable), so failures can never masquerade as fixes."""
    orch = Orchestrator(store, _config(tmp_path).data, object(),
                        SlackNotifier(None))
    t = Task.new("fix the thing", repo_path="/repo/x")
    await store.create_task(t)
    sig = error_signature("some hard failure")
    await store.record_friction(sig=sig, task_id=t.id, error_excerpt="hard")

    await orch._finalize_memory_use_outcome(
        t, TaskOutcome(t, status=TaskStatus.FAILED, detail="gave up"))

    assert await store.find_fix_pair(sig) is None


# ── orchestrator seams ──────────────────────────────────────────────────── #
#
# The store layer above can be perfect while the feature is dead: the value is
# created at two seams in the orchestrator — the prompt that CARRIES the
# remembered fix, and the screen that decides whether it may. Both are pinned
# here, each with a negative control, because an earlier version of this file
# tested neither: removing the injection and the lookup left every test green.

def _orch_for_prompt():
    """Minimal orchestrator for `_build_implement_prompt` — same idiom as
    tests/test_attempt_state_distill.py."""
    orch = object.__new__(Orchestrator)
    orch.config = {}
    orch.ci_runner = None
    orch._active_profile = None
    orch._active_memories = None
    return orch


def test_a_remembered_fix_rides_into_the_next_attempts_prompt():
    """The seam the whole ledger exists for. With a prior_fix in context the
    retry prompt carries the resolution as EVIDENCE; without one the same
    prompt does not — so this cannot pass by matching boilerplate."""
    t = Task.new("fix the widget", repo_path="/tmp/repo")
    t.status = TaskStatus.IMPLEMENTING
    t.context = {
        "attempt_log": ["attempt 1: ImportError: no module named widget"],
        "prior_fix": {
            "task_id": "abcdef1234",
            "resolution": "overcome by task abcdef12 — reinstalled the venv",
            "resolved_at": "2026-08-18T00:00:00",
        },
    }
    orch = _orch_for_prompt()
    with_fix = orch._build_implement_prompt(t, "/tmp/repo", attempt_n=2)
    assert "OVERCOME BEFORE" in with_fix
    assert "reinstalled the venv" in with_fix
    assert "abcdef12" in with_fix
    # framing: history, never an instruction
    assert "not a guaranteed fix" in with_fix

    t.context = {"attempt_log": t.context["attempt_log"]}
    without = orch._build_implement_prompt(t, "/tmp/repo", attempt_n=2)
    assert "OVERCOME BEFORE" not in without
    assert "reinstalled the venv" not in without


def test_a_first_attempt_never_carries_a_remembered_fix():
    """No failure yet means no debug preamble at all — a first attempt's
    prompt must be byte-identical whether or not the ledger has a hit."""
    t = Task.new("fix the widget", repo_path="/tmp/repo")
    t.status = TaskStatus.IMPLEMENTING
    orch = _orch_for_prompt()
    clean = orch._build_implement_prompt(t, "/tmp/repo", attempt_n=1)
    t.context = {"prior_fix": {"task_id": "abcdef1234",
                               "resolution": "reinstalled the venv",
                               "resolved_at": "2026-08-18T00:00:00"}}
    assert orch._build_implement_prompt(t, "/tmp/repo", attempt_n=1) == clean


def test_a_remembered_fix_carrying_a_screened_term_is_held_back(tmp_path):
    """A fix pair crosses tasks AND repos, so it is a text channel into the
    coder prompt exactly like an injected memory — and is screened by the same
    matcher. The control: the same text without the term rides."""
    from no_human.eval import vendor_terms

    orch = object.__new__(Orchestrator)
    orch.config = {}
    terms = sorted(getattr(vendor_terms, "BANNED_TERMS", []) or [])
    if not terms:
        pytest.skip("no banned terms in this install to screen with")
    tainted = f"overcome by task abcdef12 — patched the {terms[0]} adapter"
    assert orch._fix_pair_holds_terms(tainted)
    assert not orch._fix_pair_holds_terms(
        "overcome by task abcdef12 — reinstalled the venv")


async def test_the_diagnosis_lands_only_on_the_signature_it_describes(store, tmp_path):
    """A task that hit error A then error B has ONE diagnosis, of B. Writing
    it onto A too would present B's diagnosis to a future task as "THIS EXACT
    ERROR SIGNATURE WAS OVERCOME BEFORE" — a false pairing. A keeps the plain
    resolution; only B carries the diagnosis."""
    orch = Orchestrator(store, _config(tmp_path).data, object(),
                        SlackNotifier(None))
    t = Task.new("fix the thing", repo_path="/repo/x")
    await store.create_task(t)
    sig_a = error_signature("ConnectionRefusedError: port 5432")
    sig_b = error_signature("ImportError: no module named widget")
    await store.record_friction(sig=sig_a, task_id=t.id, error_excerpt="A",
                                repo_path="/repo/x")
    await store.record_friction(sig=sig_b, task_id=t.id, error_excerpt="B",
                                repo_path="/repo/x")
    t.context = {"stuck_hypothesis": "the venv was stale",
                 "attempt_log": ["attempt 1: A", "attempt 2: B"]}
    await store.update_task(t)

    await orch._finalize_memory_use_outcome(
        t, TaskOutcome(t, status=TaskStatus.DONE, detail=""))

    a = await store.find_fix_pair(sig_a)
    b = await store.find_fix_pair(sig_b)
    assert a is not None and b is not None
    assert "the venv was stale" in b["resolution"]
    assert "the venv was stale" not in a["resolution"]
    # both still say, truthfully, that this task overcame them
    assert t.id[:8] in a["resolution"] and t.id[:8] in b["resolution"]


async def test_the_attempt_count_in_a_resolution_is_marked_as_a_floor(store, tmp_path):
    """`attempt_log` keeps the last 3 entries, so the count saturates. A
    saturated count is reported as "3+", never as a flat 3 that reads like the
    whole history."""
    orch = Orchestrator(store, _config(tmp_path).data, object(),
                        SlackNotifier(None))
    t = Task.new("fix the thing", repo_path="/repo/x")
    await store.create_task(t)
    sig = error_signature("boom")
    await store.record_friction(sig=sig, task_id=t.id, error_excerpt="boom")
    t.context = {"attempt_log": ["a", "b", "c"]}
    await store.update_task(t)

    await orch._finalize_memory_use_outcome(
        t, TaskOutcome(t, status=TaskStatus.DONE, detail=""))

    hit = await store.find_fix_pair(sig)
    assert "3+ failed attempt(s)" in hit["resolution"]


async def test_the_producer_seam_records_looks_up_and_screens(store, tmp_path):
    """The seam the review deleted to prove it was untested: 58 lines whose
    removal left the full 9,006-test suite green. It records this failure's
    signature, finds a PAST task's resolution for the same signature, and puts
    it in context for the next attempt."""
    orch = Orchestrator(store, _config(tmp_path).data, object(), SlackNotifier(None))
    detail = "ConnectionRefusedError: [Errno 111] port 5432"
    sig = error_signature(detail)

    past = Task.new("earlier task", repo_path="/repo/other")
    await store.create_task(past)
    await store.record_friction(sig=sig, task_id=past.id, error_excerpt=detail,
                                repo_path="/repo/other")
    await store.resolve_friction(past.id, resolution="started the local pg service")

    now = Task.new("this task", repo_path="/repo/x")
    await store.create_task(now)
    await orch._record_and_lookup_fix_pair(now, detail)

    assert now.context.get("prior_fix"), "the seam did not hand the task its history"
    assert now.context["prior_fix"]["resolution"] == "started the local pg service"
    assert now.context["prior_fix"]["task_id"] == past.id
    # and it recorded THIS task's friction, so a later task inherits from it too
    rows = await store._fetchall(
        "SELECT task_id FROM fix_pairs WHERE sig = ?", (sig,))
    assert {r["task_id"] for r in rows} == {past.id, now.id}


async def test_the_producer_seam_screens_the_resolution_before_context(store, tmp_path):
    """The leak fix, asserted where it runs — not on the matcher helper. A
    resolution carrying a screened term never reaches the task's context, and
    the operator is told which row was held."""
    from no_human.eval import vendor_terms

    terms = sorted(getattr(vendor_terms, "BANNED_TERMS", []) or [])
    if not terms:
        pytest.skip("no banned terms in this install to screen with")
    events = []
    orch = Orchestrator(store, _config(tmp_path).data, object(), SlackNotifier(None))
    orch._sink = lambda e: events.append(e)

    detail = "ImportError: no module named widget"
    sig = error_signature(detail)
    past = Task.new("earlier task", repo_path="/repo/other")
    await store.create_task(past)
    await store.record_friction(sig=sig, task_id=past.id, error_excerpt=detail,
                                repo_path="/repo/other")
    await store.resolve_friction(
        past.id, resolution=f"patched the {terms[0]} adapter and reran")

    now = Task.new("this task", repo_path="/repo/x")
    await store.create_task(now)
    await orch._record_and_lookup_fix_pair(now, detail)

    assert "prior_fix" not in (now.context or {}), (
        "a screened resolution reached the task context — the leak channel is open")
    held = [e for e in events if getattr(e, "kind", getattr(e, "type", "")) == "prior_fix_held"
            or "prior_fix_held" in str(e)]
    assert held, f"nothing told the operator a fix was held: {events}"
    assert past.id[:8] in str(held[0]), "the held event must name the row it held"


async def test_the_producer_seam_drops_a_stale_prior_fix(store, tmp_path):
    """Attempt 2 failed on a DIFFERENT signature than attempt 1. The earlier
    attempt's hit must not ride into this retry's preamble as if it described
    this failure."""
    orch = Orchestrator(store, _config(tmp_path).data, object(), SlackNotifier(None))
    now = Task.new("this task", repo_path="/repo/x")
    now.context = {"prior_fix": {"task_id": "deadbeef99",
                                 "resolution": "an answer to a different error",
                                 "resolved_at": "2026-08-18T00:00:00"}}
    await store.create_task(now)

    await orch._record_and_lookup_fix_pair(now, "TypeError: unrelated failure")

    assert "prior_fix" not in (now.context or {})
