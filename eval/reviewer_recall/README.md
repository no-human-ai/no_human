# Reviewer-recall corpus — HELD OUT FROM ALL TUNING

Method: `docs/REVIEWER_RECALL_METHOD.md`. Read it before touching anything here.

Each `cases/<id>/` holds `base.ref` (provenance only — the SHA the case was cut
from), `base/` (the base file content the diff applies to, materialised inside
the case), `change.diff` (what the reviewer sees — for seeded cases the defect
is planted, unmarked), `truth.json` (ground truth — never shown to the
reviewer), and `base.manifest` (the git blob id of each `base/` file — a
byte-identity pin that outlives the history). A case may also ship
`request.txt` (the ticket text, passed to the reviewer as the task
description — required for `wiring` cases and carried by the benign-unwired
controls); see the method doc's "Spec shape" for the three shape variants
added 2026-08-07 (create-only cases, context files, external-base cases).

`base.manifest` lines are `<blob id>  <path>`, with an optional third column
holding the marker `scrubbed`. Column 1 always hashes the bytes **on disk**. The
third column appears only on a file the materialiser de-identified after
extracting it, and declares exactly that — the fixture's bytes deliberately
differ from `base.ref`. See "De-identification" below.

The marker used to be the **origin blob id**, and that was removed on
2026-08-02 (see "The manifest must not index the original"). Provenance is still
verified, just not from a shipped file: `base.ref` is a full commit id, so
`<base.ref>:<path>` already names the origin blob, and the test re-derives it
from git.

A case directory must hold all of `base.ref`, `change.diff` and `truth.json`.
`load_cases` skips a directory with **none** of them (`__pycache__` and the
like) and **raises** on one with only some: quietly dropping a half-built case
would move the denominator with nothing to say so, and would slip past
`HeadlineRefusedError`, which can only refuse over cases that became results.

## The corpus is self-contained (2026-07-30)

`prepare_case_repo` used to rebuild each case with `git archive <base.ref>`.
That pinned the corpus to this repo's history — and no_human ships as a fresh
`git init` with a single commit, so every case would have ERRORed at
`git archive` in the published repo. The base content now lives in
`cases/<id>/base/` and **`base.ref` is never resolved**. Regenerate it for a new
case with `python eval/reviewer_recall/materialize_base.py <case-id>`, run from
a checkout that still has the commit.

A case's `base/` holds the files its `change.diff` touches (83 files across
the 29 cases as of 2026-08-07; it was 70 files across 20 cases when this
section was written), each pinned by blob id in `base.manifest` — plus, for
`wiring` cases only, the untouched production caller file the reachability
question is about, pinned the same way. That
narrowness is worth naming: the old full-tree checkout also shipped every
OTHER case's `truth.json` into the reviewer's scratch repo — up to 16 of them,
in 10 of the then-20 cases. It carries none now. That is the full set the scratch repo is read
for: the runner invokes the reviewer with `diff_override`, which puts it on the
single-turn, no-tools path (`review/reviewer.py`, gate mode) — it never
explores the tree, it only gets the diff. The one place the tree is read is
`_verify_citations` (and, since 2026-08-07, the same check over the goal
block's `entry_point`), which demotes a blocking finding whose cited
`file:line` does not exist — which is exactly why a wiring case materialises
its caller file: a correct entry_point citing it must verify, not demote.

**Behavioural delta, stated plainly:** under `git archive` the whole base tree
was present, so a blocking finding citing a file *outside* the diff still
verified and stayed blocking. It now demotes to advisory, because that file is
not materialised. This can only affect a finding that names a file the reviewer
was never shown — with only the diff in its prompt, such a citation is a
hallucinated location. It cannot change a seeded case's `caught` (the planted
file is always in the diff); it can only make a control *more* likely to score
a clean pass.

That is recorded, not just warned about. `ReviewOutcome.demoted_citations`
carries `ReviewDecision.demoted_citations` through scoring; a control that
passed with demotions is marked `clean_pass_relied_on_demotion`, its `reason`
names them, `render_report` prints a ⚠ line under the specificity number
listing each one, and every `runs/<date>/<case>.json` transcript carries both
fields. When this was written the corpus had 4 controls, so one flip was 25
points, and published specificity had sat at 2/4 and 0/4; at 10 controls one
flip is still 10 points. A caveat nobody can act on is not good enough: read
those lines before quoting a specificity number.

## De-identification (2026-07-31)

A base fixture is cut from a **pre-sweep** commit — that is what makes it a base
— so it can carry vendor/employer terms that the tree-wide de-identification
sweep has since removed from live source. `eval/reviewer_recall/` is classified
`ship` in `EXPORT_CLASSIFICATION.txt`: this corpus is published, so such a term is a public leak.

