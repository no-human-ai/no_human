"""The opt-in capability-gap channel (issue #20).

Three properties are worth a test here and each one is a different kind of
claim:

* **It is off.** A default install writes nothing and sends nothing, and the
  one switch that changes that is `capability_gap.enabled` plus a destination
  that actually resolves.
* **Nothing but the closed vocabulary can ever travel on it.** Not a
  convention — `_validate` raises, on the way in AND on the way out, and the
  orchestrator hook is written against metadata that is already a closed enum.
  The privacy claim in `docs/CAPABILITY_GAP.md` is only worth as much as the
  guard under it.
* **The table that decides what counts as a capability gap is complete and
  honest.** Every blocker category is classified or deliberately excluded, and
  the one published class the built-in producer cannot emit is pinned as such
  rather than left to be discovered by a consumer who waited for it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from no_human import capability_gap as cg
from no_human import telemetry
from no_human.blockers.taxonomy import BlockerCategory
from no_human.config import DEFAULT_CONFIG
from no_human.core.orchestrator import Orchestrator

DOC = Path(__file__).resolve().parents[1] / "docs" / "CAPABILITY_GAP.md"


def _cfg(tmp_path: Path, **overrides) -> dict:
    """A config whose channel is ON and whose sink is a file in *tmp_path*.

    `instance_pseudonym` is pinned so nothing in these tests reads or writes
    the operator's real ``~/.no_human`` — the one test that exercises the
    on-disk pseudonym relocates ``Path.home`` explicitly.
    """
    section = {
        "enabled": True,
        "sink": "jsonl",
        "path": str(tmp_path / "gaps.jsonl"),
        "endpoint": "",
        "max_lines": 10,
        "instance_pseudonym": "pytest-instance",
        "synthetic": True,
    }
    section.update(overrides)
    return {"capability_gap": section}


def _lines(tmp_path: Path) -> list[dict]:
    path = tmp_path / "gaps.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


# --------------------------------------------------------------------------- #
# It is off
# --------------------------------------------------------------------------- #

def test_the_shipped_default_is_off_and_local():
    section = DEFAULT_CONFIG["capability_gap"]
    assert section["enabled"] is False
    assert section["sink"] == "jsonl"
    assert section["endpoint"] == ""
    assert not cg.enabled(DEFAULT_CONFIG)


def test_a_config_with_no_block_at_all_is_off():
    assert not cg.enabled({})


def test_enabled_needs_a_destination_that_resolves():
    on = {"capability_gap": {"enabled": True, "sink": "http", "endpoint": ""}}
    assert not cg.enabled(on)
    on["capability_gap"]["endpoint"] = "http://example.com/collect"  # not loopback
    assert not cg.enabled(on)
    on["capability_gap"]["endpoint"] = "https://example.com/collect"
    assert cg.enabled(on)


def test_an_unknown_sink_name_disables_the_channel():
    assert not cg.enabled(
        {"capability_gap": {"enabled": True, "sink": "syslog"}})


def test_disabled_writes_nothing(tmp_path):
    config = _cfg(tmp_path, enabled=False)
    cg.record("capacity_blocked", "quota_exhausted",
              constraints={"outcome": "parked"}, config=config)
    assert _lines(tmp_path) == []


@pytest.mark.parametrize("url,ok", [
    ("https://collector.example/ingest", True),
    ("https://collector.example", True),
    ("http://127.0.0.1:9000/ingest", True),
    ("http://localhost:9000/ingest", True),
    ("http://[::1]:9000/ingest", True),
    ("http://collector.example/ingest", False),
    ("http://localhost.evil.example/ingest", False),
    ("file:///etc/passwd", False),
    ("ftp://collector.example/ingest", False),
    ("collector.example/ingest", False),
    ("", False),
    ("https://", False),
])
def test_only_https_or_loopback_http_is_a_destination(url, ok):
    """`urllib.request.urlopen` honours `file://`. A scheme check is the whole
    reason a mistyped endpoint cannot turn this channel into a writer pointed
    at an arbitrary path, and `http://localhost.evil.example` is the near-miss
    a prefix check would have let through."""
    assert cg._valid_endpoint(url) is ok


# --------------------------------------------------------------------------- #
# Nothing but the closed vocabulary
# --------------------------------------------------------------------------- #

def test_the_event_has_exactly_these_fields_and_no_others(tmp_path):
    cg.record("capacity_blocked", "quota_exhausted", config=_cfg(tmp_path),
              constraints={"blocker_category": "QUOTA", "outcome": "parked",
                           "attempts_bucket": "2-5", "task_kind": "feature",
                           "backend": "claude"})
    [event] = _lines(tmp_path)
    assert set(event) == set(cg._EVENT_KEYS)
    assert event["schema"] == cg.SCHEMA
    assert event["capability_class"] == "capacity_blocked"
    assert event["reason_code"] == "quota_exhausted"
    assert event["synthetic"] is True
    assert set(event["source"]) == set(cg._SOURCE_KEYS)
    assert event["source"]["product"] == "no_human"
    assert event["source"]["instance"] == "pytest-instance"
    assert set(event["constraints"]) <= set(cg.CONSTRAINT_VALUES)


@pytest.mark.parametrize("kwargs", [
    {"capability_class": "not_a_class", "reason_code": "timeout"},
    {"capability_class": "capacity_blocked", "reason_code": "not_a_reason"},
    {"capability_class": "capacity_blocked", "reason_code": "timeout",
     "constraints": {"ticket_title": "fix the thing"}},
    {"capability_class": "capacity_blocked", "reason_code": "timeout",
     "constraints": {"outcome": "exploded"}},
    {"capability_class": "capacity_blocked", "reason_code": "timeout",
     "constraints": {"blocker_category": "NOVEL_UNKNOWN"}},
])
def test_anything_outside_the_closed_sets_raises(tmp_path, kwargs):
    """An unlisted field is a privacy bug, not an operational hiccup — so it
    raises rather than being dropped, and it raises even while the channel is
    DISABLED, which is exactly where such a bug would otherwise hide."""
    constraints = kwargs.pop("constraints", None)
    with pytest.raises(ValueError):
        cg.record(kwargs["capability_class"], kwargs["reason_code"],
                  constraints=constraints, config=_cfg(tmp_path, enabled=False))
    assert _lines(tmp_path) == []


def test_record_never_raises_for_an_unusable_sink(tmp_path):
    """Fail-open everywhere except the vocabulary: a path that cannot be
    written must not take the task run down with it."""
    blocked = tmp_path / "gaps.jsonl"
    blocked.mkdir()  # a directory where the spool file should be
    cg.record("budget_exhausted", "budget_exhausted",
              constraints={"outcome": "failed"},
              config=_cfg(tmp_path))


def test_sendable_rejects_a_hand_edited_line():
    good = cg.build_event(
        "authorization_blocked", "access_denied",
        constraints={"outcome": "escalated"}, instance="i", synthetic=True,
        version="0.0.0")
    assert cg._sendable(good)
    assert not cg._sendable({**good, "detail": "the agent said this"})
    assert not cg._sendable({**good, "capability_class": "invented"})
    assert not cg._sendable({**good, "constraints": {"task_title": "x"}})
    assert not cg._sendable({**good, "schema": "no_human.capability_gap/v2"})
    assert not cg._sendable({**good, "synthetic": "yes"})
    assert not cg._sendable({**good, "source": {"product": "no_human"}})
    assert not cg._sendable({k: v for k, v in good.items() if k != "ts"})
    assert not cg._sendable("not a dict")


# --------------------------------------------------------------------------- #
# The classification table
# --------------------------------------------------------------------------- #

#: The two categories the table deliberately does not classify, and why. A
#: NEW category must be added to `_CATEGORY_MAP` or to this set — the test
#: below refuses to let one appear in neither and silently emit nothing.
_DELIBERATELY_UNCLASSIFIED = {
    "NOVEL_UNKNOWN": "unclassified by construction — no named cause",
    "USER_PAUSED": "a human stopped the task; nothing was missing",
}


def test_every_blocker_category_is_classified_or_deliberately_excluded():
    live = {member.value for member in BlockerCategory}
    accounted = set(cg._CATEGORY_MAP) | set(_DELIBERATELY_UNCLASSIFIED)
    assert live - accounted == set(), (
        "a blocker category exists that this channel neither classifies nor "
        "deliberately excludes — it would silently emit nothing")
    assert accounted - live == set(), (
        "the table names a blocker category that no longer exists")


def test_every_mapping_target_is_inside_the_published_vocabulary():
    for capability_class, reason_code in (
            list(cg._CATEGORY_MAP.values()) + list(cg._REASON_MAP.values())):
        assert capability_class in cg.CAPABILITY_CLASSES
        assert reason_code in cg.REASON_CODES


def test_the_reason_fallback_only_names_real_failure_categories():
    """`_REASON_MAP`'s keys come from `telemetry.FAILURE_REASON_CATEGORIES`.
    A typo there would be a mapping that can never fire."""
    assert set(cg._REASON_MAP) <= telemetry.FAILURE_REASON_CATEGORIES


@pytest.mark.parametrize("reason", sorted(
    telemetry.FAILURE_REASON_CATEGORIES - {"infra", "budget_exhausted"}))
def test_an_ordinary_failure_is_not_a_capability_gap(reason):
    """The non-goal issue #20 states in as many words: a red test, a rejected
    diff, a tamper block or an exhausted attempt cap is not capability
    demand."""
    assert cg.classify(None, reason) is None


def test_classify_prefers_the_blocker_category_over_the_reason():
    assert cg.classify("QUOTA", "infra") == ("capacity_blocked", "quota_exhausted")


def test_classify_never_guesses():
    assert cg.classify("SOMETHING_NEW", "also_new") is None
    assert cg.classify(None, None) is None
    assert cg.classify("", "") is None


def test_classify_tolerates_the_spellings_a_caller_can_produce():
    assert cg.classify(" quota ", None) == ("capacity_blocked", "quota_exhausted")
    assert cg.classify(None, " INFRA ") == (
        "capability_unavailable", "backend_unavailable")


def test_verification_unavailable_is_published_but_not_derivable():
    """Both halves of the honest gap `docs/CAPABILITY_GAP.md` states.

    The class is accepted from any producer, and the built-in derivation
    cannot currently reach it: a review gate that could not RUN arrives as an
    ordinary TRANSIENT_INFRA/NOVEL_UNKNOWN blocker. When the harness grows a
    structural signal for it, this test goes red and the doc gets corrected
    — which is the point of pinning a gap rather than describing it.
    """
    assert "verification_unavailable" in cg.CAPABILITY_CLASSES
    derivable = {klass for klass, _ in
                 list(cg._CATEGORY_MAP.values()) + list(cg._REASON_MAP.values())}
    assert "verification_unavailable" not in derivable
    assert cg.CAPABILITY_CLASSES - derivable == {"verification_unavailable"}, (
        "another class became underivable, or this one became derivable — "
        "docs/CAPABILITY_GAP.md's coverage paragraph is now wrong")


@pytest.mark.parametrize("n,bucket", [
    (-1, "0"), (0, "0"), (1, "1"), (2, "2-5"), (5, "2-5"), (6, "6+"), (99, "6+"),
])
def test_attempt_counts_are_bucketed(n, bucket):
    assert cg.attempt_bucket(n) == bucket
    assert bucket in cg.ATTEMPT_BUCKETS


def test_unknown_backends_and_kinds_collapse_to_other():
    assert cg.normalize_backend("Claude") == "claude"
    assert cg.normalize_backend("gemini") == "other"
    assert cg.normalize_backend(None) == "other"
    assert cg.normalize_task_kind("bugfix") == "bugfix"
    assert cg.normalize_task_kind("whatever the client invented") == "other"
    assert cg.normalize_task_kind(None) == "other"


# --------------------------------------------------------------------------- #
# The pseudonym
# --------------------------------------------------------------------------- #

def test_the_pseudonym_is_stable_and_is_not_telemetrys(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    first = cg.instance_pseudonym({})
    assert first == cg.instance_pseudonym({})
    assert (tmp_path / ".no_human" / "capability-gap-id").is_file()
    # It lives beside config.yaml, never inside it — so it never reaches
    # /api/config's echo, and a recipient of one channel cannot join it to
    # the other.
    assert not (tmp_path / ".no_human" / "config.yaml").exists()
    telemetry_section = {"instance_id": ""}
    assert telemetry.ensure_instance_id(telemetry_section) != first


def test_a_configured_pseudonym_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert cg.instance_pseudonym({"instance_pseudonym": "pilot-7"}) == "pilot-7"
    assert not (tmp_path / ".no_human" / "capability-gap-id").exists()


def test_synthetic_is_forced_by_config_and_derived_otherwise():
    assert cg.is_synthetic({"synthetic": True}) is True
    assert cg.is_synthetic({"synthetic": False}) is False
    # Under pytest `telemetry.environment()` is "test", never "real".
    assert cg.is_synthetic({}) is True
    assert cg.is_synthetic({"synthetic": None}) is True


# --------------------------------------------------------------------------- #
# The spool
# --------------------------------------------------------------------------- #

def test_the_spool_is_compacted_once_it_outgrows_the_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(cg, "COMPACT_AT_BYTES", 400)
    config = _cfg(tmp_path, max_lines=3)
    for _ in range(12):
        cg.record("capability_unavailable", "backend_unavailable",
                  constraints={"outcome": "parked"}, config=config)
    kept = _lines(tmp_path)
    assert 0 < len(kept) <= 3
    assert all(event["schema"] == cg.SCHEMA for event in kept)


@pytest.mark.parametrize("raw", [None, "", "0", -1, "nonsense", True])
def test_a_nonsense_max_lines_falls_back_to_the_default(raw):
    assert cg._max_lines({"max_lines": raw}) == cg.DEFAULT_MAX_LINES


def test_flush_is_a_no_op_for_the_local_sink(tmp_path):
    config = _cfg(tmp_path)
    cg.record("capability_unavailable", "backend_unavailable",
              constraints={"outcome": "parked"}, config=config)
    assert cg.flush(config=config) == 0
    assert len(_lines(tmp_path)) == 1, "a jsonl sink must not drain itself"


def test_flush_posts_and_removes_only_what_it_took(tmp_path, monkeypatch):
    config = _cfg(tmp_path, sink="http", endpoint="https://collector.example/x")
    # `record` on an http sink normally kicks off a background flush. Silenced
    # here so this test drives `flush` itself, rather than racing a daemon
    # thread that would reach the real network before the stub is installed.
    monkeypatch.setattr(cg, "_spawn_flush", lambda section: None)
    for _ in range(2):
        cg.record("authorization_blocked", "access_denied",
                  constraints={"outcome": "escalated"}, config=config)
    posted: list[dict] = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(req, timeout=None):
        posted.append({"url": req.full_url, "body": json.loads(req.data)})
        return _Response()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    assert cg.flush(config=config) == 2
    assert posted[0]["url"] == "https://collector.example/x"
    assert posted[0]["body"]["schema"] == cg.SCHEMA
    assert len(posted[0]["body"]["events"]) == 2
    assert _lines(tmp_path) == []


def test_a_spool_of_nothing_sendable_is_dropped_not_posted(tmp_path, monkeypatch):
    """A poisoned head must not wedge every later flush behind lines that can
    never ship — the failure mode `telemetry.flush` documents for its own
    queue, reproduced here for this one."""
    config = _cfg(tmp_path, sink="http", endpoint="https://collector.example/x")
    (tmp_path / "gaps.jsonl").write_text(
        "not json at all\n" + json.dumps({"schema": "other", "x": 1}) + "\n",
        encoding="utf-8")

    def _explode(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("nothing sendable, so nothing may be POSTed")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _explode)
    assert cg.flush(config=config) == 0
    assert _lines(tmp_path) == []


def test_a_failed_post_leaves_the_spool_intact(tmp_path, monkeypatch):
    config = _cfg(tmp_path, sink="http", endpoint="https://collector.example/x")
    monkeypatch.setattr(cg, "_spawn_flush", lambda section: None)
    cg.record("budget_exhausted", "budget_exhausted",
              constraints={"outcome": "failed"}, config=config)

    def _refuse(*args, **kwargs):
        raise OSError("collector is down")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _refuse)
    assert cg.flush(config=config) == 0
    assert len(_lines(tmp_path)) == 1


# --------------------------------------------------------------------------- #
# The orchestrator hook
# --------------------------------------------------------------------------- #

def _orchestrator(config: dict) -> Orchestrator:
    """The hook under test and nothing else. `Orchestrator.__new__` skips
    `__init__` (a real one needs a store, a backend and a notifier); every
    attribute the hook touches has a class-level default or is set here."""
    orch = Orchestrator.__new__(Orchestrator)
    orch.config = config
    orch._sink = lambda event: None
    return orch


def test_a_parked_quota_task_emits_one_gap(tmp_path):
    orch = _orchestrator(_cfg(tmp_path))
    orch.emit("kind", "task kind: feature", task_kind="feature")
    orch.emit("attempt_start", "attempt 1/3")
    orch.emit("attempt_start", "attempt 2/3")
    orch.emit("paused_quota", "weekly limit reached", status="paused_quota",
              blocker_category="QUOTA")
    [event] = _lines(tmp_path)
    assert event["capability_class"] == "capacity_blocked"
    assert event["reason_code"] == "quota_exhausted"
    assert event["constraints"] == {
        "blocker_category": "QUOTA", "outcome": "parked",
        "attempts_bucket": "2-5", "task_kind": "feature", "backend": "other",
    }


def test_the_hook_never_copies_the_blockers_prose(tmp_path):
    """`blocker` rides on the very same emit and carries the agent's own
    words. One `.get("blocker")` here would put free text on a wire whose
    entire guarantee is that there is none."""
    secret = "ACME-1234: rotate the customer's production credential"
    orch = _orchestrator(_cfg(tmp_path))
    orch.emit("escalated", secret, status="escalated",
              blocker_category="MISSING_ACCESS",
              blocker={"question": secret, "evidence": [secret]})
    [event] = _lines(tmp_path)
    assert secret not in json.dumps(event)
    assert event["capability_class"] == "authorization_blocked"
    assert event["constraints"]["outcome"] == "escalated"


@pytest.mark.parametrize("kind,meta", [
    # Unclassified, and a cancel: neither is a capability gap.
    ("escalated", {"blocker_category": "NOVEL_UNKNOWN"}),
    ("blocked", {"blocker_category": "USER_PAUSED"}),
    ("cancelled_hard", {"status": "failed"}),
    # Ordinary failures of the change itself.
    ("failed", {"reason_category": "review_failed"}),
    ("failed", {"reason_category": "max_attempts"}),
    ("failed", {"reason_category": "tamper_blocked"}),
    # Mid-run progress is not an off-ramp at all.
    ("state", {"status": "implementing"}),
    ("pr_open", {"status": "awaiting_approval"}),
])
def test_these_emits_are_not_capability_gaps(tmp_path, kind, meta):
    orch = _orchestrator(_cfg(tmp_path))
    orch.emit(kind, "detail", **meta)
    assert _lines(tmp_path) == []


def test_a_terminal_budget_failure_is_a_gap(tmp_path):
    orch = _orchestrator(_cfg(tmp_path))
    orch.emit("failed", "budget spent", status="failed",
              blocker_category="BUDGET_EXHAUSTED",
              reason_category="budget_exhausted")
    [event] = _lines(tmp_path)
    assert event["capability_class"] == "budget_exhausted"
    assert event["constraints"]["outcome"] == "failed"


def test_an_off_ramp_without_a_blocker_falls_back_to_the_reason(tmp_path):
    orch = _orchestrator(_cfg(tmp_path))
    orch.emit("failed", "the SDK died", status="failed", reason_category="infra")
    [event] = _lines(tmp_path)
    assert event["capability_class"] == "capability_unavailable"
    assert event["reason_code"] == "backend_unavailable"
    assert "blocker_category" not in event["constraints"]


def test_the_hook_survives_metadata_it_did_not_expect(tmp_path):
    """Fail-open by construction: `emit` is called from every off-ramp and a
    raise here would take the off-ramp down with it."""
    orch = _orchestrator(_cfg(tmp_path))
    orch.emit("blocked", "x", blocker_category=object(), reason_category=17)
    orch.emit("kind", "x", task_kind=None)
    orch.emit("escalated", "x", blocker_category=["QUOTA"])
    assert _lines(tmp_path) == []


def test_the_hook_is_silent_on_a_default_install(tmp_path):
    """The whole point, restated at the integration boundary: a real off-ramp
    on a default config writes nothing."""
    spool = tmp_path / "gaps.jsonl"
    orch = _orchestrator({
        **DEFAULT_CONFIG,
        # The OTHER channel is default-ON and would queue (and flush) a real
        # `task_ended` from this very emit. Off here so this test exercises
        # only the subject it names.
        "telemetry": {**DEFAULT_CONFIG["telemetry"], "enabled": False},
        "capability_gap": {**DEFAULT_CONFIG["capability_gap"],
                           "path": str(spool)},
    })
    orch.emit("paused_quota", "weekly limit", status="paused_quota",
              blocker_category="QUOTA")
    assert not spool.exists()


def test_the_backend_travels_when_the_attempt_resolved_one(tmp_path):
    orch = _orchestrator(_cfg(tmp_path))
    orch._attempt_backend = "codex"
    orch.emit("blocked", "infra", status="blocked",
              blocker_category="TRANSIENT_INFRA")
    [event] = _lines(tmp_path)
    assert event["constraints"]["backend"] == "codex"


def test_the_two_channels_keep_their_own_books(tmp_path):
    """`_gap_attempts` is the capability channel's own counter. If it read
    `_telemetry_hook`'s `_tel_attempts`, a change made for one channel's wire
    contract would silently move the other's."""
    orch = _orchestrator(_cfg(tmp_path))
    orch.emit("attempt_start", "attempt 1")
    orch._tel_attempts = 99
    orch.emit("blocked", "infra", status="blocked",
              blocker_category="DEPENDENCY_WAIT")
    [event] = _lines(tmp_path)
    assert event["constraints"]["attempts_bucket"] == "1"


