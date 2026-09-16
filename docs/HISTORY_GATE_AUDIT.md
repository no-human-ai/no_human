# History gate hit audit — the 214 blob hits, read

**Status: read-only findings report.** This document does not fix, tune, or
narrow any detector, does not rewrite public git history, and does not flip
`NH_GUARD_MODE`. It answers one question nobody had answered before: *what
are the 214 blob / 33 message / 32 identity hits the full-history scan on the
public repo reports, actually?*

## 1. Why this document exists

`scripts/verify_public_history.py` run without `--since` against the public
repo at commit `a8a04496` reported:

```
history gate: FAILED — 214 blob, 0 path, 33 message, 32 identity, 0 tag hit(s); 0 missing, 137 extra file(s)
```

`NH_GUARD_MODE` defaults to `report`, not `enforce`, so this failure has never
blocked a push — it has only ever been a line in a log. Two independent
truncations meant nobody had read the actual hits behind that count:
`main()`'s own printer caps each hit group at 200 lines, and `nh-guard`
(the pre-push hook that invokes the scanner) logs only the last 600
characters of the scanner's output. A count cannot distinguish "214 genuine
employer-trace leaks" from "214 matches of a broad shape class against
ordinary contributor identities and test fixtures" — those two readings
demand opposite responses. This document replaces the guess with a read.

## 2. Method

- Built `scripts/history_gate_hit_report.py` — a read-only, additive tool. It
  does not edit `verify_public_history.py`; it *bypasses* the 200-hit cap by
  loading that module by path (`importlib.util.spec_from_file_location`,
  the exact mechanism the scanner already uses on itself) and calling
  `scan_history()` directly, the same arming sequence `main()` uses
  (`_load_builder` → `_terms_or_die` → `load_shapes` → `load_message_terms` →
  `scan_history`), then serializing the full, uncapped `HistoryReport` to
  JSON instead of printing the first 200 of each group.
- Ran the uncapped scan once, in the foreground, against the public repo at
  `a8a04496` (no `--since`, matching the failing gate run): ≈50 minutes for
  8,861 unique blobs. Output: `.no_human/scratch/history-audit/a8a04496-full-scan.json`
  (git-excluded scratch, not part of this change).
- Parsed and grouped every hit with `scripts/history_gate_hit_report.py report`,
  which discriminates the scanner's own two hit-marker grammars structurally
  (never by guessing): a **shape** grammar (`"{shape_name}: {match}"`, full
  match text shown because shapes are vocabulary-free regex classes) and a
  **literal** grammar (`"{rel}:{line}:{redacted_term}:{how}"`, redacted term
  only — first character + length — because literal hits are term-list
  matches and the term itself must stay secret). 12 hermetic unit tests plus
  3 added for the literal grammar (`tests/test_history_gate_hit_report.py`,
  15 passed) exercise both grammars, the grouping, the cap-elision detector,
  the mandatory-classification gate, the mandatory-enforce-verdict gate, and
  the exit-code contract (a gate-arming failure is exit 2, never conflated
  with exit 1 "leak found"). None of `verify_public_history.py`'s output
  format, `tests/test_identity_scrub_guard.py`'s pattern inventory, or
  `NH_GUARD_MODE`'s default were touched.
- Cross-referenced every identity hit and every home-path hit against `git
  log --all` and `git show`/`git log --format='%B'` on the named commits —
  both of which are already 100% public data in this repo's history, so
  reading them adds no new exposure; this is how the classifications below
  were confirmed rather than guessed.

## 3. Completeness gate (must hold before any classification is trusted)

| field | original failing banner | this capture |
|---|---|---|
| blob hits | 214 | **214** |
| path hits | 0 | **0** |
| message hits | 33 | **33** |
| identity hits | 32 | **32** |
| tag hits | 0 | **0** |
| missing files | 0 | **0** |
| extra files | 137 | **0** |

The hit counts — the numbers that drive this audit — reproduce **exactly**:
214/0/33/32/0. This is the measured condition the original gate failure
describes; the classification below is written against the real thing, not
an approximation of it.

The **extra-files count does not reproduce** (137 → 0). `missing_files`/
`extra_files` compare the export tree against a live working-tree snapshot
at scan time, not a property fixed by git history the way hit counts are —
so this number can legitimately drift between two different scan
environments/checkouts without either being "wrong". Per the task's explicit
scope, this drift is **reported, not chased**: it is not a hit count, it
carries no classification, and fixing it is out of scope for this document.