That happened. On 2026-07-30 one branch swept a term out of live source and
another materialised base fixtures from pre-sweep commits; merged in that order,
`main` went red with 16 hits on exportable paths, plus 6 more that an inherited
`# term-ok` comment had been hiding. Twelve files across eleven cases.

The fix is in the materialiser, not in anyone's memory: `materialize_base.py`
pipes every blob through `scrub()` on the way to disk, using the substitution
list in the private supplement (absent in the export, where this script cannot
run anyway). Re-running it is therefore safe and idempotent. The replacements
are the ones the live sweep chose for the same identifiers, so a fixture still
reads as a realistic file.

**What this must never do is change what the corpus measures.** Verified for
this pass: 22 lines rewritten, every one of them a vendor term inside a comment
or a string, and every one outside every pre-image hunk range in that case's
`change.diff` — so no context line moved and all 20 diffs still apply.
`truth.json` and `change.diff` are untouched in all 20 cases.
`test_scrubbed_fixtures_are_their_origin_blob_put_through_scrub` asserts both
directions of the manifest's third column, so neither an undeclared edit to a
fixture nor a stale `scrubbed` declaration can pass.

The materialiser **fails closed**: with the private supplement absent it loads
no substitutions, and `scrub()`/`materialise()` then raise
`ScrubRulesUnavailable` rather than writing the fixtures through unchanged. That
matters because the failure was silent — re-materialising without the supplement
used to write pre-sweep bytes to disk and re-pin `base.manifest` to them, leaving
a tree that looked internally consistent and had never been de-identified.
Importing the module still works everywhere, because the byte-pin test imports
it; only running it refuses.

The public export has no supplement **and** no pre-scrub history, so it can
trigger both refusals at once and must not be told the wrong one. `materialise`
therefore probes the history first and raises `HistoryUnavailable` — "this
checkout is not one the materialiser can run in", which is expected and
harmless — leaving `ScrubRulesUnavailable` to mean what it says: a checkout that
*can* resolve `base.ref` has lost its substitution list. An earlier version
checked the rules first, and an independent review caught it firing in the built
export while asserting the opposite.

## The manifest must not index the original (2026-08-02, re-measured 2026-08-04)

Column 3 held the **origin blob id** — the git object id of the file's bytes
*before* de-identification. `*.manifest` is classified `ship`, and all **31**
recorded ids were reachable from `main`, so a published fixture named the
un-de-identified original precisely enough to `git cat-file` it. That count is
not a constant: it was 20 when this was first written and is 31 at `main`
9198adf1, spread over 15 of the 20 cases, because it rises with every
substitution rule added. Any figure below that counts origin blobs was
re-measured at that commit.

That is a live index, and closing it could not be left to the history rewrite:

- **Blobs are content-addressed.** Rewriting the history's commits changes every
  commit id and no blob id. Only rewriting a blob's *content* retires its id, so
  the pointers survive a rewrite that does not happen to touch those exact blobs.
- **Being flagged is not being rewritten.** The product's 102-term scanner does
  flag all 31 origin blobs (`build_public_export.verify_tree` over the recovered
  originals: **269 hits, 31 of 31 files dirty, 44 distinct terms**; the 31
  shipped fixtures scan **clean, 0 hits**, which is the negative control) — but
  the scanner's 102 terms and the materialiser's 51 substitution rules are
  different sets, and a term a scanner can *see* is not automatically a term a
  filter *replaces*. The ticket prefix is the worked example: it is handled by
  six literal `BASE_FIXTURE_SCRUB` rules, added by hand after readers found the
  8-, 3- and single-digit forms one at a time, because no shape rule in the tree
  could see them. **10 of the 31** origin blobs carry a full ticket id (prefix
  plus six or more digits).
- **A rewrite does not retract what is already cloned.** Every existing clone,
  fork and mirror keeps the old objects, and the shipped manifest keeps naming
  them.

So the pointer is deleted rather than left to expire. (An earlier framing of this
finding had the scanner flagging only 13 of 20 and 7 surviving a rewrite
untouched; that split did not reproduce and is not the reason the column had to
go.) The prefix appears in **zero** files in the published tree — the 8 tracked
files that still carry a full ticket id are all under `eval/northstar_tasks/` and
all dropped from the export, verified against the real published tree rather than
a local build — so these blobs are its pre-scrub originals, reachable through a
shipped index.

The column is now a fixed marker.
`test_manifest_declares_the_scrub_without_indexing_the_original` enforces the
rule structurally: nothing after column 1 may look like an object id (from four
hex characters up — git's own floor, and a four-character prefix was verified to
resolve to one of these blobs). Column 1 is exempt because it hashes the
**shipped** bytes — its pre-image is the file next to it, so it indexes nothing
that is not already published.

