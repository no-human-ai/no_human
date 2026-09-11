"""Merge-ready policy — a declared, human-legible rule list evaluated
mechanically from gate outputs into a per-rule pass/fail verdict.

WHY. Today the only "is this safe to approve" signal is a human reading the
whole PR. This module lets an operator declare, in a reviewable file
(``<repo>/.no_human/merge_policy.yaml``), which mechanical facts must all
hold for a PR to be "merge-ready", and computes a per-rule pass/fail list
from those facts with NO model call.

This verdict is **advisory to the human**. No code path in this repo merges
on it, waits on it, or gates a push on it. ``nh approve`` remains the only
way a PR merges, and it is always a human invocation — nothing here changes
that. ``approval.auto_merge_on_approval`` (``config.py``) is not read by this
module and stays exactly as it is.

This module IS wired in: `core.orchestrator._finalize` calls
:func:`facts_from_evidence` to adapt the gathered `PrEvidence` (plus the raw
tamper adjudications, changed paths, and repro verdict it doesn't carry)
into :class:`GateFacts`, evaluates it, persists the verdict on
`task.context["merge_policy"][<head sha>]`, and the PR body's "Merge policy"
row/fold render it (`core.pr_evidence.PrEvidence.merge_policy`,
`orchestrator._merge_policy_evidence_section`). The task API also surfaces it
read-only (`TaskSummaryOut.merge_ready`, `GET /api/tasks?merge_ready=1`).
None of that makes the verdict binding: it stays advisory-only, computed
inside a best-effort `try`/`except` in `_finalize`, and no code path merges,
blocks, or gates a push on it — see the WHY paragraph above.

A malformed or partially-invalid policy file never raises: it degrades to a
:class:`PolicyLoad` carrying diagnostic ``problems`` strings, and a verdict
built from a load with any problem is never "ready" (fail closed).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import pathglob

# --- vocabulary ------------------------------------------------------------ #

RULE_NAMES: tuple[str, ...] = (
    "review_passed",
    "no_advisory_findings",
    "tests_ran_and_passed",
    "tamper_guard_clear",
    "repro_gate",
    "verifiers_all_satisfied",
    "ci",
    "paths_within",
    "max_changed_lines",
)

# Which rules take an argument, and what shape it must be. Loader validation
# and evaluator dispatch both read from this table so they cannot drift.
_NO_ARG_RULES = frozenset(
    {
        "review_passed",
        "no_advisory_findings",
        "tests_ran_and_passed",
        "tamper_guard_clear",
        "verifiers_all_satisfied",
    }
)
_ENUM_RULES: dict[str, frozenset[str]] = {
    "repro_gate": frozenset({"pass", "pass_or_not_required"}),
    "ci": frozenset({"success", "success_or_unknown"}),
}
_LIST_STR_RULES = frozenset({"paths_within"})
_INT_RULES = frozenset({"max_changed_lines"})

POLICY_RELPATH = ".no_human/merge_policy.yaml"
# Untrusted, hand-edited file: cap the size we'll even attempt to parse.
MAX_BYTES = 16 * 1024


@dataclass(frozen=True)
class Rule:
    name: str
    arg: Any = None


# The default, used when no policy file is present. `ci: success_or_unknown`
# is the default (not the strict `ci: success`) deliberately: forges report
# infra outages as CI failures, and a false "not ready" from an outage is
# worse than tolerating an unreported/unknown CI state here.
DEFAULT_POLICY: tuple[Rule, ...] = (
    Rule("review_passed"),
    Rule("tests_ran_and_passed"),
    Rule("tamper_guard_clear"),
    Rule("repro_gate", "pass_or_not_required"),
    Rule("verifiers_all_satisfied"),
    Rule("ci", "success_or_unknown"),
)


@dataclass
class PolicyLoad:
    rules: list[Rule]
    problems: list[str]
    source: str  # "file" or "default"


@dataclass(frozen=True)
class GateFacts:
    """Flat mechanical inputs. Deliberately flat so an adapter from the
    orchestrator's evidence object is trivial and testable — that adapter is
    a later ticket, not written here."""

    review_passed: bool | None
    review_advisory_count: int = 0
    tests_ran: bool = False
    tests_failed: int = 0
    tests_passed: int = 0
    tamper_fired: bool = False
    tamper_waived: bool = False
    repro_verdict: str | None = None  # pass/fail/waived/error/None
    repro_required: bool = False
    verifiers_ran: int = 0
    verifiers_failed: tuple[str, ...] = ()
    # Verifiers that reached no verdict after their bounded retry (the judge
    # was actually asked and never answered). Advisory only — never makes
    # `_check_verifiers_all_satisfied` fail — but tracked distinctly so a
    # human-legible detail string can name them without being conflated with
    # a genuine FAIL, per `verifiers_failed` above.
    verifiers_unavailable: tuple[str, ...] = ()
    ci_state: str | None = None  # success/failure/pending/unknown/None
    # Advisory detail only — names failing checks in `_check_ci`'s detail
    # string. The pass/fail decision is still made from `ci_state` alone, so
    # this field never affects `ready`; it is purely for the human-legible
    # detail (e.g. "ci: failure (File inventory)").
    ci_failed_checks: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    changed_lines: int = 0
    # True when the diff being evaluated itself edits the policy file
    # (`POLICY_RELPATH`) — a coder authoring its own gate. Computed by
    # `facts_from_evidence` from `changed_paths`, never set by hand in
    # production; tests may still set it directly to exercise `_evaluate`.
    policy_changed_in_diff: bool = False
    # Merge base of the measured tree against trunk at measurement time
    # (`GitRepo.merge_base_with_trunk`). `""` when unestablishable (no trunk
    # ref resolvable, not a repo, git failure) — never raises, always falls
    # open. Defaulted so every existing constructor call keeps compiling.
    base_sha: str = ""


@dataclass(frozen=True)
class RuleVerdict:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class PolicyVerdict:
    ready: bool
    rules: tuple[RuleVerdict, ...]
    source: str
    problems: tuple[str, ...] = ()
    # Mirrors `GateFacts.policy_changed_in_diff` — surfaced here too so
    # renderers (and the "self-authored gate" ⚠️ glyph) don't need the facts
    # object, only the verdict. See `_evaluate`: when true this is ALSO why
    # `ready` is forced False (a problem string is injected), but `summary`
    # below computes rule satisfaction from `self.rules`, not from `ready` —
    # so "3 of 3 rules" reads correctly even though the overall verdict is
    # not ready.
    policy_changed_in_diff: bool = False
    # Merge base the measurement was made against (`GateFacts.base_sha`,
    # carried through unchanged) and whether that measurement's test facts
    # were green, independent of whether the active policy even runs the
    # `tests_ran_and_passed` rule. Both additive, read-only facts consumed by
    # `stale_base_reason()` at read time — neither affects `ready`/`summary`
    # here; a repo-local policy that omits `tests_ran_and_passed` cannot
    # silently disable the staleness check because this is derived straight
    # from facts, not from the active rule list.
    base_sha: str = ""
    tests_green: bool = False

    @property
    def summary(self) -> str:
        n = len(self.rules)
        failed = [v.name for v in self.rules if not v.passed]
        if self.policy_changed_in_diff:
            if not failed:
                return f"ready — {n} of {n} rules — POLICY FILE CHANGED IN THIS PR"
            return (
                f"not ready — {len(failed)} of {n} rules failed: "
                f"{', '.join(failed)} — POLICY FILE CHANGED IN THIS PR"
            )
        if not failed:
            return f"ready — {n} of {n} rules satisfied"
        return (
            f"not ready — {len(failed)} of {n} rules failed: "
            f"{', '.join(failed)}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "summary": self.summary,
            "source": self.source,
            "problems": list(self.problems),
            "policy_changed_in_diff": self.policy_changed_in_diff,
            "base_sha": self.base_sha,
            "tests_green": self.tests_green,
            "rules": [
                {"name": v.name, "passed": v.passed, "detail": v.detail}
                for v in self.rules
            ],
        }


# --- loader ------------------------------------------------------------- #


def load_policy(repo_path: Path) -> PolicyLoad:
    """Read ``<repo_path>/.no_human/merge_policy.yaml``.

    Absent (or a directory at that path) ⇒ the clean default: DEFAULT_POLICY,
    no problems, source "default". Anything else that goes wrong (unreadable,
    oversized, invalid YAML, wrong shape, unknown rule name, bad argument)
    degrades to DEFAULT_POLICY as the rule list, source "file", with one
    problem string per issue found — never raises.
    """
    path = Path(repo_path).expanduser() / POLICY_RELPATH
    try:
        if not path.is_file():
            return PolicyLoad(rules=list(DEFAULT_POLICY), problems=[], source="default")
        if path.stat().st_size > MAX_BYTES:
            return PolicyLoad(
                rules=list(DEFAULT_POLICY),
                problems=[f"{path}: file exceeds {MAX_BYTES} bytes"],
                source="file",
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 — untrusted input never breaks a run
        return PolicyLoad(
            rules=list(DEFAULT_POLICY),
            problems=[f"{path}: could not be read/parsed: {exc}"],
            source="file",
        )

    if raw is None:
        return PolicyLoad(
            rules=list(DEFAULT_POLICY),
            problems=[f"{path}: empty document"],
            source="file",
        )
    if not isinstance(raw, dict):
        return PolicyLoad(
            rules=list(DEFAULT_POLICY),
            problems=[f"{path}: top level is not a mapping"],
            source="file",
        )
    if "rules" not in raw:
        return PolicyLoad(
            rules=list(DEFAULT_POLICY),
            problems=[f"{path}: missing 'rules' key"],
            source="file",
        )

    raw_rules = raw["rules"]
    if not isinstance(raw_rules, list):
        return PolicyLoad(
            rules=list(DEFAULT_POLICY),
            problems=[f"{path}: 'rules' is not a list"],
            source="file",
        )

    parsed: list[Rule] = []
    problems: list[str] = []
    for item in raw_rules:
        rule, problem = _parse_rule_item(item)
        if rule is not None:
            parsed.append(rule)
        if problem is not None:
            problems.append(f"{path}: {problem}")

    return PolicyLoad(rules=parsed, problems=problems, source="file")


def _parse_rule_item(item: Any) -> tuple[Rule | None, str | None]:
    if isinstance(item, str):
        name, arg = item, None
    elif isinstance(item, dict):
        if len(item) != 1:
            return None, f"rule item has {len(item)} keys, expected exactly one: {item!r}"
        (name, arg), = item.items()
        if not isinstance(name, str):
            return None, f"rule name is not a string: {name!r}"
    else:
        return None, f"rule item is neither a string nor a single-key mapping: {item!r}"

    if name not in RULE_NAMES:
        return None, f"unknown rule name: {name!r}"

    if name in _NO_ARG_RULES:
        if arg is not None:
            return None, f"rule {name!r} takes no argument, got {arg!r}"
        return Rule(name), None

    if name in _ENUM_RULES:
        allowed = _ENUM_RULES[name]
        if not isinstance(arg, str) or arg not in allowed:
            return None, f"rule {name!r} needs one of {sorted(allowed)}, got {arg!r}"
        return Rule(name, arg), None

    if name in _LIST_STR_RULES:
        if (
            not isinstance(arg, list)
            or not arg
            or not all(isinstance(x, str) for x in arg)
        ):
            return None, f"rule {name!r} needs a non-empty list of strings, got {arg!r}"
        return Rule(name, list(arg)), None

    if name in _INT_RULES:
        # bool is a subclass of int in Python — reject it explicitly.
        if isinstance(arg, bool) or not isinstance(arg, int) or arg < 0:
            return None, f"rule {name!r} needs a non-negative int, got {arg!r}"
        return Rule(name, arg), None

    return None, f"rule {name!r} has no known argument contract"  # pragma: no cover


# --- glob matcher --------------------------------------------------------- #
#
# Moved to `core/pathglob.py` (the single shared implementation — this
# module's translator was the stricter/more-complete of the two that used to
# exist, so it is the one that moved verbatim). `_check_paths_within` below
# now delegates to `pathglob.matches`.


# --- evaluator ------------------------------------------------------------ #


def _check_review_passed(facts: GateFacts, _arg: Any) -> tuple[bool, str]:
    if facts.review_passed is True:
        return True, "review PASSED on head"
    if facts.review_passed is False:
        return False, "review verdict is FAIL on head"
    return False, "no round has judged this head"


def _check_no_advisory_findings(facts: GateFacts, _arg: Any) -> tuple[bool, str]:
    n = facts.review_advisory_count
    ok = n == 0
    return ok, f"{n} advisory finding{'s' if n != 1 else ''}"


def _check_tests_ran_and_passed(facts: GateFacts, _arg: Any) -> tuple[bool, str]:
    if not facts.tests_ran:
        return False, "tests never ran"
    if facts.tests_failed:
        return False, f"tests: {facts.tests_failed} failed of {facts.tests_passed + facts.tests_failed} run"
    if facts.tests_passed < 1:
        return False, "tests ran but 0 passed"
    return True, f"tests: 0 failed of {facts.tests_passed} run"


def _check_tamper_guard_clear(facts: GateFacts, _arg: Any) -> tuple[bool, str]:
    if not facts.tamper_fired:
        return True, "tamper guard did not fire"
    if facts.tamper_waived:
        return True, "tamper fire waived as legitimate"
    return False, "tamper guard fired, unwaived"


def _check_repro_gate(facts: GateFacts, arg: Any) -> tuple[bool, str]:
    verdict = facts.repro_verdict
    if arg == "pass":
        if verdict == "pass":
            return True, "repro gate pass"
        return False, f"repro gate {verdict if verdict else 'not run'}"
    # pass_or_not_required
    if verdict == "pass":
        return True, "repro gate pass"
    if not facts.repro_required and verdict in (None, "waived"):
        return True, f"repro gate not required (verdict: {verdict if verdict else 'none'})"
    if verdict == "waived":
        return False, "repro gate waived but required"
    return False, f"repro gate {verdict if verdict else 'not run'}"


def _check_verifiers_all_satisfied(facts: GateFacts, _arg: Any) -> tuple[bool, str]:
    if facts.verifiers_ran == 0:
        return True, "0 verifiers ran"
    if facts.verifiers_failed:
        return False, f"{len(facts.verifiers_failed)} verifier{'s' if len(facts.verifiers_failed) != 1 else ''} failed: {', '.join(facts.verifiers_failed)}"
    if facts.verifiers_unavailable:
        n = len(facts.verifiers_unavailable)
        return (
            True,
            f"{facts.verifiers_ran} verifiers, none failed "
            f"({n} no verdict (advisory): {', '.join(facts.verifiers_unavailable)})",
        )
    return True, f"{facts.verifiers_ran} verifiers, none failed"


def _format_failed_checks(names: tuple[str, ...]) -> str:
    """Render failing check names as a detail suffix, e.g. ``" (File
    inventory)"`` or ``" (a, b, c +2 more)"``. Empty ``names`` -> ``""`` (the
    detail string is left exactly as it was before this field existed)."""
    if not names:
        return ""
    shown = names[:3]
    suffix = f" +{len(names) - 3} more" if len(names) > 3 else ""
    return f" ({', '.join(shown)}{suffix})"


def _check_ci(facts: GateFacts, arg: Any) -> tuple[bool, str]:
    state = facts.ci_state
    if arg == "success":
        if state == "success":
            return True, "ci: success"
        if state in (None,):
            return False, "ci: none reported (strict mode requires success)"
        if state == "unknown":
            return False, "ci: unknown (strict mode requires success)"
        return False, f"ci: {state}{_format_failed_checks(facts.ci_failed_checks)}"
    # success_or_unknown
    if state == "success":
        return True, "ci: success"
    if state == "unknown":
        return True, "ci: unknown (tolerated)"
    if state is None:
        return True, "ci: none reported (tolerated)"
    return False, f"ci: {state}{_format_failed_checks(facts.ci_failed_checks)}"


def _check_paths_within(facts: GateFacts, arg: Any) -> tuple[bool, str]:
    patterns: list[str] = arg
    paths = facts.changed_paths
    if not paths:
        return True, "no changed paths"
    offenders = [p for p in paths if not pathglob.matches(p, patterns)]
    if not offenders:
        return True, f"all {len(paths)} changed paths within the allowed set"
    shown = offenders[:5]
    suffix = f" (+{len(offenders) - 5} more)" if len(offenders) > 5 else ""
    return False, f"{len(offenders)} paths outside the allowed set: {', '.join(shown)}{suffix}"


def _check_max_changed_lines(facts: GateFacts, arg: Any) -> tuple[bool, str]:
    limit: int = arg
    n = facts.changed_lines
    if n <= limit:
        return True, f"{n} changed lines (limit {limit})"
    return False, f"{n} changed lines exceeds limit {limit}"


_CHECKS: dict[str, Callable[[GateFacts, Any], tuple[bool, str]]] = {
    "review_passed": _check_review_passed,
    "no_advisory_findings": _check_no_advisory_findings,
    "tests_ran_and_passed": _check_tests_ran_and_passed,
    "tamper_guard_clear": _check_tamper_guard_clear,
    "repro_gate": _check_repro_gate,
    "verifiers_all_satisfied": _check_verifiers_all_satisfied,
    "ci": _check_ci,
    "paths_within": _check_paths_within,
    "max_changed_lines": _check_max_changed_lines,
}


def _evaluate(rules: list[Rule], facts: GateFacts, problems: tuple[str, ...], source: str) -> PolicyVerdict:
    verdicts = []
    for rule in rules:
        check = _CHECKS[rule.name]
        passed, detail = check(facts, rule.arg)
        verdicts.append(RuleVerdict(name=rule.name, passed=passed, detail=detail))
    all_problems = list(problems)
    if facts.policy_changed_in_diff:
        # One mechanism, not two: a self-authored gate is recorded as a
        # `problems` entry so the existing `ready = ... and not problems ...`
        # line below is the ONLY place that forces `ready=False` for it —
        # there is no separate `and not facts.policy_changed_in_diff` clause
        # to keep in sync.
        all_problems.append(
            f"{POLICY_RELPATH} was itself changed in this diff — a coder "
            "cannot author its own merge gate"
        )
    ready = bool(rules) and not all_problems and all(v.passed for v in verdicts)
    # Derived from facts directly, not from whether `tests_ran_and_passed` is
    # in `rules` — a repo-local policy that drops that rule must not silently
    # disable the staleness check `stale_base_reason()` performs at read time.
    tests_green = bool(facts.tests_ran and not facts.tests_failed and facts.tests_passed >= 1)
    return PolicyVerdict(
        ready=ready,
        rules=tuple(verdicts),
        source=source,
        problems=tuple(all_problems),
        policy_changed_in_diff=facts.policy_changed_in_diff,
        base_sha=facts.base_sha,
        tests_green=tests_green,
    )


def evaluate(rules: list[Rule], facts: GateFacts, *, source: str = "memory") -> PolicyVerdict:
    """Evaluate an explicit rule list against gate facts. Pure function; no
    file I/O. ``source`` defaults to "memory" — this rule list did not come
    from `load_policy`, so it should not be labelled "file". Callers that
    loaded rules from disk should prefer :func:`evaluate_repo`, which carries
    the real load source through; a caller with its own loaded rules can
    still pass an explicit ``source="file"``."""
    return _evaluate(rules, facts, (), source=source)


def evaluate_repo(repo_path: Path, facts: GateFacts, *,
                  extra_problems: tuple[str, ...] = ()) -> PolicyVerdict:
    """``load_policy`` + :func:`evaluate`, carrying ``source``/``problems``
    from the load into the verdict. A verdict built from a load with any
    problem is never "ready" — fail closed.

    ``extra_problems`` is for the CALLER's own evidence failures — a diff it
    could not read, a fact it could not establish. They join the load's
    problems in the one ``ready = ... and not all_problems ...`` line, so a
    caller never has to invent a second way to force "not ready", and the
    reason is rendered to the human like any other problem."""
    load = load_policy(repo_path)
    return _evaluate(load.rules, facts,
                     tuple(load.problems) + tuple(extra_problems),
                     source=load.source)


# --- PrEvidence adapter ---------------------------------------------------- #


def facts_from_evidence(
    evidence: Any,
    *,
    changed_paths: list[str] | tuple[str, ...] = (),
    changed_lines: int = 0,
    repro_verdict: str | None = None,
    repro_required: bool = False,
    tamper_adjudications: list[dict] | tuple[dict, ...] | None = None,
    ci_failed_checks: list[str] | tuple[str, ...] | None = None,
    base_sha: str = "",
) -> GateFacts:
    """Adapt a `core.pr_evidence.PrEvidence` (plus the facts it deliberately
    does not carry — changed paths/lines, and the repro gate's verdict,
    which lives in the orchestrator's attempt-scoped state, not on the
    evidence object) into a flat :class:`GateFacts`.

    Deliberately duck-typed on *evidence* (reads attributes, not an isinstance
    check): `pr_evidence.py`'s own boundary is "never import the review
    package", and this module mirrors that discipline by not importing
    `core.pr_evidence` either — the two modules only agree on shape.

    ``tamper_adjudications`` should be the RAW, unfiltered adjudication list
    (``task.context["tamper_adjudications"]``) — every entry the tamper
    guard ever produced for this run, not just the ones some other reader
    already decided were legitimate. ``evidence.tamper`` (`core/pr_evidence.
    py`) is NOT that: per its own docstring it is pre-filtered by
    `Orchestrator._tamper_data` to `verdict == "LEGITIMATE"` entries only, so
    a caller that falls back to it (``tamper_adjudications=None``) can only
    ever observe "did not fire" or "fired and waived" — an unwaived fire is
    structurally invisible through that field, because the filter has
    already removed it before this function ever sees it. Passing the raw
    list is what makes an unwaived fire (TAMPERING / CANNOT_DECIDE) visible
    at all, so callers that HAVE it should always pass it; the fallback
    exists only for callers that genuinely don't (e.g. tests exercising the
    evidence-only path on purpose).
    """
    review_verdict = getattr(evidence, "review_verdict", None) or {}
    review_passed: bool | None = None
    # "unmatched" (rounds exist, but none is stamped for THIS head) and a
    # never-run review both mean "no round has judged this head" — `_check_
    # review_passed` already reads that as `review_passed=None` -> not ready.
    if review_verdict.get("rounds") and not review_verdict.get("unmatched"):
        verdict = review_verdict.get("verdict")
        if verdict:
            review_passed = str(verdict).upper() == "PASSED"
    review_advisory_count = int(review_verdict.get("advisory_count", 0) or 0)

    tests = getattr(evidence, "tests", None) or {}
    tests_ran = bool(tests.get("ran")) if isinstance(tests, dict) else False
    tests_failed = int(tests.get("failed", 0) or 0) if isinstance(tests, dict) else 0
    tests_passed = int(tests.get("passed", 0) or 0) if isinstance(tests, dict) else 0

    if tamper_adjudications is not None:
        raw = tamper_adjudications
    else:
        raw = getattr(evidence, "tamper", None) or []
    # Duck-typed dict check only — see the module-boundary note above; this
    # module never imports `review.tamper_adjudication` to compare against
    # its verdict enum, it compares the literal string the adjudicator
    # writes (`review/tamper_adjudication.py`'s `Verdict.LEGITIMATE.value`).
    tamper_entries = [e for e in raw if isinstance(e, dict)]
    tamper_fired = len(tamper_entries) > 0
    tamper_waived = tamper_fired and all(
        e.get("verdict") == "LEGITIMATE" for e in tamper_entries
    )

    verifiers = getattr(evidence, "verifiers", None) or []
    verifiers_ran = len(verifiers)
    # An unavailable (no-verdict-after-retry) verifier is advisory, not a
    # failure — it must not land in `verifiers_failed` (which drives
    # `_check_verifiers_all_satisfied`'s ready=False), but it is also not a
    # pass, so it gets its own bucket rather than being silently dropped.
    verifiers_failed = tuple(
        sorted(
            str(v.get("verifier_id", "")) for v in verifiers
            if isinstance(v, dict) and not v.get("passed") and not v.get("unavailable")
        )
    )
    verifiers_unavailable = tuple(
        sorted(
            str(v.get("verifier_id", "")) for v in verifiers
            if isinstance(v, dict) and v.get("unavailable")
        )
    )

    ci_state = getattr(evidence, "ci_state", None)
    ci_state = str(ci_state) if ci_state else None

    if ci_failed_checks is None:
        ci_failed_checks = getattr(evidence, "ci_failed_checks", None) or ()
    ci_failed_checks = tuple(str(n) for n in ci_failed_checks)

    policy_changed_in_diff = any(
        pathglob.normalize_path(p) == POLICY_RELPATH for p in changed_paths
    )

    return GateFacts(
        review_passed=review_passed,
        review_advisory_count=review_advisory_count,
        tests_ran=tests_ran,
        tests_failed=tests_failed,
        tests_passed=tests_passed,
        tamper_fired=tamper_fired,
        tamper_waived=tamper_waived,
        repro_verdict=repro_verdict,
        repro_required=repro_required,
        verifiers_ran=verifiers_ran,
        verifiers_failed=verifiers_failed,
        verifiers_unavailable=verifiers_unavailable,
        ci_state=ci_state,
        ci_failed_checks=ci_failed_checks,
        changed_paths=tuple(changed_paths),
        changed_lines=changed_lines,
        policy_changed_in_diff=policy_changed_in_diff,
        base_sha=base_sha,
    )


# --- staleness ------------------------------------------------------------ #


def stale_base_reason(mp: dict | None, trunk_sha: str) -> str | None:
    """Pure, no I/O: compares a persisted verdict dict (`PolicyVerdict.
    as_dict()`, as stored at `task.context["merge_policy"][<head_sha>]`)
    against the caller's already-resolved local trunk tip sha, and returns a
    human-legible reason to refuse the verdict as merge-ready, or `None` if
    it should be trusted as-is.

    Fails OPEN on everything it cannot establish — this can only ever turn a
    ready verdict into a refused one, never the reverse, and only when both
    shas are known and differ:

    - `mp` not a dict, or `tests_green` falsy/absent (never ran, or failed):
      `None` — untouched (AC4). This function never changes the behaviour of
      a task with no recorded green or a failing one.
    - `tests_green` true but `base_sha` absent/empty (a legacy verdict
      stamped before this field existed): `None` — fail open. The bug is
      fixed forward: the next push re-stamps a verdict with a real
      `base_sha`.
    - `tests_green` true, `base_sha` present, but `trunk_sha` is unknown
      (`""` — the caller could not resolve trunk locally): `None` — cannot
      prove staleness without a trunk tip to compare against.
    - `tests_green` true, `base_sha` present, `trunk_sha` known, and they
      match: `None` — the green was measured against trunk's current tip.
    - `tests_green` true, `base_sha` present, `trunk_sha` known, and they
      differ: a reason string naming both shas — trunk moved since the
      suite ran, so the green describes a tree that will not land.
    """
    if not isinstance(mp, dict):
        return None
    if not mp.get("tests_green"):
        return None
    base_sha = mp.get("base_sha") or ""
    if not base_sha:
        return None
    if not trunk_sha:
        return None
    if base_sha == trunk_sha:
        return None
    return (
        f"the recorded green was measured on a tree whose merge base is "
        f"{base_sha[:12]}, but trunk is now at {trunk_sha[:12]} — trunk "
        "moved since that suite ran, so the green describes a tree that "
        "will not land; re-cut the branch onto trunk and re-run"
    )
