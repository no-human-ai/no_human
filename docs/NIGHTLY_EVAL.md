# Nightly funnel eval

A regression gate for the whole funnel — ticket in, reviewed PR out — run
unattended overnight against a fixed five-tier corpus of invented toy repos.

It answers one question: **did anything that worked last night stop working?**
It is not a benchmark and must never be tuned to. The tiers are deliberately
small and boring; the interesting number is the one that moved.

## What runs

Five tiers, each a ticket, a tiny self-contained git repo and — from t2 up —
one **held-out test that is red before the change and must be green after**.
The corpus lives in `eval/funnel_corpus/`.

| tier | shape | ceiling (weighted tokens) | ceiling (wall) |
| --- | --- | --- | --- |
| `t1_docs_oneliner` | a one-line documentation fix | 400,000 | 15 min |
| `t2_small_fix` | raise instead of returning None | 1,500,000 | 30 min |
| `t3_small_feature` | one new method in one file | 3,000,000 | 45 min |
| `t4_cross_file` | one change threaded across two files | 4,000,000 | 60 min |
| `t5_test_first` | write the failing test first | 2,000,000 | 40 min |

A tier passes when **all five** hold: a PR was opened, the reviewer passed it,
the held-out test went green, and the run stayed inside both ceilings.

**Quality is measured, never judged.** `holdout_green` comes from running the
tier's held-out test against what the agent left behind — there is no score
anywhere in this pipeline and there must never be one. A reviewer scripted to
pass everything still cannot make a tier green.

## Cost

This section is the pre-run authorised ceiling — see "The cost reference, and
how to refresh it" below for the separate post-run verdict that compares
tonight's actual spend against recent nights.

The corpus's ceiling sum is **10,900,000 weighted tokens** — the worst case if
every tier spends right up to its limit, priced with the same
fresh/cache-read/cache-write weights every budget cap in the product uses. A
healthy night costs a fraction of that.

That number is *authorised* rather than merely documented because each tier's
`max_weighted_tokens` is filed into its task as the task's own
`lifetime_tokens`, stamped `budget_unit: weighted`, so the product's existing
budget gate parks the tier at its published ceiling. Without that the only cap
in play is `bounds.lifetime_tokens` — 4,000,000 for every task alike — and five
tiers would authorise a 20,000,000 night.

The same number is the default for `eval.nightly_budget_tokens`. That is a
static pre-check: it compares the corpus sum against the configured budget
before anything runs, so **adding a tier without raising the budget refuses the
whole night**. Out of the box the two are equal, so the pre-check permits
exactly this corpus and nothing more — it is a guard against the corpus
growing, not against a tier overspending. The per-task cap above is what stops
a tier.

## Running it

```bash
./scripts/nightly_eval.sh                 # the whole thing, guarded
uv run python -m no_human.eval.funnel_eval --home ~/.no_human-nightly
```

The run gets its **own** HOME (`~/.no_human-nightly` by default), its own
database, its own worktree root and its own port. It never touches
`~/.no_human`, and both the shell script and `funnel_eval._assert_isolated`
refuse if it would.

The exit code is the verdict: `0` only when every tier passed **and** the
ratchet held. `124` is the shell timeout guard firing. Everything else is red.

## Reading a red night

Start with `SUMMARY.md` in the output directory, then the dated
`nightly-YYYY-MM-DD.json` beside it. Each tier carries a `stage` saying where
it stopped, and `failures` naming each broken criterion **with the actual
numbers**. The stages worth knowing:

| stage | what happened |
| --- | --- |
| `awaiting_approval` | reached the human gate — the healthy terminal state |
| `wall_clock_kill` | the tier blew its own wall-clock ceiling and was killed |
| `run_deadline_exceeded` | the night ran out of time before this tier started |
| `crashed` | the runner itself failed on this tier; `detail` has the exception |
| `escalated` / `blocked` | the agent stopped honestly — read `detail`, this may be correct |

`escalated` deserves one warning of its own, because it is the stage most
easily misread as "fine". A run with **no review gate wired** escalates or
crashes *after* the coder attempt has been paid for, and reads exactly like an
honest stop. The shipped path builds a real reviewer, so if you see this on
every tier at once, suspect the gate before you suspect the agent. The cure is
never `reviewer.allow_advisory` — that makes a skipped gate report a pass, and
`review_passed` stops meaning anything.

Three failures that look alike and are not:

- **`holdout_green: held-out tests measured holdout_red`** — the change landed
  and does not do what the ticket asked. This is the interesting one.
- **`max_weighted_tokens: …`** — the change may be right and cost too much. Read
  the cost line before touching the code.
- **`pr_opened: no PR was opened`** — nothing reached the gate at all; the
  `stage` says why, and it is usually the environment, not the agent.

A red night is a question, not a verdict on the product. Reproduce before
changing anything — cheapest first:

```bash
# 1. No quota at all: rebuild the tier's fixture and run its held-out test
#    against it. Answers "is the holdout still red at base, and does the
#    change I am looking at turn it green?"
uv run python -c "
from no_human.eval.funnel_corpus import load_corpus
t = {c.name: c for c in load_corpus()}['t2_small_fix']
print(t.materialize('/tmp/nh-one'), t.holdout_cmd)"

# 2. The whole thing again, into a throwaway HOME. This runs ALL FIVE tiers:
#    there is no single-tier selector, deliberately — see below.
uv run python -m no_human.eval.funnel_eval --home /tmp/nh-one --out /tmp/nh-one/out
```

There is no `--tier` flag. A selector would have to bypass the five-name corpus
check on purpose, which is a second and weaker way into the guard that exists
because "0 passed, 0 failed, exit 0" once looked like a clean night. One tier's
worth of reproduction is available without it — option 1 above needs no quota,
and driving a single tier from Python is three lines (`run_funnel_eval(home,
out, corpus=[one_tier])`), which is what the test suite does.

## The baseline, and how to refresh it

`eval/funnel_corpus/baseline.json` is the ratchet. One movement fails a night:
a tier that **passed** at the baseline fails tonight — including a baseline
tier that is missing from tonight's run entirely, the same "this used to be
covered" failure. Cost is **not** judged here any more; see "The cost
reference, and how to refresh it" below for why and where it moved.

A run also refuses outright if `eval/funnel_corpus/` is not exactly the five
tiers above — by name, not by count. An emptied or partially checked-out corpus
would otherwise report "0 passed, 0 failed" and exit 0: the instrument broken
and the night green.

A cheaper or better night is **recorded and changes nothing**. The runner never
writes the baseline. Auto-tightening would turn one lucky run into a gate
nobody can pass, which is exactly how an instrument becomes a target.

**It ships UNSEEDED.** Until a human seeds it, every night reports its results
and ratchets nothing, and the summary says so. To seed or refresh it: take an
`out/nightly-YYYY-MM-DD.json` **you have read**, copy its `tasks` array into
`baseline.json`, set `unseeded` to `false`, and record the date and the product
commit — in the commit that fixes or accepts something, never to turn a red
night green.

## The cost reference, and how it is written

`eval/funnel_corpus/cost_median.json` holds the last `COST_HISTORY_NIGHTS`
(30) recorded nights, and the nightly run holds tonight's weighted cost
against the **median** of those nights — two independent movements, neither
gated on the other:

1. the **night total** (every tier's weighted cost, summed) over **1.15x**
   the median of the last 30 recorded night totals, and
2. any **one tier** over **2x** its own median over the same 30 nights,
   regardless of what the night total does.

Either can fire alone: a single noisy tier does not need to also move the
five-tier sum to be caught, and a slow creep spread evenly across every tier
does not get to hide behind each tier individually looking fine.

This replaced a flat **25%-over-baseline** band judged per tier against a
single recorded number, because a single number is too noisy at n=1 per tier:
issue #425 saw one tier vary 85,000-174,000 weighted tokens across three
same-day, all-green runs while the *night total* stayed within about 5% of
its mean the whole time. The old band fired on that noise, not on a
regression — the median-of-30 reference and the wider (2x) per-tier band
exist specifically so ordinary day-to-day variance does not read as one.

**Fewer than 30 nights recorded is a warm-up, not a failure.** The reference
has nothing to compare against yet, so the run passes on cost and says so in
the summary — this is why `cost_median.json` ships with a single recorded
night instead of 30 invented ones.

**The runner writes this file itself.** Unlike the baseline, a human does not
maintain `cost_median.json` by hand: after a night that was **not refused**
and produced a real, measured `cost` for every tier (never an invented one),
`funnel_eval._run` appends `{"date", "total", "tasks"}` for that night —
never editing or removing an existing entry — and trims the file to the most
recent `COST_HISTORY_NIGHTS`. A refused night (the corpus check or the
budget guard fired before anything ran) writes nothing, because there is no
real measurement to record.

**An unreadable reference is a FAILURE, not a fresh start.** A file that has
genuinely never been written is the warm-up path above. A file that
**exists** but does not parse as JSON, or parses to something without a
usable `nights` list — a truncated write, a bad merge, a hand-edit gone
wrong — is a different event: the history is there and could not be read, so
the night is **red**, with a line naming the file and the parse error, and
the runner leaves the file untouched rather than silently overwriting the
evidence of the corruption with tonight's night alone.

## Scheduling it (after sign-off)

`scripts/com.nohuman.nightly-eval.plist` runs it at 03:00 local. **It ships
disabled** — `Disabled` is true and `RunAtLoad` is false — because a job that
spends subscription quota every night is an operator's decision, not a
checkout's. Enable it only after a real run has been read and signed off:

```bash
sed "s|__REPO__|$PWD|g" scripts/com.nohuman.nightly-eval.plist \
  > ~/Library/LaunchAgents/com.nohuman.nightly-eval.plist \
  && launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.nohuman.nightly-eval.plist \
  && launchctl enable gui/$UID/com.nohuman.nightly-eval
```

Stop it with `launchctl bootout gui/$UID/com.nohuman.nightly-eval`.
