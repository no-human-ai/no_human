"""Reviewer-recall runner — SCRUM-29.

Scores the fresh-context reviewer against the held-out seeded-defect corpus
(``eval/reviewer_recall/cases/``) per ``docs/REVIEWER_RECALL_METHOD.md``.
Scoring is entirely mechanical (no LLM judge) so the number is stable across
runs.

CLI entry point: ``nh bench report --reviewer-recall`` (wired in
``src/no_human/cli/commands.py::_load_reviewer_recall_runner``) loads this
module by file path and calls :func:`run_and_report`. That CLI file is the
ONLY module outside this ``eval/`` tree allowed to reference
``eval/reviewer_recall`` — ``tests/test_reviewer_recall_guard.py`` pins this,
because nothing under ``eval/reviewer_recall/`` may ever be imported by
prompt-construction, few-shot, tuning, or intake code (method doc, "Corpus
design").
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

CASES_DIR = Path(__file__).resolve().parent / "cases"
RUNS_DIR = Path(__file__).resolve().parent / "runs"
METHOD_DOC = "docs/REVIEWER_RECALL_METHOD.md"
# Per-case directory holding the base content the case's diff applies to.
BASE_DIR_NAME = "base"

logger = logging.getLogger(__name__)


class CasePrepError(RuntimeError):
    """A case could not be materialised into a scratch repo — e.g. its
    ``base/`` directory is missing. Caught by :func:`run_case` and reported as
    ``status="ERROR"`` (never scored as a miss)."""


class HeadlineRefusedError(RuntimeError):
    """Raised when the recall headline would be computed with an unmeasured
    (ERROR) case in the denominator — the runner refuses rather than print a
    misleading number."""


class InstrumentInvalidError(HeadlineRefusedError):
    """The count of clean controls that drew a blocking finding exceeds half
    the seeded-case count: a reviewer that flags (nearly) every diff "catches"
    every defect, so the recall headline would measure nothing. A subclass of
    :class:`HeadlineRefusedError` so ``nh bench report`` prints it as the same
    clean refusal instead of a traceback."""


class TranscriptOverwriteRefused(RuntimeError):
    """``runs/<date>/`` already holds transcripts and ``overwrite=`` was not
    given.

    Writes to the audit trail are destructive per-case-file
    (``Path.write_text`` replaces) and not idempotent across differing runs:
    a stub or partial run silently replaces a real measurement's transcripts
    for the same UTC day. Reproduced 2026-08-12 as a bogus 0/19 headline
    after a test suite run overwrote a real day's transcripts. Fail closed:
    pass ``overwrite=True`` only when that replacement is intended.
    """


@dataclass
class Finding:
    file: str = ""
    line: int = 0
    text: str = ""
    blocking: bool = True


@dataclass
class ReviewOutcome:
    """What a reviewer invocation produced for one case."""

    status: str  # "PASS" | "FAIL"
    findings: list[Finding] = field(default_factory=list)
    # The verdict's goal-reachability block (``ReviewDecision.goal``), as-is:
    # {"reachable": bool, "entry_point": str, "evidence": str, +"demoted" when
    # the entry_point citation failed}. None when the reviewer emitted none —
    # the pre-goal-field behaviour, scored exactly as before.
    goal: dict[str, Any] | None = None
    # Blocking findings the reviewer's citation rule demoted to advisory
    # ("label: reason" strings, from ``ReviewDecision.demoted_citations``).
    #
    # Recorded because a demotion can decide a control. The scratch repo holds
    # only the files the diff touches, so a blocking finding citing a file
    # OUTSIDE the diff demotes and the control then scores a clean pass. With 4
    # controls one flip is 25 points, and published specificity has sat at 2/4
    # and 0/4 — the range where one flip is the whole signal. Without this
    # field, a demoted-citation clean pass is indistinguishable from a genuine
    # one in every recorded artifact, and the README's "say so rather than
    # banking the number" is unperformable.
    demoted_citations: list[str] = field(default_factory=list)


@dataclass
class CaseSpec:
    case_id: str
    dir: Path
    base_ref: str
    diff_text: str
    truth: dict[str, Any]
    # Optional ticket text (``request.txt``), passed to the reviewer as the
    # task description. Wiring-class cases need it: "the outcome never happens
    # through the production caller" is only judgeable against the outcome the
    # ticket asked for. Benign-unwired controls carry it for the same reason —
    # their ticket explicitly requests an artifact nothing calls yet.
    request: str = ""

    @property
    def is_control(self) -> bool:
        return self.truth.get("class") == "control"


@dataclass
class CaseResult:
    case_id: str
    cls: str
    is_control: bool
    outcome: ReviewOutcome
    caught: bool | None = None       # seeded cases only
    clean_pass: bool | None = None   # control cases only
    reason: str = ""
    status: str = "OK"               # "OK" | "ERROR" (prepare_case_repo failed)

    @property
    def demoted_citations(self) -> list[str]:
        return self.outcome.demoted_citations

    @property
    def clean_pass_relied_on_demotion(self) -> bool:
        """This control passed, but the reviewer DID raise blocking findings
        that the citation rule demoted — so the pass may be an artifact of the
        scratch repo holding only the diff's files, not reviewer specificity.

        Never bank such a control silently: report it.
        """
        return bool(self.is_control and self.clean_pass
                    and self.outcome.demoted_citations)


@dataclass
class RecallReport:
    results: list[CaseResult]
    model: str
    run_date: str
    mode: str = "diff-only"  # "diff-only" | "gate" (issue #436)
    method_doc: str = METHOD_DOC


ReviewerFn = Callable[[Path, str, CaseSpec], Awaitable[ReviewOutcome]]


CASE_FILE_NAMES = ("base.ref", "change.diff", "truth.json")


def load_cases(cases_dir: Path = CASES_DIR) -> list[CaseSpec]:
    """Iterate ``cases_dir`` and parse each ``<id>/{base.ref,change.diff,truth.json}``.

    A directory with NONE of those files is not a case at all (``__pycache__``,
    editor scratch) and is skipped. A directory with SOME of them is a broken
    case and raises: silently dropping it would move the denominator with
    nothing to say so, and it would route around
    :class:`HeadlineRefusedError` — a dropped case never becomes a
    :class:`CaseResult`, so the refusal guard never sees it. Loud beats short.
    """
    cases: list[CaseSpec] = []
    for entry in sorted(cases_dir.iterdir()):
        if not entry.is_dir():
            continue
        present = [n for n in CASE_FILE_NAMES if (entry / n).is_file()]
        if not present:
            continue
        if len(present) != len(CASE_FILE_NAMES):
            missing = [n for n in CASE_FILE_NAMES if n not in present]
            raise CasePrepError(
                f"{entry.name}: incomplete case directory — missing {missing} "
                f"(has {present}). Fix or remove the directory; the runner will "
                "not quietly shrink the denominator around it."
            )
        base_ref = (entry / "base.ref").read_text().strip()
        diff_text = (entry / "change.diff").read_text()
        truth = json.loads((entry / "truth.json").read_text())
        request_file = entry / "request.txt"
        request = request_file.read_text() if request_file.is_file() else ""
        cases.append(CaseSpec(
            case_id=entry.name, dir=entry, base_ref=base_ref,
            diff_text=diff_text, truth=truth, request=request,
        ))
    return cases


def _run(cmd: list[str], *, cwd: Path, input_bytes: bytes | None = None) -> None:
    subprocess.run(cmd, cwd=cwd, input=input_bytes, check=True, capture_output=True)


def prepare_case_repo(repo_root: Path, case: CaseSpec, workdir: Path) -> Path:
    """Seed a single-commit scratch repo from the case's own materialised base
    content, then apply ``case.diff_text``.

    HISTORY-INDEPENDENT (2026-07-30). This used to run
    ``git archive <case.base_ref>`` against ``repo_root``, which pinned the
    corpus to this repo's history. no_human ships as a fresh ``git init`` with
    a single commit and no other objects, so every case would have ERRORed at
    ``git archive`` in the published repo. The base content each diff needs now
    lives in ``cases/<id>/base/`` (regenerate with
    ``eval/reviewer_recall/materialize_base.py``); ``base.ref`` is kept as
    provenance only and is never resolved. ``repo_root`` is likewise unused —
    kept in the signature because it is part of the runner's public shape.

    The no-descendant-history property the method doc requires is unchanged and
    in fact stricter: the scratch repo is seeded from plain files, so there is
    no ref, remote, ancestor or descendant commit to do archaeology against —
    only the single ``base`` commit this function creates.

    Only the files the diff touches are materialised. That is the whole set the
    scratch repo is ever read for in diff-only mode: the recall runner calls
    the reviewer with ``diff_override``, which puts it on the single-turn
    no-tools path (``reviewer.py``, gate mode), so the tree is consulted only
    by ``_verify_citations``. See the README for the one behavioural delta this
    implies. ``--mode gate`` (:func:`_gate_reviewer_fn`) commits the applied
    change as ``head`` and gives the reviewer read tools over this same tree.
    """
    base_src = case.dir / BASE_DIR_NAME
    target = workdir / case.case_id
    if base_src.is_dir():
        shutil.copytree(base_src, target, dirs_exist_ok=True)
    elif _diff_needs_base_content(case.diff_text):
        raise CasePrepError(
            f"{case.case_id}: no materialised base content at {base_src} — "
            f"run eval/reviewer_recall/materialize_base.py {case.case_id} from "
            f"a checkout that still resolves base.ref ({case.base_ref})"
        )
    else:
        # A create-only diff (every pre-image is /dev/null) applies to an
        # empty tree, and git cannot track an empty base/ directory — so its
        # absence is the correct materialisation, not a broken case. This is
        # the ns-1746bea3 ticket shape: "single new file plus its test".
        target.mkdir(parents=True)
    _run(["git", "init", "-q"], cwd=target)
    _run(["git", "add", "-A"], cwd=target)
    # --allow-empty carries the create-only case; every other case still
    # commits its base files exactly as before.
    _run([
        "git", "-c", "user.email=reviewer-recall@no-human.local",
        "-c", "user.name=reviewer-recall", "commit", "-q", "-m", "base",
        "--allow-empty",
    ], cwd=target)
    _run(["git", "apply", "--whitespace=nowarn"], cwd=target,
        input_bytes=case.diff_text.encode())
    return target


def _diff_needs_base_content(diff_text: str) -> bool:
    """True when any pre-image in the diff is a real file (not /dev/null).

    Reads only `--- ` header lines. A CONTENT line starting with "--- " (an
    added line whose text begins "-- ") is conceivable and would read as a
    header here; the failure direction is safe — a false True demands base
    content and raises loudly, never silently prepares a wrong tree.
    """
    for line in diff_text.splitlines():
        if line.startswith("--- ") and line[4:].strip() != "/dev/null":
            return True
    return False


def score_case(case: CaseSpec, outcome: ReviewOutcome) -> CaseResult:
    """Mechanical scoring per docs/REVIEWER_RECALL_METHOD.md, "Scoring".

    Seeded case caught iff EITHER:
    - verdict FAIL (or a blocking finding exists) + a blocking finding names
      the planted file + that finding's cited line falls within hunk_lines ±3
      + the finding text contains >=1 truth.json keyword; OR
    - the verdict's goal block says ``reachable: false`` (not demoted) and its
      entry_point cites the ``caller_file`` truth.json names. No keywords, no
      LLM judge — the goal rule stays as mechanical as the finding rule.
    Control case is a clean pass iff verdict is PASS (or FAIL with only
    non-blocking findings) AND the goal block did not veto — a
    ``reachable: false`` on a defect-free diff is a false alarm even when no
    blocking finding accompanies it, because the gate blocks on it alone.
    """
    cls = case.truth.get("class", "")
    blocking = [f for f in outcome.findings if f.blocking]
    goal = outcome.goal or {}
    goal_veto = goal.get("reachable") is False and not goal.get("demoted")

    if case.is_control:
        clean = (outcome.status == "PASS" or not blocking) and not goal_veto
        if goal_veto:
            reason = ("false alarm: goal.reachable=false on a clean diff "
                      f"(entry point: {goal.get('entry_point') or '(none)'})")
        elif not clean:
            reason = (f"false alarm: blocking finding on a clean diff "
                      f"({blocking[0].file}:{blocking[0].line})")
        elif outcome.demoted_citations:
            # Do not let this read as plain specificity — the reviewer DID
            # raise blocking findings and the citation rule dropped them.
            reason = ("clean pass — BUT "
                      f"{len(outcome.demoted_citations)} blocking finding(s) "
                      "were demoted by the citation rule: "
                      f"{'; '.join(outcome.demoted_citations)}")
        else:
            reason = "clean pass"
        return CaseResult(case_id=case.case_id, cls=cls, is_control=True,
                          outcome=outcome, clean_pass=clean, reason=reason)

    truth = case.truth
    planted_file = truth.get("file")
    hunk = truth.get("hunk_lines") or [None, None]
    hunk_lo, hunk_hi = hunk[0], hunk[1]
    keywords = [k.lower() for k in (truth.get("keywords") or [])]

    # The goal-reachability rule: mechanical, like everything else here. The
    # entry_point must cite the production caller truth.json names — a bare
    # "reachable: false" pointing anywhere would let an unrelated veto score
    # a wiring catch.
    caller_file = str(truth.get("caller_file") or "")
    if (goal_veto and caller_file
            and caller_file in str(goal.get("entry_point") or "")):
        return CaseResult(
            case_id=case.case_id, cls=cls, is_control=False,
            outcome=outcome, caught=True,
            reason=("caught by goal.reachable=false citing "
                    f"{goal.get('entry_point')}"))

    if outcome.status != "FAIL" and not blocking:
        return CaseResult(case_id=case.case_id, cls=cls, is_control=False,
                          outcome=outcome, caught=False,
                          reason="verdict PASS and no blocking finding — defect not flagged")

    if not blocking:
        return CaseResult(case_id=case.case_id, cls=cls, is_control=False,
                          outcome=outcome, caught=False,
                          reason="FAIL verdict but no blocking finding")

    file_matches = [f for f in blocking if f.file == planted_file]
    for f in file_matches:
        in_hunk = (hunk_lo is None or hunk_hi is None or
                  (hunk_lo - 3) <= f.line <= (hunk_hi + 3))
        if not in_hunk:
            continue
        text_lower = f.text.lower()
        if keywords and not any(kw in text_lower for kw in keywords):
            continue
        return CaseResult(case_id=case.case_id, cls=cls, is_control=False,
                          outcome=outcome, caught=True,
                          reason=f"caught by finding at {f.file}:{f.line}")

    if not file_matches:
        reason = "missed — no blocking finding names the planted file"
    elif not any(
        (hunk_lo is None or hunk_hi is None or (hunk_lo - 3) <= f.line <= (hunk_hi + 3))
        for f in file_matches
    ):
        reason = "missed — cited line falls outside hunk_lines ±3"
    else:
        reason = "missed — finding text matches no truth.json keyword"
    return CaseResult(case_id=case.case_id, cls=cls, is_control=False,
                      outcome=outcome, caught=False, reason=reason)


async def run_case(
    repo_root: Path, case: CaseSpec, reviewer_fn: ReviewerFn, workdir: Path,
) -> CaseResult:
    """Checkout + apply + invoke + score a single case."""
    try:
        case_repo = prepare_case_repo(repo_root, case, workdir)
    except (subprocess.CalledProcessError, CasePrepError, OSError) as exc:
        stderr = (getattr(exc, "stderr", None) or b"").decode(errors="ignore")[:400]
        outcome = ReviewOutcome(status="ERROR", findings=[])
        return CaseResult(
            case_id=case.case_id, cls=case.truth.get("class", ""),
            is_control=case.is_control, outcome=outcome,
            caught=None, clean_pass=None, status="ERROR",
            reason=f"case setup failed: {stderr or exc}",
        )
    outcome = await reviewer_fn(case_repo, case.diff_text, case)
    return score_case(case, outcome)


def _gate_reviewer_fn(model: str, config_data: dict[str, Any] | None = None) -> ReviewerFn:
    """The shipping gate, not the diff-only probe (issue #436).

    Builds the reviewer through ``AdversarialReviewer.from_config`` — the
    factory ``core/runtime.build_orchestrator`` uses — so the configured
    reviewer backend (``llm.role_backends.reviewer``) is honoured, and calls
    ``review()`` WITHOUT ``diff_override`` so it takes the refs path: the
    multi-turn read-only session plus the lint/wiring/type evidence sections.

    ``config_data`` is passed EXPLICITLY (the CLI hands in the config it
    already loaded). Nothing here touches ``os.environ`` or copies a file out
    of ``~/.no_human``: the case repo is a temp dir outside the checkout that
    holds only the diff's files, so it contains no case file (``truth.json``
    included). The reviewer's read-only tools are not path-contained, so the
    checkout itself stays readable by absolute path. ``model`` is unused — in
    gate mode the model is whatever the factory resolves from ``config_data``.
    """
    del model

    async def _fn(repo_path: Path, diff_text: str, case: CaseSpec) -> ReviewOutcome:
        from no_human.config import load_config
        from no_human.core.task import Task
        from no_human.review.reviewer import AdversarialReviewer

        data = (config_data if config_data is not None
                else load_config(create_if_missing=False).data)
        # prepare_case_repo left the change applied but uncommitted; commit it
        # so the scratch repo holds exactly two commits, base and head.
        _run(["git", "add", "-A"], cwd=repo_path)
        _run(["git", "-c", "user.email=reviewer-recall@no-human.local",
              "-c", "user.name=reviewer-recall", "commit", "-q", "-m", "head",
              "--allow-empty"], cwd=repo_path)
        reviewer = AdversarialReviewer.from_config(data)
        task = Task.new(f"reviewer-recall probe: {case.case_id}",
                        repo_path=str(repo_path),
                        description=case.request or None)
        decision = await reviewer.review(task, repo_path=repo_path,
                                         before_ref="HEAD~1", after_ref="HEAD")
        return _outcome_from_decision(decision)

    return _fn


def _outcome_from_decision(decision: Any) -> ReviewOutcome:
    blocking_ids = {id(item) for item in decision.blocking_items}
    findings = [
        Finding(file=item.file or "", line=item.line or 0,
                text=f"{item.label}: {item.comment or item.evidence}",
                blocking=id(item) in blocking_ids)
        for item in decision.failed_items
    ]
    return ReviewOutcome(
        status="PASS" if decision.passed else "FAIL",
        findings=findings,
        # Kept, not discarded: with only the diff's files in the scratch
        # repo, a demotion here is what turns a control's false alarm into
        # a "clean pass".
        demoted_citations=list(decision.demoted_citations),
        goal=getattr(decision, "goal", None),
    )


def _default_reviewer_fn(model: str) -> ReviewerFn:
    """Wraps the existing fresh-context reviewer (no_human.review.reviewer)."""

    async def _fn(repo_path: Path, diff_text: str, case: CaseSpec) -> ReviewOutcome:
        from no_human.core.task import Task
        from no_human.review.reviewer import AdversarialReviewer

        task = Task.new(f"reviewer-recall probe: {case.case_id}",
                        repo_path=str(repo_path),
                        # The case's ticket text, when it ships one — the gate
                        # prompt renders it verbatim, which is what makes a
                        # goal-reachability judgment possible at all.
                        description=case.request or None)
        if case.truth.get("send_back_feedback"):
            task.context = {"send_back_feedback": case.truth["send_back_feedback"]}
        reviewer = AdversarialReviewer(model=model)
        decision = await reviewer.review(task, repo_path=repo_path,
                                         diff_override=diff_text, before_ref="HEAD")
        return _outcome_from_decision(decision)

    return _fn


async def run_all(
    repo_root: Path,
    *,
    cases_dir: Path = CASES_DIR,
    reviewer_fn: ReviewerFn | None = None,
    model: str,
    mode: str = "diff-only",
    config_data: dict[str, Any] | None = None,
    run_date: str | None = None,
    runs_dir: Path = RUNS_DIR,
    overwrite: bool = False,
) -> RecallReport:
    run_date = run_date or _today()
    _assert_runs_dir_writable(runs_dir, run_date, overwrite)
    if mode not in ("diff-only", "gate"):
        raise ValueError(f"unknown reviewer-recall mode {mode!r}")
    if mode == "gate" and reviewer_fn is None:
        from no_human.agent.backend import explicit_role_backend
        from no_human.config import load_config

        if config_data is None:
            config_data = load_config(create_if_missing=False).data
        # A non-default reviewer backend is disclosed wherever the run's
        # model is shown (constraint #6, §6d) — the report's model line.
        entry = explicit_role_backend(config_data, "reviewer")
        if entry:
            model = f"{entry['model']} on {entry['backend']} (non-default reviewer)"
    fn = reviewer_fn or (_gate_reviewer_fn(model, config_data) if mode == "gate"
                         else _default_reviewer_fn(model))
    cases = load_cases(cases_dir)
    results: list[CaseResult] = []
    with tempfile.TemporaryDirectory(prefix="nh-reviewer-recall-") as tmp:
        workdir = Path(tmp)
        for case in cases:
            results.append(await run_case(repo_root, case, fn, workdir))
    report = RecallReport(results=results, model=model, run_date=run_date, mode=mode)
    write_transcripts(report, runs_dir, overwrite=overwrite)
    return report


def _assert_runs_dir_writable(runs_dir: Path, run_date: str, overwrite: bool) -> None:
    """Refuse to touch ``runs_dir/run_date/`` if it already holds transcripts,
    unless ``overwrite`` is True. Fail closed: an unreadable directory raises
    :class:`TranscriptOverwriteRefused` too — never treated as "empty" and
    silently skipped past. A missing or genuinely empty directory is fine.
    """
    if overwrite:
        return
    out_dir = runs_dir / run_date
    try:
        existing = list(out_dir.glob("*.json")) if out_dir.exists() else []
    except OSError as exc:
        raise TranscriptOverwriteRefused(
            f"{out_dir} could not be inspected before writing ({exc}) — "
            "refusing to write into it. Pass overwrite=True once you have "
            "confirmed it is safe to replace, or move the directory aside."
        ) from exc
    if existing:
        raise TranscriptOverwriteRefused(
            f"{out_dir} already holds {len(existing)} transcript(s) — "
            "refusing to overwrite a same-day audit trail (it may be a real "
            "measurement). Pass overwrite=True to replace it deliberately, "
            "or move the directory aside first."
        )


def write_transcripts(
    report: RecallReport, runs_dir: Path = RUNS_DIR, *, overwrite: bool = False,
) -> Path:
    """Persist one ``<case_id>.json`` audit transcript per case under
    ``runs_dir/<run_date>/`` — called on every :func:`run_all` invocation, per
    docs/REVIEWER_RECALL_METHOD.md ("Borderline transcripts are kept").

    Schema: ``{case_name, status, caught, score, error_message_if_error,
    demoted_citations, clean_pass_relied_on_demotion, goal}``.
    ERROR cases omit ``caught`` (never measured — must not read as a miss)
    and get ``score=None``.

    ``demoted_citations`` is recorded on every case so a score of 1.0 can be
    audited after the fact. It is the difference between "the reviewer found
    nothing wrong with this clean diff" and "the reviewer flagged something and
    the citation rule threw it away because the cited file is not in the
    scratch repo" — which, with 4 controls, is worth 25 specificity points.

    This is the enforcement point for :func:`_assert_runs_dir_writable`: it is
    called here too (not only in :func:`run_all`'s preflight) so no caller —
    present or future — can route around the audit-trail guard by calling
    this function directly.
    """
    _assert_runs_dir_writable(runs_dir, report.run_date, overwrite)
    out_dir = runs_dir / report.run_date
    out_dir.mkdir(parents=True, exist_ok=True)
    for r in report.results:
        data: dict[str, Any] = {
            "case_name": r.case_id,
            "status": r.status,
            "error_message_if_error": r.reason if r.status == "ERROR" else None,
        }
        if r.status == "ERROR":
            data["score"] = None
        elif r.is_control:
            data["caught"] = r.caught
            data["score"] = 1.0 if r.clean_pass else 0.0
        else:
            data["caught"] = r.caught
            data["score"] = 1.0 if r.caught else 0.0
        data["demoted_citations"] = list(r.demoted_citations)
        data["clean_pass_relied_on_demotion"] = r.clean_pass_relied_on_demotion
        # The goal block, verbatim, so a wiring catch (or a goal false alarm
        # on a control) can be audited without re-running anything.
        data["goal"] = r.outcome.goal
        (out_dir / f"{r.case_id}.json").write_text(json.dumps(data, indent=2))
    return out_dir


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def render_report(report: RecallReport) -> str:
    """Render exactly the method-doc format — recall + denominator, per-class
    breakdown, specificity ratio, model, run date, method-doc path. Never a
    bare percentage (the denominator always accompanies it).

    Refuses (raises :class:`HeadlineRefusedError`) if any case has
    ``status == "ERROR"``: an unmeasured case (broken checkout) must never be
    silently folded into the denominator or scored as a miss.
    """
    errored = [r for r in report.results if r.status == "ERROR"]
    if errored:
        ids = ", ".join(r.case_id for r in errored)
        logger.error(
            "reviewer-recall headline refused: %d case(s) errored before "
            "measurement: %s", len(errored), ids,
        )
        raise HeadlineRefusedError(
            f"{len(errored)} case(s) errored before measurement ({ids}) — "
            "refusing to compute the headline recall statistic with an "
            "unmeasured case in the denominator"
        )

    seeded = [r for r in report.results if not r.is_control]
    controls = [r for r in report.results if r.is_control]

    caught = sum(1 for r in seeded if r.caught)
    total = len(seeded)
    pct = f" ({caught / total:.0%})" if total else ""

    by_class: dict[str, list[CaseResult]] = {}
    for r in seeded:
        by_class.setdefault(r.cls, []).append(r)
    class_parts = ", ".join(
        f"{cls} {sum(1 for r in rs if r.caught)}/{len(rs)}"
        for cls, rs in sorted(by_class.items())
    )
    class_suffix = f"  [{class_parts}]" if class_parts else ""

    clean = sum(1 for r in controls if r.clean_pass)
    false_alarms = len(controls) - clean
    if false_alarms > total / 2:
        raise InstrumentInvalidError(
            f"instrument invalid: {false_alarms} of {len(controls)} clean "
            f"controls drew a blocking finding, more than half the {total} "
            "seeded cases — a reviewer that flags nearly every diff makes the "
            "recall number meaningless"
        )

    lines = [
        f"reviewer recall: {caught}/{total}{pct}{class_suffix}",
        f"specificity:     {clean}/{len(controls)} clean diffs passed",
    ]

    # A control that passed only because the citation rule demoted the
    # reviewer's blocking findings is not evidence of specificity. The scratch
    # repo holds only the diff's files, so a finding citing anything else is
    # demoted by construction. Print it next to the number — a caveat that
    # lives only in a doc cannot be acted on.
    suspect = [r for r in controls if r.clean_pass_relied_on_demotion]
    if suspect:
        lines.append(
            f"  ⚠ {len(suspect)} of those {clean} clean passes had blocking "
            "findings demoted by the citation rule — do not bank this "
            "specificity number without reading them:"
        )
        for r in suspect:
            for d in r.demoted_citations:
                lines.append(f"      {r.case_id}: {d}")

    lines.append(
        f"model: {report.model} ({report.mode} mode) · run date: {report.run_date} · "
        f"method: {report.method_doc}"
    )
    return "\n".join(lines)


def run_and_report(
    repo_root: Path | str | None = None,
    *,
    reviewer_fn: ReviewerFn | None = None,
    model: str,
    mode: str = "diff-only",
    config_data: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> str:
    """CLI entry point: `nh bench report --reviewer-recall` calls this.

    Synchronous wrapper: runs the async per-case pipeline and returns the
    rendered report text (never a bare percentage).
    """
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
    report = asyncio.run(run_all(root, reviewer_fn=reviewer_fn, model=model,
                                 overwrite=overwrite, mode=mode,
                                 config_data=config_data))
    return render_report(report)