## 4. Known-identity baseline

`git log --all --format='%an <%ae>%n%cn <%ce>' a8a04496 | sort -u` lists 19
distinct author/committer strings, collapsing to a smaller set of real
people once GitHub-noreply and human-email spellings of the same person are
merged (e.g. `Lukas Buck <...e-mail.de>` / `L4XB <...@users.noreply...>` are
one person; `Eyal Golan <eyalgolan96@gmail.com>` / `eyalgolan
<...@users.noreply...>` are one person — the operator). This is consistent
with the scanner's own `identities: 13` field (a canonicalized/deduplicated
identity count, not a raw string count) and with the roster this task
described going in: 9 external contributors, the operator's 2 spellings, and
others (bot accounts: `GitHub <noreply@github.com>`, `no_human
<no-human@users.noreply.github.com>`). Every identity named in the hits
below is on this baseline; none is an unrecognized third party.

## 5. Findings by class

There are exactly **5 detector classes** across the 279 raw hits
(214 blob + 33 message + 32 identity), listed below with raw/distinct
counts, 3-5 representative samples, and an explicit classification.
Literal-family samples show the scanner's own redacted signature
(`X*(N)` = first character + length) exactly as the scanner already prints
it — never an unredacted term.

### 5.1 `corporate-home-path` (shape family) — **FALSE-POSITIVE**

- raw hits: 1 · distinct paths: 1 · surfaces: blob

```
a8a04496d438 web/e2e/replay-body-leak.mjs :: corporate-home-path: /Users/e2e-sentinel
```

**Evidence.** The single hit is the string `/Users/e2e-sentinel`, shown in
full because shape hits are vocabulary-free (no private term to protect).
The file it lives in is `web/e2e/replay-body-leak.mjs` — an end-to-end test
of the product's own body-leak/redaction feature — and `e2e-sentinel` is an
unambiguous, self-describing synthetic placeholder username, not any real
person's account. Confirmed directly from the shape match text itself (no
inference needed): this is the detector correctly matching a
`/Users/<name>` pattern in a file whose entire purpose is to *contain*
`/Users/<name>`-shaped strings for testing.

### 5.2 `personal-email` (shape family) — **REAL-TRACE** (mixed; see below)

- raw hits: 68 (54 blob + 14 identity) · distinct matched addresses: 3 ·
  distinct paths: 3 · surfaces: blob, identity

```
a8a04496d438 src/no_human/email/base.py :: personal-email: dana.lee@x.io
a8a04496d438 testdata/email_validation_cases.json :: personal-email: dana.lee@example.io
a8a04496d438 web/src/onboardingEmail.test.mjs :: personal-email: dana.lee@example.io
b6d5c0608ea7 AUTHOR Avri Schneider <avri.schneider@gmail.com> :: personal-email: avri.schneider@gmail.com
b6d5c0608ea7 COMMITTER Avri Schneider <avri.schneider@gmail.com> :: personal-email: avri.schneider@gmail.com
```

**Evidence — the 54 blob hits are false positives.** All 54 are
`dana.lee@x.io` / `dana.lee@example.io`, confined to exactly three files:
`src/no_human/email/base.py`, `testdata/email_validation_cases.json`,
`web/src/onboardingEmail.test.mjs`. Reading `base.py` directly confirms it:
its own module docstring explains `dana.lee@x.io` as the worked example for
`greeting_name`'s "best-effort from the address's local part" docstring
(`"dana.lee@x.io" → "Dana"`) — a deliberately-chosen, self-documented
synthetic example address, not a real person, repeated verbatim into the
test-fixture JSON and the onboarding test file that exercise the same code
path.

**Evidence — the 14 identity hits are a real, known trace.** All 14 are
`Avri Schneider <avri.schneider@gmail.com>` in `AUTHOR`/`COMMITTER` trailers
across 7 real commits. `git log --all` confirms Avri Schneider is a genuine,
recurring contributor identity across this repo's real history — one of the
13 known identities from §4, not an unrecognized third party. This is a
correct, real match (a personal gmail address genuinely appears in public
commit metadata) — but commit author/committer trailers are inherently
public in every git repository, and this specific identity is already known
and expected, not a secret the detector newly exposed.

