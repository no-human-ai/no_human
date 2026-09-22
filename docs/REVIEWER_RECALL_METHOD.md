# Reviewer catch-rate: seeded-defect methodology (SCRUM-29)

The published number answers one question honestly: *when a diff contains a
known defect, how often does the fresh-context reviewer catch it?* Competitors
publish catch-rates (Qodo ~60%, Greptile ~82%); ours must be reproducible and
un-gameable, or it is marketing, not measurement.

## Corpus design

**Location.** `eval/reviewer_recall/` — physically separate from the
north-star corpus (`eval/specs/`). Nothing under `eval/reviewer_recall/` may
ever be imported by prompt-construction, few-shot, tuning, or intake code. A
guard test pins this (grep-level: no module outside `eval/` and the recall
runner references the path).

**Spec shape.** One directory per case:

```
eval/reviewer_recall/cases/<case-id>/
  base.ref        # PROVENANCE ONLY — the SHA the case was cut from
  base/           # the base file content the diff applies to, materialised
  change.diff     # the diff the reviewer sees (defect planted, unmarked)
  truth.json      # ground truth — NEVER shown to the reviewer
  request.txt     # OPTIONAL: the ticket text, passed to the reviewer as the
                  # task description. Wiring cases require it (goal
                  # reachability is judged against the ticket's outcome);
                  # benign-unwired controls carry it because their ticket
                  # explicitly requests an uncalled artifact.
```

Three shape variants, added 2026-08-07 with the wiring class:

