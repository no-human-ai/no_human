"""no-human-67: the independent reviewer's checklist (verdict, rounds, every
finding with severity and file:line) is posted ONCE as its own PR comment —
before this, it only lived in the DB and the PR body's folded one-row
summary. `_review_checklist_comment` renders the body; `_post_review_
checklist_comment` posts it idempotently via `post_to_pr_once`; `_finalize`
wires the call after the PR is opened.

Two prior attempts on this ticket FAILED independent review:

* attempt #1 monkeypatched `_review_checklist_comment` itself (and emptied
  `REVIEW_CHECKLIST_MARKER`) to prove a banned term is refused — which never
  exercised the real scrub path at all. `test_a_banned_term_is_redacted_by_
  the_real_scrub_before_it_reaches_the_forge` below drives the REAL
  `_review_checklist_comment` -> real `post_to_pr` -> real `scrub_outbound`
  chain, stubbing only the process boundary (`subprocess.run`/marker lookup),
  never the orchestrator's own new methods.
* attempt #2 built `_severity_cell` from `item['severity'].strip().lower()`
  with NO `_inline_cell` guard, unlike label/file/note — and left the
  `_finalize` call-site wiring (the checklist decode, the `"rounds" in
  review_verdict` guard, the actual post-PR-open invocation) completely
  untested. `test_severity_survives_the_same_injection_label_does` and the
  `_finalize`-driven tests below close both holes.
"""

import json

import pytest

from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.eval.vendor_terms import BANNED_TERMS
from no_human.notify.slack import SlackNotifier
from no_human.vcs import comment_poster
from no_human.vcs.git import GitRepo

from .test_pr_body_truthfulness import _Commit, _git, _Result

MARKER = Orchestrator.REVIEW_CHECKLIST_MARKER


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