### What this cost

The origin blobs are reachable from `main` — **31 of 31 resolve in a default
`git clone` of the private remote** — so in a fresh clone column 3 would have let
`_scrub(cat-file <origin>) == <on disk>` run for all 31 files. Its replacement
resolves the origin through the `base.ref` **commit**, and a *default* clone
resolves only **4 of the 20** cases — so that check now runs in full or skips
outright, and in a default clone it skips. It does **not** skip after a PR-ref
fetch, which is the whole subject of the next section. A portable verification
aid was traded for closing the leak, which is the right way round: the column
that made the check portable was the column that published the index.

### What is still recoverable, and what actually closes it

The column is gone. **The content it named is not.** Measured 2026-08-04 against
a real network clone of the private remote (`find .git/objects -type f -links +1`
→ 0, no `objects/info/alternates`, so nothing is borrowed from a local object
store), scanned with the repo's own gate — `build_public_export.verify_tree` at
102 terms — with both controls run: the 31 shipped scrubbed fixtures scan
**CLEAN, 0 hits over 31 files**, and the 31 recovered originals **FAIL, 269 hits,
31 of 31 dirty, 44 distinct terms**. What comes back is genuinely private
content, not a theoretical exposure.

**A default clone — 48 distinct pre-scrub blobs.** Every `change.diff` carries
git's own `index <old>..<new>` headers, which abbreviate blob ids to 7 hex (all
132 of them are exactly 7). Across the 20 diffs those headers name 132 distinct
abbreviations, **115 of which resolve to a blob in a plain `git clone`**, and
**48 of those 115 are pre-scrub** — they fail the 102-term gate. None of this
needs `base.ref`, and none of it needs anything beyond `git clone`.

**A clone plus one documented command — all 20 `base.ref` commits.** A default
clone fetches `refs/heads/*` and tags, and resolves 4 of the 20 `base.ref`
commits. `git fetch origin '+refs/pull/*:refs/remotes/pull/*'` — one standard,
documented command — takes that to **20 of 20**, after which `<base.ref>:<path>`
returns all 31 manifest-named originals byte-for-byte. Measured on the private
remote, which publishes **241 refs: 120 `refs/heads/*`, 118 `refs/pull/*`, one
annotated tag (2 lines) and `HEAD`**; every commit a default clone cannot see is
reachable from a PR ref. An earlier draft of this section scoped the residual to
"a clone of the published repo" and put it at **one** blob. That is true only of
the default clone, and stating it as *the* residual understated it 20×. Scoping a
measurement to a command nobody is obliged to stop at is not a measurement.

**Removing the column cut a redundant path *inside a clone*, not the content.**
All 31 blobs the column named are also named by a `change.diff` shipped in the
same directory: the manifest-only set is **empty**. Distinct recoverable
pre-scrub blobs: **48 before this change, 48 after.** Inside a clone, what fell
is the number of *paths* to the same bytes, from two to one.

**Outside a clone it was not redundant at all.** A reader who never clones can
still ask GitHub for a blob by id, and that endpoint requires the **full 40 hex**:
a 7- and a 9-character prefix both return `422 Unprocessable Entity`, the full id
returns `200`. `change.diff` only ever publishes 7. So column 3 was the sole
shipped source of an id you could dereference **remotely**, and deleting it
closes the remote-lookup path outright. That distinction matters more than it
looks — see the next paragraph.

**The history rewrite did not retire the ids, and that is now measured rather
than predicted.** The publish target's history was replaced on 2026-08-04 with a
filtered rewrite (755 commits, terminal tree from the export) — it is no longer
the fresh single-commit `git init` this section used to describe. Re-measured
against the real target: a default clone of it contains **0 of the 31** origin
blobs, and the term one of them carries appears in **zero** blobs across all its
refs, so the filtered *history* is clean. But **2 of the 31 origin blobs are
still served by full-SHA lookup from that repository today** (`GET
/repos/<owner>/<repo>/git/blobs/<40-hex>` → `200`, content confirmed pre-scrub by
diffing it against the shipped fixture, which differs by exactly the one
substituted line). They are unreachable objects that survived the replacement and
have not been garbage-collected. Content-addressing is why: the rewrite retired
no id, and the shipped manifest published exactly the address needed to fetch
what the rewrite left behind. The instrument was validated both ways before this
was believed — the same endpoint returns `200` for a tree-reachable blob in the
same repo, and `200` for these two ids in the private repo.

**What still has to close it is the publish target.** The residual is a property
of *the repository this corpus is published from*, not of the manifest and not of
a rewrite. The current target publishes **zero `refs/pull/*`**, which is the
shape this section asks for. The safe shape remains a **new, empty, public repo
that has never had a pull request**.