**Why the class verdict is REAL-TRACE, not FALSE-POSITIVE.** A class
verdict must not average away a genuine hit: 14 of 68 raw hits are real
matches of real (if already-known) personal data, so the honest verdict for
the class as a whole is REAL-TRACE, with the rationale above making clear
which 80% of the volume is fixture noise and which 20% is a legitimate,
already-disclosed identity.

### 5.3 `literal:plaintext` (98 raw) and `literal:whitespace-stripped-adjacency` (44 raw) — **REAL-TRACE** (mixed; see below)

These two classes are reported together because they are the **same
underlying terms found by two scan passes** — the scanner runs its literal
match both against verbatim text and against a whitespace-collapsed variant
(to catch a leak wrapped across a line break), so most plaintext hits have a
paired whitespace-stripped-adjacency hit at the same location, one term
lower in raw count only where the collapsed pass finds nothing new to add.

- raw hits: 98 + 44 = 142 · surfaces: blob, message, identity

```
a8a04496d438 src/no_human/email/__init__.py :: u32/f.py:3:n*(14):plaintext
83b0b3640dc8 .nh-local :: u13/f:1:s*(3):plaintext
0b97a344d74e MESSAGE :: m64:92:e*(11):plaintext
a4e3c6a8ce5f MESSAGE :: m1051:6:w*(8):plaintext
950d3c8834bf AUTHOR Eyal Golan <eyalgolan96@gmail.com> :: i3:1:e*(11):plaintext
```

**Evidence — the `n*(14)` blob hits are false positives.** This 14-character
redacted term (`n*(14)`) appears only in `src/no_human/email/__init__.py`,
`src/no_human/email/base.py`, and `web/src/Onboarding.jsx` — the app's own
email subsystem and its onboarding UI. Reading these files directly (fully
public source, no secrecy involved) shows a normal, self-contained module
implementing/testing the app's own transactional-email feature; the
recurring term is the codebase's own domain vocabulary appearing in its own
source, not third-party or employer content leaking in.

**Evidence — the `.nh-local` hits are a real, confirmed leak.** `git show
83b0b3640dc8:.nh-local` returns exactly one line of real content:

```
/Users/eyalgolan/git/snc/master/no_human/.nh-local
```