def _orch(store, tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


def _term(i: int) -> str:
    return BANNED_TERMS[i % len(BANNED_TERMS)]


def _item(label="a finding", passed=False, severity="high", file="src/x.py",
         line=12, comment="do this", evidence=""):
    return {"label": label, "passed": passed, "evidence": evidence, "file": file,
            "line": line, "comment": comment, "severity": severity}


def _decision(passed=True, items=None):
    return {"passed": passed, "items": items if items is not None else [],
            "raw_output": None}


def _repo_with_one_commit(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    (work / "seed.txt").write_text("seed\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "seed")
    _git(work, "checkout", "-qb", "feat/x")
    (work / "seed.txt").write_text("seed\nmore\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "the change")
    return work


# ─────────────────────── _review_checklist_comment: shape ────────────────── #


def test_body_carries_marker_verdict_rounds_sha_and_rows():
    decision = _decision(passed=True, items=[
        _item(label="blocking one", passed=False, severity="high",
              file="a.py", line=3, comment="fix this"),
        _item(label="passed one", passed=True, severity="", file="b.py",
              line=0, comment="looks right"),
        _item(label="a nit", passed=False, severity="nit", file="", line=0,
              comment="cosmetic"),
    ])
    body = Orchestrator._review_checklist_comment(
        Task.new("t", repo_path="/r"), decision, head_sha="abc1234def", rounds=1)

    assert body.startswith(MARKER + "\n")
    assert "## Independent review — PASSED (1 round) on `abc1234`" in body
    assert 'told to refute "done"' in body
    assert "| ❌ high | blocking one | `a.py:3` | fix this |" in body
    assert "| ✅ | passed one | `b.py` | looks right |" in body
    # advisory folded, never in the main table
    assert "a nit" not in body.split("<details>")[0]
    assert "<details><summary>1 advisory finding (low/nit — never blocking)" in body
    assert "| ❌ nit | a nit | — | cosmetic |" in body
    assert "</details>" in body


def test_blocking_first_then_passed_then_advisory_is_the_ordering():
    decision = _decision(items=[
        _item(label="passed A", passed=True),
        _item(label="advisory A", passed=False, severity="low"),
        _item(label="blocking A", passed=False, severity="high"),
        _item(label="passed B", passed=True),
        _item(label="blocking B", passed=False, severity="medium"),
    ])
    body = Orchestrator._review_checklist_comment(
        Task.new("t", repo_path="/r"), decision, head_sha="deadbee", rounds=2)
    main = body.split("<details>")[0]
    order = [name for name in ("blocking A", "blocking B", "passed A", "passed B")
            if name in main]
    assert order == ["blocking A", "blocking B", "passed A", "passed B"], main
    assert "advisory A" not in main


def test_failed_round_renders_failed_heading():
    body = Orchestrator._review_checklist_comment(
        Task.new("t", repo_path="/r"), _decision(passed=False, items=[
            _item(label="still broken", passed=False, severity="high"),
        ]), head_sha="0123456789", rounds=3)
    assert "## Independent review — FAILED (3 rounds) on `0123456`" in body


def test_zero_items_still_posts_verdict_and_says_no_findings():
    body = Orchestrator._review_checklist_comment(
        Task.new("t", repo_path="/r"), _decision(passed=True, items=[]),
        head_sha="abc1234", rounds=1)
    assert "## Independent review — PASSED (1 round) on `abc1234`" in body
    assert "_no findings recorded_" in body


def test_where_cell_is_file_colon_line_file_alone_or_dash():
    both = _item(file="a.py", line=9)
    file_only = _item(file="b.py", line=0)
    neither = _item(file="", line=0)
    assert Orchestrator._where_cell(both) == "`a.py:9`"
    assert Orchestrator._where_cell(file_only) == "`b.py`"
    assert Orchestrator._where_cell(neither) == "—"


def test_cap_is_40_rows_and_says_how_many_more():
    items = [_item(label=f"finding {i}", passed=False, severity="high")
            for i in range(45)]
    body = Orchestrator._review_checklist_comment(
        Task.new("t", repo_path="/r"), _decision(passed=False, items=items),
        head_sha="abc1234", rounds=1)
    assert "finding 39" in body
    assert "finding 40" not in body
    assert "(5 more not shown)" in body


# ─────────────────── every model-authored cell is _table_cell'd ─────────── #


def test_severity_survives_the_same_injection_label_does():
    """Attempt #2's exact finding: severity was interpolated RAW into
    `❌ {sev}`, unlike label/file/comment. A newline + a `# heading` in
    severity must fold to the same single table row and never start a live
    heading — exactly the property already held for `label`."""
    hostile = "high\n# PWNED\nmore"
    item = _item(label=hostile, passed=False, severity=hostile,
                 comment=hostile, file=hostile, line=1)
    decision = _decision(passed=False, items=[item])
    body = Orchestrator._review_checklist_comment(
        Task.new("t", repo_path="/r"), decision, head_sha="abc1234", rounds=1)
    assert "\n# PWNED\n" not in body
    assert "<h1>" not in body and "<h2>" not in body
    # exactly one table row for this finding (severity, label, note all folded
    # onto one line each) — a live heading would have split it onto several.
    row_lines = [ln for ln in body.splitlines() if "PWNED" in ln]
    assert len(row_lines) == 1, row_lines
    assert row_lines[0].startswith("| ❌"), row_lines[0]


def test_label_with_a_heading_stays_on_one_row():
    hostile = "normal text\n# heading\nmore text"
    decision = _decision(passed=False, items=[_item(label=hostile, passed=False)])
    body = Orchestrator._review_checklist_comment(
        Task.new("t", repo_path="/r"), decision, head_sha="abc1234", rounds=1)
    assert "\n# heading" not in body
    lines_with_it = [ln for ln in body.splitlines() if "normal text" in ln]
    assert len(lines_with_it) == 1
    assert lines_with_it[0].count("|") >= 4


def test_a_pipe_in_a_label_does_not_add_a_column():
    """A literal `|` inside a GFM table cell ENDS the cell — unescaped, a
    finding label like `pipe | here` (a regex, a shell pipeline, a type
    union) silently shifts every column after it and drops the last cell
    off the row.

    NOTE the repro's `note` key is not what the Note cell reads: `_checklist_
    note_cell` reads `item["comment"]`/`item["evidence"]`, never `item["note"]`
    — that is why the ORIGINAL repro's control row showed an empty Note. Here
    both the control and the hostile item drive `comment=`, so the control's
    Note cell is legitimately populated, not blank.
    """
    control = _item(label="plain", comment="ab")
    hostile = _item(label="pipe | here", comment="a|b")
    control_row = Orchestrator._checklist_row(control)
    hostile_row = Orchestrator._checklist_row(hostile)
    control_seps = control_row.replace("\\|", "").count("|")
    assert control_seps == 5, control_row
    hostile_seps = hostile_row.replace("\\|", "").count("|")
    assert hostile_seps == control_seps, hostile_row
    assert "pipe \\| here" in hostile_row, hostile_row
    assert "a\\|b" in hostile_row, hostile_row


def test_a_pipe_survives_end_to_end_in_the_posted_comment():
    """Same property as the row-level test above, but through the actual
    posted comment body, and covering ALL FOUR cells (severity, label, file,
    comment) now that `_checklist_row` routes every one of them through
    `_table_cell`."""
    hostile = _item(label="pipe | here", severity="hi|gh", file="a|b.py",
                     line=3, comment="c|d")
    decision = _decision(passed=False, items=[hostile])
    body = Orchestrator._review_checklist_comment(
        Task.new("t", repo_path="/r"), decision, head_sha="abc1234", rounds=1)
    # NOT "here" — the header's "Where" column name contains it too.
    row_lines = [ln for ln in body.splitlines() if "pipe" in ln]
    assert len(row_lines) == 1, row_lines
    row = row_lines[0]
    assert row.startswith("| ❌"), row
    header_seps = "| Severity | Finding | Where | Note |".count("|")
    assert row.replace("\\|", "").count("|") == header_seps, row
    assert "pipe \\| here" in row, row
    assert "hi\\|gh" in row, row
    assert "a\\|b.py" in row, row
    assert "c\\|d" in row, row


def test_a_backslash_escaped_pipe_is_not_escaped_twice():
    """The escape must be idempotent: a label that already contains a
    backslash-escaped pipe (`a\\|b`) must stay `a\\|b`, never become the
    doubly-escaped `a\\\\|b` — a literal backslash followed by a LIVE column
    break, which is the same defect one layer down."""
    row = Orchestrator._checklist_row(_item(label=r"a\|b"))
    assert "a\\|b" in row, row
    assert "a\\\\|b" not in row, row
    for value in ("a|b", r"a\|b", "||", "plain"):
        once = Orchestrator._table_cell(value)
        twice = Orchestrator._table_cell(once)
        assert twice == once, (value, once, twice)


def test_inline_cell_does_not_escape_pipes():
    """Pins that the fix is ADDITIVE: `_inline_cell` itself must keep
    passing raw pipes through unescaped for its non-table callers."""
    assert Orchestrator._inline_cell("a|b") == "a|b"
    assert Orchestrator._inline_cell("normal\n# x") == "normal # x"


def test_table_cell_is_inline_cell_plus_only_the_pipe_escape():
    for value in ("# heading text", "<h1>PWNED</h1>", "line1\nline2\nline3",
                  "x" * 200):
        assert Orchestrator._table_cell(value) == Orchestrator._inline_cell(value)
    long_value = "y" * 200
    assert (Orchestrator._table_cell(long_value, limit=None)
            == Orchestrator._inline_cell(long_value, limit=None))
    assert len(Orchestrator._table_cell(long_value, limit=None)) == 200


def test_composed_cells_are_idempotent_under_inline_cell():
    """`_checklist_row` runs `_table_cell` (= `_inline_cell` + pipe escape)
    over cells that already went through `_inline_cell` once inside
    `_severity_cell`/`_where_cell`/`_checklist_note_cell`. That double
    application is only safe if `_inline_cell` is idempotent on its own
    output — this pins the assumption directly rather than trusting it."""
    for hostile in ("x\n# heading", "<h1>PWNED</h1>", "-bullet"):
        item = _item(severity=hostile, file=hostile, line=1, comment=hostile)
        sev_out = Orchestrator._severity_cell(item)
        assert Orchestrator._inline_cell(sev_out) == sev_out, sev_out
        where_out = Orchestrator._where_cell(item)
        assert Orchestrator._inline_cell(where_out) == where_out, where_out
        note_out = Orchestrator._checklist_note_cell(item)
        assert Orchestrator._inline_cell(note_out) == note_out, note_out


# ─────────────────── shared-fixture: verdict/rounds match _review_verdict_data ─ #


def test_verdict_and_rounds_match_review_verdict_data_for_the_same_head():
    head_sha = "f" * 40
    task = Task.new("t", repo_path="/r")
    task.context = {"review_history": [
        {"round": 1, "sha": "0" * 40, "passed": False, "blocking": [], "advisory": []},
        {"round": 2, "sha": head_sha, "passed": True, "blocking": [], "advisory": []},
    ]}
    rv = Orchestrator._review_verdict_data(task, head_sha=head_sha, repo=None)
    # `advisory_count` joined this contract with the merge-ready work: the PR
    # body needs the UNCAPPED advisory count, which the 5-item review_history
    # trail cannot give. Pinned by exact equality like the three fields before
    # it, so a fifth key cannot appear unnoticed either.
    assert rv == {"rounds": 2, "verdict": "PASSED", "addressed": [],
                  "advisory_count": 0}

    body = Orchestrator._review_checklist_comment(
        task, _decision(passed=True, items=[]), head_sha=head_sha,
        rounds=rv["rounds"])
    assert f"## Independent review — PASSED ({rv['rounds']} rounds)" in body


# ───────────────────────── idempotency / negative controls ───────────────── #


async def test_no_pr_url_posts_nothing_and_never_calls_the_poster(
    store, tmp_path, monkeypatch):
    orch = _orch(store, tmp_path)
    called = []
    monkeypatch.setattr(comment_poster, "post_to_pr_once",
                        lambda *a, **k: called.append(1) or {"ok": True})
    result = await orch._post_review_checklist_comment(
        Task.new("t", repo_path="/r"), "", _decision(), head_sha="abc1234",
        rounds=1)
    assert result is False
    assert not called, "no PR url must never reach the poster at all"


async def test_skipped_duplicate_is_success(store, tmp_path, monkeypatch):
    orch = _orch(store, tmp_path)
    monkeypatch.setattr(comment_poster, "marker_present_on_pr",
                        lambda url, marker: (True, True))
    forge = []
    monkeypatch.setattr(
        comment_poster, "post_to_pr",
        lambda url, body: forge.append(body) or {"ok": True,
                                                 "mode": "issue_comment", "error": ""})
    events = []
    orch.emit = lambda kind, msg, **kw: events.append((kind, kw.get("status")))
    result = await orch._post_review_checklist_comment(
        Task.new("t", repo_path="/r"), "https://github.com/o/r/pull/1",
        _decision(), head_sha="abc1234", rounds=1)
    assert result is True
    assert not forge, "a duplicate must not re-post"
    assert ("review_comment", "skipped") in events


async def test_a_raising_poster_is_advisory_not_raised(store, tmp_path, monkeypatch):
    orch = _orch(store, tmp_path)
    monkeypatch.setattr(
        comment_poster, "marker_present_on_pr",
        lambda url, marker: (_ for _ in ()).throw(RuntimeError("gh boom")))
    result = await orch._post_review_checklist_comment(
        Task.new("t", repo_path="/r"), "https://github.com/o/r/pull/1",
        _decision(), head_sha="abc1234", rounds=1)
    assert result is False


async def test_orchestrator_posts_once_across_two_calls(store, tmp_path, monkeypatch):
    orch = _orch(store, tmp_path)
    forge = []
    monkeypatch.setattr(comment_poster, "marker_present_on_pr",
                        lambda url, marker: (True, any(MARKER in b for b in forge)))
    monkeypatch.setattr(
        comment_poster, "post_to_pr",
        lambda url, body: forge.append(body) or {"ok": True,
                                                 "mode": "issue_comment", "error": ""})
    task = Task.new("t", repo_path="/r")
    url = "https://github.com/o/r/pull/1"
    assert await orch._post_review_checklist_comment(
        task, url, _decision(), head_sha="abc1234", rounds=1) is True
    assert len(forge) == 1
    assert await orch._post_review_checklist_comment(
        task, url, _decision(), head_sha="abc1234", rounds=1) is True
    assert len(forge) == 1, "the second call must not duplicate the comment"


async def test_config_false_skips_posting(store, tmp_path, monkeypatch):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data["review"] = {"post_checklist_comment": False}
    orch = Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))
    called = []
    monkeypatch.setattr(comment_poster, "post_to_pr_once",
                        lambda *a, **k: called.append(1) or {"ok": True})
    events = []
    orch.emit = lambda kind, msg, **kw: events.append((kind, kw.get("status")))
    result = await orch._post_review_checklist_comment(
        Task.new("t", repo_path="/r"), "https://github.com/o/r/pull/1",
        _decision(), head_sha="abc1234", rounds=1)
    assert result is False
    assert not called
    assert ("review_comment", "skipped") in events


async def test_config_default_is_true_and_posts(store, tmp_path, monkeypatch):
    from no_human.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["review"]["post_checklist_comment"] is True

    orch = _orch(store, tmp_path)
    monkeypatch.setattr(comment_poster, "marker_present_on_pr",
                        lambda url, marker: (True, False))
    forge = []
    monkeypatch.setattr(
        comment_poster, "post_to_pr",
        lambda url, body: forge.append(body) or {"ok": True,
                                                 "mode": "issue_comment", "error": ""})
    result = await orch._post_review_checklist_comment(
        Task.new("t", repo_path="/r"), "https://github.com/o/r/pull/1",
        _decision(), head_sha="abc1234", rounds=1)
    assert result is True
    assert forge


# ───────── the REAL scrub path (not a stub of my own new method) ─────────── #


async def test_a_banned_term_is_redacted_by_the_real_scrub_before_it_reaches_the_forge(
    store, tmp_path, monkeypatch):
    """Drives the REAL chain: `_review_checklist_comment` renders the term
    into a finding's comment cell -> `_post_review_checklist_comment` calls
    the REAL `post_to_pr_once` -> REAL `post_to_pr` -> REAL `scrub_outbound`.
    Only the process boundary (`subprocess.run` inside `comment_poster`) is
    stubbed — never `_review_checklist_comment` itself, and the marker
    constant is never touched. This is the fix for attempt #1's exact sin.

    `scrub_outbound` REDACTS a term embedded in real prose (it only raises
    `ScrubDrainedError` when the WHOLE field is vendor terms — confirmed via
    `tests/test_outbound_scrub.py::test_title_that_is_only_a_term_is_
    refused`). A fixed-heading, fixed-table-header comment body can never be
    all-vendor-terms, so redaction — not refusal — is the real, reachable
    outcome here, and it is still advisory: the PR is never blocked."""
    term = _term(0)
    decision = _decision(passed=False, items=[
        _item(label="uses a banned term", passed=False, severity="high",
              comment=f"references {term} in a comment", file="x.py", line=1),
    ])
    orch = _orch(store, tmp_path)

    argv_seen = []

    class _P:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def fake_run(argv, **kw):
        argv_seen.append(argv)
        return _P()

    monkeypatch.setattr(comment_poster.subprocess, "run", fake_run)

    events = []
    orch.emit = lambda kind, msg, **kw: events.append((kind, msg, kw.get("status")))

    result = await orch._post_review_checklist_comment(
        Task.new("t", repo_path="/r"), "https://github.com/o/r/pull/9",
        decision, head_sha="abc1234", rounds=1)

    # The comment WAS posted (the stub answered the list call with "[]" and
    # the POST with rc 0), so the real chain ran end to end:
    assert result is True
    assert [e for e in events if e[0] == "review_comment" and e[2] == "posted"]
    # ...and the body that actually reached the process boundary — the
    # `-f body=...` argument of the POST argv — is the SCRUBBED one: the
    # term is gone and the redaction marker is in its place. This is the
    # assertion attempt #1 and #3 lacked: it reads what left, not what
    # scrub_outbound does in isolation.
    posted = [a for a in argv_seen
              if any(str(x).startswith("body=") for x in a)]
    assert posted, f"no POST with a body reached the stub: {argv_seen}"
    body_arg = next(x for x in posted[-1] if str(x).startswith("body="))
    assert term not in body_arg
    assert "[REDACTED]" in body_arg
    # And the unscrubbed render did carry the term, so the scrub had
    # something to do (the test cannot pass vacuously):
    rendered = Orchestrator._review_checklist_comment(
        Task.new("t", repo_path="/r"), decision, head_sha="abc1234", rounds=1)
    assert term in rendered, "the term must actually reach the render (real path)"


async def test_an_undecodable_stored_checklist_posts_as_unreadable_not_clean(
    store, tmp_path, monkeypatch):
    """A corrupt `attempts.review_checklist` string must never render as a
    clean "no findings recorded": the reader would believe the reviewer
    recorded nothing. It renders as UNREADABLE, says it is missing evidence,
    and the post site emits an advisory naming the decode failure."""
    orch = _orch(store, tmp_path)
    corrupt = '{"passed": true, "items": [ <not json'
    body = Orchestrator._review_checklist_comment(
        Task.new("t", repo_path="/r"), corrupt, head_sha="abc1234", rounds=2)
    assert "UNREADABLE" in body
    assert "could not be decoded" in body
    assert "no findings recorded" not in body
    assert "PASSED" not in body

    advisories = []
    orch._advisory = lambda msg: advisories.append(msg)
    events = []
    orch.emit = lambda kind, msg, **kw: events.append((kind, msg, kw.get("status")))

    class _P:
        returncode = 0
        stdout = "[]"
        stderr = ""

    monkeypatch.setattr(comment_poster.subprocess, "run", lambda argv, **kw: _P())
    result = await orch._post_review_checklist_comment(
        Task.new("t", repo_path="/r"), "https://github.com/o/r/pull/9",
        corrupt, head_sha="abc1234", rounds=2)
    assert result is True
    assert any("could not be decoded" in a for a in advisories), advisories


# ─────────────────────────── `_finalize` call-site wiring ────────────────── #


class _FakePR:
    kind = "github"

    def __init__(self, url, pushed_sha):
        self.url = url
        self.pushed_sha = pushed_sha


@pytest.fixture
def captured_pr(monkeypatch):
    import no_human.core.orchestrator as orch_mod
    from no_human.vcs.receipts import Receipt

    seen = {}

    def fake_open_pr(repo, branch, title, body, **kw):
        seen["body"] = body
        return _FakePR("https://github.com/o/r/pull/300", repo.head_sha())

    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)
    monkeypatch.setattr(orch_mod, "verify_pr_receipt",
                        lambda *a, **k: Receipt("pr_open", a[1] if len(a) > 1
                                                else "", "landed", "ok"))
    return seen


async def test_finalize_posts_the_checklist_comment_after_opening_the_pr(
    store, tmp_path, captured_pr, monkeypatch):
    """WIRING: the checklist decode (str/dict), the `"rounds" in review_
    verdict` guard, and the actual post-PR-open invocation — all three of
    attempt #2's untested call-site pieces, driven through the real
    `_finalize`."""
    work = _repo_with_one_commit(tmp_path)
    repo = GitRepo(work)
    orch = _orch(store, tmp_path)
    head_sha = repo.head_sha()

    task = Task.new("t", repo_path=str(work))
    task.context = {"review_history": [
        {"round": 1, "sha": head_sha, "passed": True, "blocking": [], "advisory": []},
    ]}
    await store.create_task(task)
    await store.set_status(task, TaskStatus.TESTING, validate=False)
    attempt_id = await store.create_attempt(task.id, 1)
    # STORED AS A JSON STRING — proves the str-vs-dict decode at the call site
    # actually decodes, not just accepts an already-dict fixture.
    await store.update_attempt(
        attempt_id,
        review_checklist=json.dumps(_decision(passed=True, items=[
            _item(label="checked the thing", passed=True, severity="",
                 file="y.py", line=4, comment="looks right"),
        ])))

    forge = []
    monkeypatch.setattr(comment_poster, "marker_present_on_pr",
                        lambda url, marker: (True, False))
    monkeypatch.setattr(
        comment_poster, "post_to_pr",
        lambda url, body: forge.append(body) or {"ok": True,
                                                 "mode": "issue_comment", "error": ""})

    commit = _Commit()
    commit.sha = head_sha
    out = await orch._finalize(task, repo, "feat/x", "main", commit,
                               attempt_id, _Result())
    assert out.status == TaskStatus.AWAITING_APPROVAL

    checklist_posts = [b for b in forge if b.startswith(MARKER)]
    assert len(checklist_posts) == 1, forge
    posted = checklist_posts[0]
    assert "## Independent review — PASSED (1 round)" in posted
    assert "checked the thing" in posted
    assert "| ✅ | checked the thing | `y.py:4` | looks right |" in posted


async def test_finalize_posts_nothing_when_the_delivering_row_has_no_checklist(
    store, tmp_path, captured_pr, monkeypatch):
    """A human-gated resume delivers on a FRESH attempt row that carries no
    `review_checklist` (`_resume_human_gated` copies branch/sha only), while
    `review_history` still matches the head with a PASS. The independent
    review of this feature reproduced the consequence: a comment reading
    "FAILED … no findings recorded" under a body whose Evidence row says
    PASSED. The call site must post NOTHING in that shape and say why."""
    work = _repo_with_one_commit(tmp_path)
    repo = GitRepo(work)
    orch = _orch(store, tmp_path)
    head_sha = repo.head_sha()

    task = Task.new("t", repo_path=str(work))
    task.context = {"review_history": [
        {"round": 1, "sha": head_sha, "passed": True, "blocking": [], "advisory": []},
    ]}
    await store.create_task(task)
    await store.set_status(task, TaskStatus.TESTING, validate=False)
    attempt_id = await store.create_attempt(task.id, 2)   # no review_checklist

    forge = []
    monkeypatch.setattr(comment_poster, "marker_present_on_pr",
                        lambda url, marker: (True, False))
    monkeypatch.setattr(
        comment_poster, "post_to_pr",
        lambda url, body: forge.append(body) or {"ok": True,
                                                 "mode": "issue_comment", "error": ""})
    advisories = []
    orch._advisory = lambda msg: advisories.append(msg)

    commit = _Commit()
    commit.sha = head_sha
    out = await orch._finalize(task, repo, "feat/x", "main", commit,
                               attempt_id, _Result())
    assert out.status == TaskStatus.AWAITING_APPROVAL
    assert not [b for b in forge if b.startswith(MARKER)], forge
    assert any("no review checklist" in a for a in advisories), advisories


async def test_finalize_posts_nothing_when_no_review_round_matches_the_head(
    store, tmp_path, captured_pr, monkeypatch):
    """`_review_verdict_data` returns `{"unmatched": True}` (no `"rounds"`
    key) when review rounds exist but none is stamped for this head — the
    guard `_finalize` uses must skip the comment call entirely rather than
    post a claim about the wrong commit."""
    work = _repo_with_one_commit(tmp_path)
    repo = GitRepo(work)
    orch = _orch(store, tmp_path)
    head_sha = repo.head_sha()

    task = Task.new("t", repo_path=str(work))
    task.context = {"review_history": [
        {"round": 1, "sha": head_sha, "passed": True, "blocking": [], "advisory": []},
    ]}
    await store.create_task(task)
    await store.set_status(task, TaskStatus.TESTING, validate=False)
    attempt_id = await store.create_attempt(task.id, 1)

    forge = []
    monkeypatch.setattr(comment_poster, "marker_present_on_pr",
                        lambda url, marker: (True, False))
    monkeypatch.setattr(
        comment_poster, "post_to_pr",
        lambda url, body: forge.append(body) or {"ok": True,
                                                 "mode": "issue_comment", "error": ""})
    # Force the verdict lookup to report "unmatched" (no `rounds` key), the
    # same shape a genuinely-stale review_history round produces.
    monkeypatch.setattr(Orchestrator, "_review_verdict_data",
                        staticmethod(lambda task, **kw: {"unmatched": True}))

    commit = _Commit()
    commit.sha = head_sha
    await orch._finalize(task, repo, "feat/x", "main", commit, attempt_id, _Result())

    assert not any(b.startswith(MARKER) for b in forge), (
        "a comment was posted with no review round proven to judge this head")