> **Mandatory pre-publish check.** `git ls-remote <target>` must print **zero**
> `refs/pull/*`. One command, run before every publish.

**Never** flip the existing private repo to public instead. Its 118 PR refs go
public with it, and the residual goes from 48 blobs behind a `change.diff` to
every pre-scrub original the corpus pins, recoverable by the fetch above.

**Severity, stated plainly.** Both the working repo and the publish target are
**private today** — so there is no outside reader and none of this has leaked.
The claim is forward-looking, and it bites in exactly the scenario this section
is about: the moment the corpus is published from a repo that has PR refs, or
from a repo whose object store still holds pre-scrub objects the manifest names.
It is not a live breach, and it must not be written up as one. It is also not a
non-issue: the mitigation is a decision taken once, at publish time, and it is
invisible and irreversible afterwards.

**Why `base.ref` stays.** It is the corpus's provenance record and the thing the
surviving verification is built on. Removing it would delete the audit trail
without removing any content — `change.diff` publishes the same blobs regardless.

**Why the `change.diff` index headers stay.** Stripping those lines changes the
diff text the reviewer is shown in all 20 cases, and this corpus is held out, so
it needs a re-measure rather than a quiet edit. This is the one open item; the
publish-target rule above is what keeps it from mattering.

Rules that keep the number honest:

- **Never** use these diffs, truths, or their lessons to tune the reviewer
  prompt, few-shot examples, or intake. A case that motivates a change is
  retired to `burned/` and leaves the denominator (see the method doc).
- **Never** mix these into the north-star corpus, or vice versa.
- The review checkout for a case must not contain history descending from
  `base.ref`, or git archaeology finds the plant. It no longer contains any
  history at all: the scratch repo is seeded from `base/` and gets exactly one
  commit, so there is no ref, remote, ancestor or descendant to dig through.
- Every seeded case's `change.diff` must keep the diff's own tests green —
  a plant the test suite catches measures pytest, not review skill. Verify
  before adding a case.
- Controls (`class: control`) are real merged diffs, unmodified; specificity
  over them is reported alongside recall, always.

## The 2026-08-07 expansion: the wiring class, and its controls

Cut after the goal-reachability gate change (a reviewer twice found the
"implemented in the rate engine, never wired through the sole production
caller" defect, graded it `[low]`, and passed — the recorded artifacts are
named in the wiring cases' `truth.json`):

- **3 `wiring` seeded cases** — two cut unmodified from those recorded
  replays (external base.ref; base pinned byte-for-byte to the startup
  scenario definition by
  `test_parcelo_wiring_bases_are_the_scenario_definition_verbatim`) and one
  cut unmodified from real merged dogfood history (a verification module
  landed invokable only by a documented manual command; a later one-line
  commit wired it, which is the record that wiring was the missing piece).
- **6 new controls** (4 → 10), all real merged diffs, unmodified. Two are
  *benign-unwired*: their diff adds an artifact nothing calls and their
  `request.txt` asks for exactly that — the canary shape that keeps the goal
  rule from hardening into "unreached new code always blocks".
- **Scoring** gained the goal rule and the control goal-veto false alarm;
  see the method doc's "Scoring".

**Residual-measurement staleness, stated before someone quotes the numbers
above:** every figure in "The manifest must not index the original" and "What
is still recoverable" (48 blobs, 132 index headers, 115 resolving, 4-of-20 /
20-of-20 base.ref reachability) was measured 2026-08-04 **on the then-20-case
corpus**. The 9 new `change.diff` files add index headers of their own, and
the one whose base fixture carries a `scrubbed` marker
(`wiring-demo-verify-sync`) names a pre-scrub blob by
7-hex abbreviation exactly as the old diffs do — so the recoverable-blob
counts are floors now, not totals. Nothing about the CONCLUSION moves: the
mitigation was never those counts, it is the publish-target rule (zero
`refs/pull/*`, `git ls-remote` before every publish), and that rule covers
the new cases identically.

Current denominator: 20 seeded (4 logic, 4 security, 4 test-tamper,
5 spec-miss, 3 wiring) + 10 controls = 30 cases. Target per the method doc:
≥3 per class, ≥4 controls — met. (Until 2026-09-22 this read "19 seeded +
10 controls = 29", moved by `specmiss-anchor-guard-aperture` — a real,
already-merged PR #418 review miss, not a synthetic plant, admitted under
the 2026-08-07 real-recorded-defect provenance exception. Until 2026-08-07
it read "16 seeded + 4 controls = 20"; before 2026-07-30 it said "12 seeded
(3/3/3/3)" — stale, not a retirement. `load_cases` reports 30 and
`render_report` breaks out 4/4/4/5/3.)
