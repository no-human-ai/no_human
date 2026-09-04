"""P4 (safe slice): the PR body surfaces an Assumptions & Open Questions
section so the reviewing human catches what the agent assumed."""


from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


def _orch(store, tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


def test_section_empty_when_nothing_to_flag(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("clean task", repo_path="/r")
    assert orch._assumptions_section(t) == ""


def test_section_lists_assumptions_and_enrichment(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("ambiguous task", repo_path="/r")
    t.context = {
        "assumptions": ["header is X-Instance-Id", "retry 3 times"],
        "original_criteria": ["works"],
    }
    section = orch._assumptions_section(t)
    assert "2 assumptions made on your behalf" in section
    assert "header is X-Instance-Id" in section
    assert "retry 3 times" in section
    assert "auto-sharpened" in section
    assert "works" in section


def test_section_includes_blocker_diagnosis(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("stuck task", repo_path="/r")
    t.blocker = {"root_cause_hypothesis": "stuck at 60%", "question": "revise?"}
    section = orch._assumptions_section(t)
    assert "> ⚠️ **Unresolved:** stuck at 60%" in section
    assert "> ⚠️ **Open question:** revise?" in section


class _Commit:
    files_changed = 2
    insertions = 10
    deletions = 1


class _Result:
    final_text = "did the thing"
    num_turns = 5


def test_pr_body_embeds_section(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    t.acceptance_criteria = ["c1"]
    t.context = {"assumptions": ["assume A"]}
    body = orch._pr_body(t, _Commit(), _Result())
    assert "assumption made on your behalf" in body
    assert "assume A" in body
    # Section sits after the changes, before the command log.
    assert body.index("## Changes") < body.index("made on your behalf")
    assert body.index("made on your behalf") < body.index("## How I verified this")


def test_pr_body_clean_when_no_assumptions(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    body = orch._pr_body(t, _Commit(), _Result())
    assert "made on your behalf" not in body
    assert "auto-sharpened" not in body


def test_pr_body_no_evidence_section_by_default(store, tmp_path):
    """Default path (no test_evidence) is unchanged: no Test evidence section."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    body = orch._pr_body(t, _Commit(), _Result())
    assert "Test evidence" not in body


def test_pr_body_surfaces_layered_evidence(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    evidence = {
        "ran": True, "ok": True,
        "layers": [
            "unit: PASS — 12 passed",
            "integration: PASS — 4 passed",
            "e2e: deferred (wake-gated)",
        ],
    }
    body = orch._pr_body(t, _Commit(), _Result(), test_evidence=evidence)
    # The test run is a row (per layer) of the one `## Evidence` table, and
    # the Stats section is gone.
    assert "## Evidence\n| Check | Result |" in body
    assert "| Tests | unit: PASS — 12 passed |" in body
    assert "## Stats" not in body
    assert "integration: PASS — 4 passed" in body
    assert "e2e: deferred (wake-gated)" in body
    # The test evidence sits inside the Evidence umbrella, before the mechanical
    # "How I verified this" receipts section.
    assert body.index("## Evidence") < body.index("| Tests |")
    assert body.index("| Tests |") < body.index("## How I verified this")


def test_pr_body_surfaces_single_run_aggregate(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    evidence = {"ran": True, "ok": False, "passed": 8, "failed": 2, "errors": 1}
    body = orch._pr_body(t, _Commit(), _Result(), test_evidence=evidence)
    assert "| Tests | ❌ FAIL" in body
    assert "8 passed, 2 failed, 1 errors" in body


def test_pr_body_discloses_when_tests_did_not_run(store, tmp_path):
    """Used to pin the OPPOSITE — no "Test evidence" section at all for
    `ran=False` — which is how a PR from a repo with no test command
    carried no test line. The section now exists and says NOT RUN; only an
    absent evidence object (None) leaves it out."""
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    body = orch._pr_body(t, _Commit(), _Result(), test_evidence={"ran": False})
    assert "NOT RUN — no test command detected" in body
    assert "Test evidence" not in orch._pr_body(t, _Commit(), _Result(), test_evidence=None)


def test_section_includes_intake_qa(store, tmp_path):
    """§6 grill: the self-answered Q&A is audited at the human gate."""
    orch = _orch(store, tmp_path)
    t = Task.new("grilled task", repo_path="/r")
    t.context = {"intake_qa": [
        {"question": "Which file?", "decision_it_changes": "target",
         "answer": "src/x.py:1", "source": "repo-evidence", "carve_out": "none"},
        {"question": "Rotate credential?", "decision_it_changes": "auth",
         "answer": "HUMAN-GATED: not self-answerable", "source": "",
         "carve_out": "access"},
    ]}
    section = orch._assumptions_section(t)
    assert "Which file?" in section
    assert "src/x.py:1" in section
    assert "repo-evidence" in section
    assert "Rotate credential?" in section
    assert "HUMAN-GATED" in section


def test_assumptions_are_folded_but_open_questions_are_visible(store, tmp_path):
    """2026-08-21: the Q/A the agent answered for the absent requester is
    delivered folded behind a one-line count; an UNRESOLVED blocker or an
    open question is the one thing a reviewer must not miss, so it stays
    visible above the fold."""
    orch = _orch(store, tmp_path)
    t = Task.new("ambiguous task", repo_path="/r")
    t.context = {
        "intake_qa": [{"question": "Which DB?", "answer": "sqlite",
                       "source": "assumption"}],
        "assumptions": ["the API is idempotent"],
    }
    t.blocker = {"question": "Is the retry cap 3 or 5?"}
    body = orch._pr_body(t, _Commit(), _Result())
    assert "## ⚠️ Assumptions" not in body
    visible = body.split("<details>", 1)[0]
    assert "> ⚠️ **Open question:** Is the retry cap 3 or 5?" in visible
    assert ("<details><summary>⚠️ 2 assumptions made on your behalf — "
            "verify at review</summary>") in body
    assert "the API is idempotent" in body
    assert "**Q:** Which DB? **A:** sqlite" in body


def test_only_sharpened_criteria_fold_under_their_own_summary(store, tmp_path):
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path="/r")
    t.context = {"original_criteria": ["works"]}
    section = orch._assumptions_section(t)
    assert section.startswith(
        "<details><summary>⚠️ acceptance criteria were auto-sharpened at "
        "intake — originals inside</summary>")
    assert "works" in section