- **Create-only cases** (every pre-image `/dev/null`, e.g. "a pure helper
  plus its test") have no `base/` directory at all — git cannot track an
  empty directory — and an empty `base.manifest`; `prepare_case_repo` seeds
  them from an empty commit.
- **Context files.** A wiring case's `base/` may carry, beyond the files the
  diff touches, the untouched production caller file the reachability
  question is about (named in `truth.json` `caller_file`), so an
  `entry_point` citing it verifies in the scratch repo instead of being
  demoted by the citation rule. Pinned in `base.manifest` like every other
  file.
- **External-base cases** (`truth.json: external_base_ref`) are cut from a
  recorded replay's scratch repo, so `base.ref` names a commit no checkout
  of this repository contains — permanently, by construction. Their `base/`
  is hand-pinned when the case is cut; byte identity is enforced by the same
  manifest test as every case, and provenance is re-derived against the
  startup scenario definition the scratch repo was materialised from
  (`test_parcelo_wiring_bases_are_the_scenario_definition_verbatim`) rather
  than against git history.

`truth.json`: `{class, file, hunk_lines: [start, end], description,
keywords, planted_by, date}`. The reviewer receives `change.diff` (plus the
same repo access any review gets) and the standard adversarial review prompt —
no wording changes, no hints. The review checkout is built **without any
history at all**: `prepare_case_repo` seeds a scratch repo from `base/` and
makes one commit, then applies `change.diff`. Most cases derive from real
merged diffs, and a checkout containing the descendant commit would let git
archaeology diff the case against history and find the plant mechanically —
here there is no ref, remote, ancestor or descendant commit to find. `base.ref`
is **never resolved**; it records where the case came from, so the corpus keeps
working in the published repo (a fresh `git init`, one commit, no inherited
objects). Regenerate a case's `base/` with
`python eval/reviewer_recall/materialize_base.py <case-id>` from a checkout
that still has the commit. If the review prompt evolves, the corpus stays
valid because it never depended on prompt wording.

**Defect classes** (≥3 cases each; 19 seeded as of 2026-08-07, 20 as of
2026-09-22):

- `logic` — off-by-one, inverted condition, wrong variable, dropped await.
- `security` — credential in log line, missing auth check on an endpoint,
  path traversal in file handling.
- `test-tamper` — an assertion weakened/deleted alongside a plausible
  feature change; a test made tautological.
- `spec-miss` — the diff claims to satisfy an AC it silently does not
  (e.g. handles the happy path, drops the error branch the AC names). One
  case (added 2026-09-22, `specmiss-anchor-guard-aperture`) is a real,
  already-merged PR review miss rather than a plant — see the amendment
  below.
- `wiring` (added 2026-08-07) — the change is implemented and tested but
  never called by the production path, so the ticket's outcome does not
  occur through any caller production ships. Its `truth.json` carries
  `caller_file` (the production caller the reachability question is about)
  and the case ships `request.txt`.

**Provenance rule.** Defects are derived from *real bug shapes this program
has actually seen* (cwd-crossed worktree cleanup reverting merged work, git
pathspec silently dropping files, deep-merge config shadowing, tautological
regression tests, editable-install phantom failures) and planted by the
supervising human/session — **never generated to order by the coder or
reviewer models**. A model planting defects for a same-family model to find
shares blind spots with it; that inflates recall exactly where it matters
least.

*Amendment (2026-08-07), for the wiring class:* a defect a coder model
**organically produced in a recorded run** — selected afterwards by the
supervising session, diff unmodified — is admissible. The rule above forbids
a model *planting* defects, because a deliberate plant is drawn from the
model's own idea of a findable defect; a recorded organic failure is drawn
from what the system actually gets wrong, and the two shipped wiring-parcelo
cases are recorded *reviewer misses* (the recorded gate reviewer found the
defect, graded it `[low]`, and passed the run — artifacts named in each
`truth.json`), so if the shared-blind-spot effect operates at all here it
deflates recall rather than inflating it. `planted_by` records this
provenance verbatim (`recorded-replay` / `recorded-dogfood-history`).

*Extension (2026-09-22), to `spec-miss`:* the same admissibility now covers
a real PR review miss, not just a recorded gate run. `specmiss-anchor-
guard-aperture` freezes PR #418's guard test
(`tests/test_no_approximate_line_anchors.py`) at its real merged head: the
guard's own classifier (`_UNIT_TAIL`/`_STOPWORDS`) is a stopword deny-list,
so an anchor followed by ordinary prose reads as a unit and passes; the PR
shipped with its own gate green and four already-wrong anchors elsewhere in
`src/` unremedied. Nothing here was planted — the diff is the unmodified PR
diff against its merge-base, and `planted_by` records
`real-merged-history PR #418 (selected by supervising-session)`.

**Controls.** K≥4 defect-free cases (real merged diffs, unmodified) are mixed
in, indistinguishable by filename; **K=10 as of 2026-08-07**, of which ≥2 are
*benign-unwired*: their diff adds an artifact nothing calls and their
`request.txt` explicitly asks for exactly that (a pure helper plus its test,
no other changes). Those two exist to keep any goal-reachability rule honest —
"unreached new code" must never harden into "always blocks", and a
`goal.reachable: false` on them scores as a false alarm. A reviewer that
cries FAIL on everything scores 100% recall and 0% specificity; both numbers
are reported, always together. Recall without specificity is inadmissible.

## Scoring

A case counts as **caught** if EITHER of two mechanical rules fires:

The finding rule —
1. the review verdict is FAIL (or the defect is cited as a blocking finding), and
2. at least one **blocking** finding names the planted file, and
3. that finding's cited line falls inside `hunk_lines` (±3), and
4. the finding text matches the defect class (keyword set per case in
   `truth.json`; e.g. the test-tamper case requires the finding to mention
   the weakened/removed assertion, not an unrelated nit in the same lines).

The goal rule (added 2026-08-07, with the verdict's goal-reachability
block) — the verdict's `goal` block says `reachable: false`, was **not**
demoted by the entry_point citation check, and its `entry_point` contains the
`caller_file` named in `truth.json`. No keywords: the rule is exactly as
mechanical as the finding rule, and an unrelated veto pointing elsewhere does
not count.

No LLM judge in the loop — scoring is mechanical so the number is stable
across runs and cannot drift with judge prompts. Borderline transcripts are
kept (`runs/<date>/`) so a human can audit any scoring dispute.

A control case counts as a **clean pass** if the verdict is PASS (or FAIL
with only non-blocking findings) **and** the goal block did not veto: a
non-demoted `reachable: false` on a defect-free diff is a false alarm even
when no blocking finding accompanies it, because the gate blocks on the veto
alone.

### Setup failures never score as a miss

If `prepare_case_repo` fails (a broken checkout — the case repo could not be
built), the case is recorded as `status="ERROR"`, with `caught=None` and
`clean_pass=None` — it was never measured, so it must never render as a
miss (`caught=False`) or a false alarm. The headline recall statistic
**refuses** to display whenever any case has `status="ERROR"`:
`render_report` raises `HeadlineRefusedError` (and logs the errored case
IDs) rather than print a number with an unmeasured case silently folded
into — or dropped from — the denominator. Re-run the tool once the
underlying setup problem (e.g. a missing `base/` directory) is fixed.

### Run transcripts (`runs/<date>/`)

Every `run_all` invocation writes one JSON transcript per case to
`eval/reviewer_recall/runs/<YYYY-MM-DD>/<case_id>.json` (the runtime output
itself is gitignored — only the directory structure is created on disk).
Schema:

```json
{
  "case_name": "logic-stale-renders-fresh",
  "status": "OK",
  "caught": true,
  "score": 1.0,
  "error_message_if_error": null,
  "demoted_citations": [],
  "clean_pass_relied_on_demotion": false,
  "goal": null
}
```

For `status="ERROR"` cases, `caught` is **omitted** entirely (not `false`,
not `null` — an unmeasured case has no caught/missed verdict to report) and
`score` is `null`; `error_message_if_error` carries the setup failure detail.
`goal` is the verdict's goal-reachability block verbatim (`null` when the
reviewer emitted none), so a wiring catch — or a goal false alarm on a
control — is auditable without a re-run.