This is a **genuine absolute path from the operator's own machine**,
committed verbatim into public history — the `s*(3)` redacted term
(`"snc"`, the private repo's own path segment) and the `e*(9)` redacted
term reported under §5.4 below (`"eyalgolan"`) are both real substrings of
this one real line, not a false positive of any kind.

**Evidence — the `e*(11)` message/identity hits are the operator's own,
already-known identity.** `e*(11)` (11 characters, matching
`eyalgolan96`) accounts for 30 of the 33 message hits and 18 of the 32
identity hits (9 commits × `AUTHOR` only). These are real matches — the
operator's own email genuinely appears, in plaintext, in their own commit
messages and `AUTHOR` trailers — but this identity is explicitly on the
known/expected roster (§4: "operator's 2 spellings"), so it is a correct
detection of expected, already-disclosed data, not a newly-discovered
secret.

**Evidence — the single `w*(8)` message hit is a false positive.** The one
commit carrying this 8-character `w`-term (`a4e3c6a8ce5f`) has the public
subject "Ignore the tooling worktree directory alongside the other
IDE-generated ones" — an ordinary engineering commit about git worktree
hygiene. `"worktree"` is exactly 8 characters and a common git/engineering
term; this reads as an over-broad vocabulary-list entry firing on ordinary
terminology, not a leak.

**Why the class verdict is REAL-TRACE, not FALSE-POSITIVE.** The `.nh-local`
hit is an unambiguous, directly-confirmed real leak of a real absolute path
from a real machine. One real hit is enough to make the class verdict
REAL-TRACE regardless of how much of the remaining volume is fixture noise
or already-known identity — the class must not be marked "safe to ignore"
when it contains a confirmed genuine leak.

### 5.4 `literal:absolute-home-path` (68 raw) — **REAL-TRACE** (mixed; see below)

- raw hits: 68 · distinct redacted signatures: 5 · surfaces: blob, message

```
a8a04496d438 tests/test_navigation_value.py :: u79/f.py:1491:/home/o*(8)/...:plaintext (absolute home path)
a8a04496d438 web/e2e/replay-body-leak.mjs :: u54/f.mjs:149:/Users/e*(17)/...:plaintext (absolute home path)
a8a04496d438 web/src/replayScrub.test.mjs :: u119/f.test.mjs:112:/Users/e*(4)/...:plaintext (absolute home path)
faab31dffa9c tests/test_precommit_manifest_gate.py :: u110/f.py:415:/Users/o*(8)/...:plaintext (absolute home path)
83b0b3640dc8 .nh-local :: u13/f:1:/Users/e*(9)/...:plaintext (absolute home path)
```

**Evidence — 4 of 5 signatures are false positives.** `/home/o*(8)/...` and
`/Users/o*(8)/...` (both 8-char `o`-terms, almost certainly the same
placeholder — consistent with `"operator"`) live in
`tests/test_navigation_value.py` and `tests/test_precommit_manifest_gate.py`;
`/Users/e*(17)/...` and `/Users/e*(4)/...` live in
`web/e2e/replay-body-leak.mjs` and `web/src/replayScrub.test.mjs` — the same
e2e leak-scrubber test files already confirmed synthetic in §5.1 (the
unredacted shape hit in the same file at the same commit is literally
`/Users/e2e-sentinel`). These four are test fixtures for the very feature
this gate is testing, not real machine paths.

**Evidence — the 5th signature, `/Users/e*(9)/...` in `.nh-local`, is a real,
confirmed leak.** This is the exact same commit and the exact same real
line already read in §5.3: `git show 83b0b3640dc8:.nh-local` →
`/Users/eyalgolan/git/snc/master/no_human/.nh-local` — a genuine absolute
home-directory path from the operator's own machine. The 9-character
`e`-term matches `"eyalgolan"` exactly. This same signature also appears
once more under the `message` surface (commit `5760cef4f8ec`); reading that
commit's message directly (public data) confirms it too contains a real,
plaintext absolute path: `nh repo setup-cmds /Users/eyalgolan/git/no_human-public ...`.

**Why the class verdict is REAL-TRACE.** Two independent, directly-confirmed
real hits (one blob, one message) of a genuine absolute machine path,
alongside four confirmed-synthetic test-fixture signatures. Same rule as
§5.3: one confirmed real leak makes the class REAL-TRACE.

## 6. Summary table

| class | family | raw | real / false-positive split | verdict |
|---|---|---|---|---|
| `corporate-home-path` | shape | 1 | 0 real / 1 synthetic | FALSE-POSITIVE |
| `personal-email` | shape | 68 | 14 real (known identity) / 54 synthetic | REAL-TRACE |
| `literal:plaintext` | literal | 98 | ≥1 confirmed real (`.nh-local`) + known-identity matches / rest fixture or over-broad vocab | REAL-TRACE |
| `literal:whitespace-stripped-adjacency` | literal | 44 | mirrors `literal:plaintext` (paired duplicate pass) | REAL-TRACE |
| `literal:absolute-home-path` | literal | 68 | 2 confirmed real (`.nh-local` + one commit message) / 66 synthetic test fixtures | REAL-TRACE |

**Zero detector tuning is proposed as a result of this audit.** The one
class that is cleanly, unambiguously a false positive end-to-end
(`corporate-home-path`) is a single hit — narrowing anything for one hit
in a report-only gate is not worth the risk of also narrowing away a real
future hit of the same shape, and the task is explicit that tuning is only
ever justified by verified false-positive evidence, never by a desire to
reduce the count. No such justification reaches the bar here for any class
that also contains a confirmed real trace.

## 7. Recommendation

**RECOMMENDATION: ENFORCE = NO.** Three of five classes contain at least one
directly-confirmed real trace — the operator's own already-known identity
appearing in commit trailers and messages (expected, public-by-design git
metadata, not a secret), and, more importantly, a genuine absolute machine
path (`/Users/eyalgolan/git/snc/master/no_human/.nh-local`) leaked verbatim
into two places in public history. Flipping `NH_GUARD_MODE` from `report` to
`enforce` today would immediately and permanently block every push on a
repository whose history is already public and cannot be un-published by a
gate — it would not remediate the one real leak, only make the tool
unusable until an operator makes the two separable decisions this audit
does not have standing to make: whether to rewrite public history to
excise the confirmed `.nh-local` leak, and whether to extend the
identity/pattern allowlist for the already-known, already-public operator
and Avri Schneider identities so the gate stops re-flagging expected data on
every future push. Enforcement should be reconsidered once those two
operator decisions are made and this class-level classification is
re-verified against a fresh scan — not before.

## 8. Scope decisions disclosed

- **Did not edit `scripts/verify_public_history.py`.** All uncapped scanning
  went through `scan_history()` called directly from
  `scripts/history_gate_hit_report.py`; the scanner's own file, its 200-hit
  cap, and its `_SUMMARY_RE`-parsed banner format are byte-for-byte
  untouched.
- **Did not edit `tests/test_identity_scrub_guard.py`** or any shape/prefix
  inventory (`_SHAPES`, `_TRACKER_PREFIX_HEX`, `_ATTESTED_PREFIX_HEX`,
  `_SCOPED_APP_PREFIX_HEX`) — no pattern was narrowed anywhere, consistent
  with §6's "zero tuning proposed."
- **Did not flip `NH_GUARD_MODE`'s default** — report→enforce is an explicit
  operator (T2) decision; §7 only recommends against it for now.
- **Did not rewrite public git history** to excise the confirmed
  `.nh-local` leak — that is an explicit operator decision, out of this
  report's scope by the task's own constraints.
- **Did not chase the 137-extra/0-missing completeness drift** — reported
  as a discrepancy in §3, not investigated further, per the task's explicit
  instruction.
- **Did not edit `nh-guard`, `EXPORT_CLASSIFICATION.txt`, `contributors/*.md`,
  `RELEASE_MANIFEST.txt`, or any CI lane definition — all of these live
  outside this worktree** (in `/Users/eyalgolan/git/snc/master/no_human` or
  `/Users/eyalgolan/git/no_human-public`), and the harness instructions for
  this task are explicit: "This is your working directory — make ALL edits
  here... do not touch any other checkout of this repo." PLAN.md's own file
  list had proposed a `nh-guard` truncation-limit fix (raising the 600-char
  guard-log excerpt) and an `EXPORT_CLASSIFICATION.txt` ship-line pointing at
  this document; both are reasonable and reversible follow-ups an operator
  with access to that checkout can make, but they are not made here. Reading
  those files (for the reverse-engineering in §2 and this report) was done
  and is fine; writing to them was not, and is disclosed here rather than
  silently skipped.

## 9. Acceptance criteria

- **AC1** (all 214 blob hits — plus 33 message + 32 identity — captured in
  detailed format, grouped by class, 3-5 samples per class with original
  content and context) — **MET** — evidence: §5 above; raw capture at
  `.no_human/scratch/history-audit/a8a04496-full-scan.json` (214/0/33/32/0,
  matching the failing banner exactly, per §3); tool output verified via
  `uv run python scripts/history_gate_hit_report.py report --json
  .no_human/scratch/history-audit/a8a04496-full-scan.json --check-totals
  blob=214,message=33,identity=32,path=0,tag=0` → `totals match: {...};
  cap_elided=0 (complete capture)`.
- **AC2** (each class assigned REAL-TRACE/FALSE-POSITIVE with 2-3 sentences
  of evidence referencing known identities or repo design context) —
  **MET** — evidence: §5.1-5.4, each with a dedicated "Evidence" paragraph
  citing either `git show`/`git log` confirmation or the known-identity
  baseline in §4.
- **AC3** (detector tuning only recommended after verified false-positive
  confirmation, never to zero out a count) — **MET** — evidence: §6, "Zero
  detector tuning is proposed as a result of this audit," with the
  reasoning for why not tuning even the one clean false-positive class.
- **AC4** (one-paragraph explicit YES/NO enforce recommendation tied to
  classification findings) — **MET** — evidence: §7, "RECOMMENDATION:
  ENFORCE = NO," reasoning tied directly to the confirmed real traces in
  §5.3/§5.4.

## 10. Test evidence

```
$ uv run pytest -q tests/test_history_gate_hit_report.py
...............                                                          [100%]
15 passed in 0.39s
```

`.no_human/repro_tests.json` (git-excluded) names
`tests/test_history_gate_hit_report.py::test_gate_error_is_exit_2_not_1` —
verified to fail (collection error) with `scripts/history_gate_hit_report.py`
absent and pass (15/15) with it present, satisfying the bugfix-shaped
repro-test requirement for this change scope.