def test_the_outcome_table_covers_only_real_off_ramp_kinds():
    """A typo in `_CAPABILITY_GAP_OUTCOMES` is a trigger that never fires.

    Cross-checked against `_TASK_END_KINDS`, which the orchestrator already
    maintains for the telemetry channel: every trigger is one of the kinds
    that module recognises as an off-ramp, plus `"failed"` (which reaches
    telemetry by its own earlier branch and so is not in that tuple).
    """
    from no_human.core import orchestrator as orch_mod

    triggers = set(orch_mod._CAPABILITY_GAP_OUTCOMES)
    assert set(orch_mod._CAPABILITY_GAP_OUTCOMES.values()) <= cg.OUTCOMES
    assert triggers - {"failed"} <= set(orch_mod._TASK_END_KINDS), (
        "a trigger names a kind the orchestrator does not treat as an "
        "off-ramp at all")
    # A human stop is not a capability gap, in either spelling.
    assert not triggers & {"cancelled", "cancelled_hard"}


# --------------------------------------------------------------------------- #
# The doc and the code agree, in both directions
# --------------------------------------------------------------------------- #

def _doc_section(heading: str) -> str:
    """The BODY of a section — the heading line itself is dropped, since it
    backticks the field name and that is not one of the field's values."""
    text = DOC.read_text(encoding="utf-8")
    start = text.index(heading)
    body = text.index("\n", start) + 1
    nxt = text.find("\n## ", body)
    return text[body:] if nxt == -1 else text[body:nxt]


def _backticked(text: str) -> set[str]:
    return set(re.findall(r"`([A-Za-z0-9_+\-]+)`", text))


def test_every_capability_class_is_documented_and_no_phantoms():
    documented = _backticked(_doc_section("## `capability_class`"))
    assert documented & cg.CAPABILITY_CLASSES == cg.CAPABILITY_CLASSES
    assert documented - cg.CAPABILITY_CLASSES == set(), (
        "docs/CAPABILITY_GAP.md names a capability class the code does not "
        "have")


def test_every_reason_code_is_documented_and_no_phantoms():
    documented = _backticked(_doc_section("## `reason_code`"))
    assert documented == cg.REASON_CODES


def test_every_constraint_key_and_value_is_documented_and_no_phantoms():
    documented = _backticked(_doc_section("## `constraints`"))
    expected = set(cg.CONSTRAINT_VALUES)
    for values in cg.CONSTRAINT_VALUES.values():
        expected |= values
    # `ValueError` is prose in the same section, not part of the vocabulary.
    assert documented - {"ValueError"} == expected


def test_the_doc_states_the_schema_and_the_off_default():
    text = DOC.read_text(encoding="utf-8")
    assert cg.SCHEMA in text
    assert "off by default" in text