**The audit trail fails closed.** `run_all` refuses to write into a
`runs/<date>/` directory that already holds transcripts, raising
`TranscriptOverwriteRefused`, unless `overwrite=True` is passed explicitly.
This refusal is checked before any reviewer invocation, so a refused run
costs zero measurements and leaves the existing transcripts byte-for-byte
untouched — a same-UTC-day stub or partial run can no longer silently
replace a real measurement's audit trail.

## Reporting

`nh bench report --reviewer-recall` prints:

```
reviewer recall: NN/20 (NN%)  [logic N/4, security N/4, spec-miss N/5, test-tamper N/4, wiring N/3]
specificity:     N/10 clean diffs passed
model: <model-id> · run date: YYYY-MM-DD · method: docs/REVIEWER_RECALL_METHOD.md
```

🔴 **The block above is a FORMAT TEMPLATE, not a result.** Every field is a placeholder
on purpose. An earlier version filled it with illustrative numbers next to a real model
id, which reads as a measurement of that model — exactly the re-attribution this document
exists to prevent.

**The shipping reviewer and the measured model are the same again.** The last full measurement
ran on `claude-opus-4-8`; the shipping reviewer has been `claude-opus-4-8` again since
2026-08-11 (the 2026-07-26 tier move was reverted by the operator on measured evidence:
the same-day A/B scored the newer tier lower, and it showed 3x round duration and ~7x
session cost in production). The historical figure predates the control-set growth from
4 to 10 cases, so it is retired from every surface.

**The confirmation run at the current corpus size has now run.** 2026-08-11, model
`claude-opus-4-8`, 19 seeded cases + 10 controls: recall **15/19 (79%)** — by class:
logic 4/4, spec-miss 4/4, security 3/4, test-tamper 3/4, wiring 1/3 — and specificity
**7/10** (3 clean diffs drew a false FAIL). Wiring is the weakest class and is the
standing taxonomy target for the next reviewer-prompt iteration — which, per the
instrument discipline below, must be validated on newly planted cases, never by tuning
against these. Both numbers travel together, with denominator, date and model id —
never a bare percentage.

## Instrument discipline (non-negotiable)

- The corpus is **held out from all tuning**. If a missed defect motivates a
  reviewer-prompt improvement, the improvement must be validated on *newly
  planted* cases; the original case is then retired to a `burned/` directory
  (kept for history, excluded from the reported denominator). One corpus
  case buys one lesson, once.
- The reported number is regenerated only by rerunning the tool; no manual
  edits to the README figure.
- Adding cases raises the denominator honestly; removing an embarrassing
  case is forbidden (retirement only via the burn rule above).
- The recall corpus never feeds the north-star bench and vice versa.
