"""The PR body's evidence PIPELINE (part 2 of the c7da49d4 decomposition).

Part 1 (27e7352b) is the TEMPLATE: a short, scannable body with the verbose
evidence folded into `<details>` blocks. THIS module is the other half — the
attempt's gate outputs (repro, tamper, tests, review verdict, CI/annotation
state) are gathered ONCE into `PrEvidence`, a single structured object, and
every renderer that prints a measured fact ("truth pin") reads it from there.
No renderer may re-derive a gate's output independently: that is what let two
sections print two different answers to the same question.

`PrEvidence` is deliberately a thin, frozen carrier — it stores what each gate
ACTUALLY produced (dicts/lists already in that gate's own shape), not a
re-interpretation of it. The re-interpretation (formatting a sentence a human
reads) stays with the renderer that owns that section's prose; what this
module guarantees is that the renderer has nowhere else to get the fact from.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Any


@dataclass(frozen=True)
class PrEvidence:
    """The attempt's gate outputs, gathered once per `_pr_body` render.

    Fields hold the RAW structured output of each gate — `None` when that
    gate produced nothing for this attempt. A field being `None` is itself a
    fact ("this gate has nothing to say"), so renderers distinguish "no data"
    from "data says nothing happened" the same way they always have.

    * ``repro`` — the verification-receipts gate: ``{"receipts": [...],
      "observable": bool}``.
    * ``tamper`` — the tamper-adjudication gate: the list of LEGITIMATE
      waiver entries, or ``None``.
    * ``tests`` — the orchestrator's own layered test run (`test_evidence`),
      in the shape `testing/runner.py` produces it.
    * ``review_verdict`` — the independent reviewer's verdict trail:
      ``{"rounds": int, "verdict": str, "addressed": [...], "unmatched":
      bool}``, or ``None`` when no round has judged this head.
    * ``verifiers`` — every deterministic verifier verdict from this head's
      gate round (`review/verifiers.py`'s ``VerifierResult.as_dict()``), or
      ``None`` when no verifiers were configured/selected for this head —
      never an empty list standing in for "not run".
    * ``ci_state`` — CI/annotation state for this attempt, or ``None`` before
      CI has reported anything.
    * ``merge_policy`` — the merge-ready policy verdict for this head
      (`core.merge_policy.PolicyVerdict.as_dict()`), or ``None`` when it was
      never computed (evidence-gathering is best-effort; a failed compute is
      advisory, never blocking — see `orchestrator._finalize`). Advisory to
      the human ONLY: nothing in this repo merges, waits, or gates a push on
      this field.
    * ``merge_policy_error`` — the exception CLASS NAME (never the message —
      messages can carry model-authored text and this module's fields are
      rendered) of a merge-policy compute that raised in `_finalize`, or
      ``None``. Two states stay distinguishable: ``(merge_policy=None,
      merge_policy_error=None)`` means no policy was ever attempted for this
      head — say nothing; ``(merge_policy=None, merge_policy_error="...")``
      means the compute was tried and failed — disclose it. A stamped
      ``merge_policy`` always wins over a stale error string.
    * ``reviewer_attribution`` — constraint amendment §6d disclosure: ``""``
      (never ``None`` — this field is a plain flag, not a "gate never ran"
      slot like the others above) when the reviewer ran on the default
      (`CLAUDE_PINNED_ROLES`'s pin, `claude-opus-4-8`), or ``"<backend>
      `<model>`"`` when it ran on an explicit `llm.role_backends.reviewer`
      Settings choice instead. Deliberately NOT a truth pin
      (`truth_pins()`/`review_verdict_pin()` are unchanged by this field —
      the evidence-based review verdict itself is backend-agnostic, per the
      out-of-scope list): this is an honesty disclosure about WHICH backend
      produced the verdict above, not a second verdict.
    """

    repro: dict[str, Any] | None = None
    tamper: list[dict[str, Any]] | None = None
    tests: dict[str, Any] | None = None
    review_verdict: dict[str, Any] | None = None
    verifiers: list[dict[str, Any]] | None = None
    ci_state: Any = None
    merge_policy: dict[str, Any] | None = None
    merge_policy_error: str | None = None
    reviewer_attribution: str = ""
    #: #23 proof ledger: Evidence-table row name → URL of the ledger file
    #: behind it, at the ledger commit (`core/evidence_ledger.py`). Empty
    #: when no ledger was delivered (non-GitHub remote, delivery failure, the
    #: pre-review draft body) — a row then renders exactly as before.
    proof_urls: dict[str, str] = dc_field(default_factory=dict)

    def has(self, field: str) -> bool:
        return getattr(self, field, None) is not None

    # ----------------------- truth pins ----------------------- #
    # A "truth pin" is a concrete, measured-fact sentence the body renders
    # (a count, a verdict, a pass/fail line) — as opposed to prose. Every pin
    # method below returns `None` when its backing field has nothing to say,
    # so "the field is empty" and "the pin was never asked for" are the same
    # code path: a renderer cannot accidentally print a pin its evidence
    # object does not back.

    def review_verdict_pin(self) -> str | None:
        rv = self.review_verdict
        if not rv or not rv.get("rounds"):
            return None
        n = int(rv["rounds"])
        return f"**{rv['verdict']}** — {n} round{'s' if n != 1 else ''}"

    def repro_count_pin(self) -> str | None:
        if self.repro is None:
            return None
        n = len(self.repro.get("receipts") or [])
        return f"{n} command{'s' if n != 1 else ''} recorded"

    def tests_summary_pin(self) -> str | None:
        t = self.tests
        if not isinstance(t, dict) or not t.get("ran"):
            return None
        verb = "PASS" if t.get("ok") else "FAIL"
        return (f"{verb} — {t.get('passed', 0)} passed, "
                f"{t.get('failed', 0)} failed, {t.get('errors', 0)} errors")

    def ci_state_pin(self) -> str | None:
        # This pin holds the row's UNESCAPED text, while `orchestrator.py`'s
        # `_evidence_section` now renders the CI row through `_table_cell`
        # (a pipe-bearing `ci_state` would otherwise truncate the row — see
        # `orchestrator.py`'s `_TABLE_PIPE`). So when `ci_state` itself
        # contains a `|`, this pin is no longer an exact substring of the
        # rendered body — by design: fixing the truncation necessarily means
        # the pin (a fact string, not markdown) and the row (markdown) can
        # diverge on escaping alone, never on content.
        if not self.ci_state:
            return None
        return f"| CI | {self.ci_state} |"

    def proof(self, key: str) -> str:
        """` · [proof](url)` for the row *key* when the ledger holds its file,
        else "" — appended inside the row's cell, never a cell of its own."""
        url = self.proof_urls.get(key)
        return f" · [proof]({url})" if url else ""

    def log_anchors(self) -> dict[str, str]:
        """kind → URL into the ledger's `verification.md` on that kind's last
        command (`proof_urls` keys `verification:<kind>`), for the fold
        summaries of "How I verified this"."""
        pre = "verification:"
        return {k[len(pre):]: u for k, u in self.proof_urls.items() if k.startswith(pre)}

    def headline(self) -> str:
        """#23: the facts a human reads first — the reviewer's verdict, the
        orchestrator's own test run, the merge-policy verdict — as ONE quoted
        line the body renders ahead of `## Evidence`. Read from the same
        fields the table renders from, worded differently from the table
        cells (`Review passed (2 rounds)` vs `**PASSED** — 2 rounds`) so no
        gate output is rendered twice. "" when no gate has a verdict."""
        parts: list[str] = []
        rv = self.review_verdict
        if rv and not rv.get("unmatched"):
            n = int(rv["rounds"])
            word = "passed" if rv["verdict"] == "PASSED" else "not passed"
            parts.append(f"**Review {word}** ({n} round{'s' if n != 1 else ''})")
        te = self.tests
        if isinstance(te, dict):
            def _n(key: str) -> int:
                try:
                    return int(te.get(key) or 0)
                except (TypeError, ValueError):
                    return 0
            p, f, e = _n("passed"), _n("failed"), _n("errors")
            counts = (f" ({p} passed, {f} failed" + (f", {e} errors" if e else "")
                      + ")") if p + f + e else ""
            if te.get("ran") is False or (te.get("invocation_error") and not counts):
                parts.append("**Tests not run**")
            elif "ok" in te:
                # `ok` is the runner's own verdict (`rc == 0`), not the counts:
                # `pytest && mypy` with mypy failing is ok=False, 0 failed.
                parts.append(f"**Tests {'passed' if te['ok'] else 'failed'}**{counts}")
            elif counts:
                parts.append(f"**Tests**{counts}")
        mp = self.merge_policy
        if mp:
            if mp.get("problems") or mp.get("policy_changed_in_diff"):
                parts.append("**Merge policy** ⚠️ see below")
            else:
                parts.append("**Merge policy** "
                             + ("ready" if mp.get("ready") else "not ready"))
        return "> " + " · ".join(parts) + "\n\n" if parts else ""

    def verifiers_pin(self) -> str | None:
        """Re-implemented over dicts, not `verifiers.summary_line`: this
        module's own boundary is "never import the review package", and that
        holds for the verifiers module too — the shape is duplicated here on
        purpose rather than crossing it.

        An unavailable verifier (no verdict after its bounded retry) is its
        own third state — neither a pass nor a genuine failure — and must
        read as such here too, not be folded into "failed".
        """
        if not self.verifiers:
            return None
        total = len(self.verifiers)
        failed = sorted(
            v.get("verifier_id", "") for v in self.verifiers
            if not v.get("passed") and not v.get("unavailable")
        )
        unavailable = sorted(
            v.get("verifier_id", "") for v in self.verifiers if v.get("unavailable")
        )
        if not failed and not unavailable:
            return f"{total} of {total} satisfied"
        if failed:
            detail = f"{len(failed)} of {total} failed — {', '.join(failed)}"
            if unavailable:
                detail += (
                    f"; {len(unavailable)} no verdict (advisory) — "
                    f"{', '.join(unavailable)}"
                )
            return detail
        checked = total - len(unavailable)
        return (
            f"{checked} of {total} satisfied, {len(unavailable)} no verdict "
            f"(advisory) — {', '.join(unavailable)}"
        )

    def merge_policy_pin(self) -> str | None:
        """The merge-ready policy's own summary sentence — re-derived nowhere
        else; this returns exactly `PolicyVerdict.summary` as it was computed
        and persisted, never a second opinion of it."""
        mp = self.merge_policy
        if not mp:
            return None
        summary = mp.get("summary")
        return str(summary) if summary else None

    def truth_pins(self) -> dict[str, str]:
        """``{backing field name: exact fact sentence}`` for every truth pin
        this object can justify. A PR body must never render a measured-fact
        sentence that is not a value here — `tests/test_pr_evidence.py`'s
        `test_every_rendered_truth_pin_has_backing_evidence` asserts exactly
        that against a real `_pr_body()` render.
        """
        pins: dict[str, str] = {}
        for field, pin in (
            ("review_verdict", self.review_verdict_pin()),
            ("verifiers", self.verifiers_pin()),
            ("repro", self.repro_count_pin()),
            ("tests", self.tests_summary_pin()),
            ("ci_state", self.ci_state_pin()),
            ("merge_policy", self.merge_policy_pin()),
        ):
            if pin:
                pins[field] = pin
        return pins


_DETAILS_RE = re.compile(r"<details>.*?</details>", re.S)


def visible_chars(body: str) -> int:
    """Characters of *body* outside every `<details>...</details>` block —
    what a reader sees without expanding anything, since GitHub renders
    `<details>` closed by default. This is the "scannable" length the
    operator's short-PR-body directive targets, as opposed to the raw total
    length (which collapsing does not shrink — it only relocates text behind
    a fold).
    """
    return len(_DETAILS_RE.sub("", body))

