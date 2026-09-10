# Navigation value — measured before building (issue #114 phase 3, 2026-09-10)

Phase 3 of [issue #114](https://github.com/no-human-ai/no_human/issues/114) is a
gate, not a feature: *"measure navigation value before building"*. Phases 1 and
2 each shipped a signal — net-new type diagnostics at the review gate, then the
same signal one turn earlier at edit time. Phase 3 ships an **instrument and a
verdict**, and the thing it gates — an in-process SDK MCP server exposing
read-only `definition`/`references`/`hover` — is deliberately **not** built
here.

The issue's own reason for the gate: three years of LSP-for-agents development
has produced no published controlled experiment showing that navigation tools
improve resolve rates, independent benchmarks show frontier models underusing
them, and the Claude Agent SDK exposes no LSP tool while cloud sessions start no
language server. A symbol server is infrastructure this product would own and
carry, polyglot, indefinitely. The case for paying for it has to come from this
project's own recorded behaviour.

The instrument is [`scripts/navigation_value.py`](../scripts/navigation_value.py).
It writes nothing, imports nothing from the product, and prints no path, search
pattern, session id or file content — only aggregates.

## The question

Of the characters the coder spends on file reads, how many sit in reads that a
`definition`/`references`/`hover` call could have answered instead — **net of
what the symbol call would itself have cost to answer**?

## The decision rule, pre-registered

Stated before the script was first run against any corpus, so it could not be
tuned to the answer. A reader is invited to disagree with it in the open.

**PROCEED to the A/B** only if all three hold:

1. the corpus clears its floor — ≥ 200 sized reads across ≥ 20 sessions;
2. net addressable read mass is ≥ **15%** in **both** units (see below);
3. the two units agree in direction. If they straddle the gate the answer is
   INCONCLUSIVE, because which unit is right would decide the phase.

Otherwise **HALT**. Phase 3's terms are explicit that negative results halt
development, and phase 4 (polyglot wiring evidence) is contingent on phase 3.

Why 15%: the ceiling on any fix is the mass it can address, and the lever this
one competes with is already measured — lowering the coder's SDK compaction
window cut modelled burn **30.9%** ([COST_LEVERS.md](COST_LEVERS.md)) by
configuring something the SDK already owns. A net addressable share below 15%
cannot pay for owned infrastructure when a configuration change moved twice
that.

## The coder searches through the shell, and reading only `Grep` broke the gate

The first version of this instrument recognised searches only from the `Grep`
and `Search` **tools**. On the fleet database, coder events only:

```
Bash 115,776 | Read 35,557 | Edit 18,425 | Write 3,010
Grep 0 | Glob 0 | Search 0
Bash calls containing grep/rg/ag/ack: 50,459
```

no_human's coder never emits `Grep`. It cannot: `core/prompt_blocks.py`
instructs it to *"locate the relevant lines with `grep -n`"* for a large file,
so the shell is the sanctioned search channel and always was. So
`symbol_lookup` — the class this measurement leans on hardest, the one whose
substitute tool is exact — **could only ever be zero on the product's own
telemetry**, and the verdict fell back entirely to the size heuristic while
still rendering as though it had two signals. The corpus cleared both floors, so
the script did not hold back: it decided, and it decided HALT, on evidence where
its own strongest signal could not exist.

Two things now stop that:

- **The shell is read as a search channel.** A `grep`/`rg`/`ag`/`ack`/`ugrep` (or
  `git grep`) in any segment of a shell command is a search, its pattern is
  extracted, and it feeds the same `is_symbol_query` test. Segments are split on
  shell control operators the way `orchestrator._looks_like_test_run` already
  splits them, and for the same measured reason.
- **A corpus with reads and no searches is refused, not decided.** `symbol_lookup`
  reaching zero has four different causes, and the class alone renders them
  identically. Zero search calls is a gap in the source or the instrument, never
  evidence about the agent, so it gets no verdict at all — the same refusal, for
  the same reason, as zero reads.

The census is now printed unconditionally and separates all four cases:

```
search channel: 1477 search call(s), 1026 with an extractable pattern, 450 symbol-shaped
```

"0 searches seen" and "0 of 1,026 patterns were symbol-shaped" are opposite
facts, and the report no longer renders them the same way. The middle number is
this script's own **coverage**, not a fact about the agent: a search whose
pattern it could not extract — `grep -f patterns.txt`, or a command with an
unmatched quote — counts as a search and never as a symbol one.

## What gets counted, and what does not

| class | rule | why it is separate |
|---|---|---|
| `symbol_lookup` | a read within 3 tool calls of a **search** — the `Grep` tool *or* a grep/rg/ag/ack in a shell command — whose pattern is a symbol query (a bare identifier, optionally behind `def`/`class`/`function`/`interface`/…) | go-to-definition done by hand; the substitute tool is exact |
| `whole_file` | a read with no `offset`/`limit` whose result is ≥ 12,000 chars | the issue's own wording, "large-file reads"; weaker, because a whole-file read is sometimes right |
| `navigable` | the union of the two, **and only for a language a symbol server serves** | the number the verdict uses |
| `non_code` | a read of a file no symbol call can answer at any size — README, JSON fixture, log, lockfile | see the refutation below |
| `repeat` | a read of a file the same context window already read | orthogonal; a re-read is a compaction question, not a symbol-tool one |

Two rules keep the number honest rather than flattering:

- **Net of the fix's own cost.** A `definition` call returns a symbol body, so
  the addressable mass is `max(0, read_chars − 2,000)`, not the read. The
  2,000-char figure is an estimate — no symbol server exists here to measure
  one from — and `--symbol-response-chars` re-derives the table under any other
  assumption.
- **The cost unit is not the read.** COST_LEVERS measured 95.6% of the bill as
  cache **read**: the conversation re-sent every turn. So a read's cost is its
  size times how long it then sits in context. `chars` is a fact; `weighted`
  (chars × tool calls remaining in the same context window) is a **model**, and
  the verdict requires both.

Weighting is per **context window**, not per session: an `attempt_start` and a
compaction each reset what gets re-sent. Subagent reads carry raw chars and
zero weighted mass, because a subagent's result is re-read in the subagent's
context, whose remaining turns neither source records.

## The refutation that flipped the verdict

An earlier run of the instrument returned **PROCEED at 27.6%**. It was wrong.

`.md` was the largest single extension in the corpus by read mass — 32% of
every character read — and no `definition`/`references`/`hover` call answers a
question about a README. Markdown, JSON, logs, text and lockfiles were all
being counted as addressable. With a closed allowlist of languages a symbol
server actually serves (`CODE_EXTENSIONS`), that same corpus dropped to 10.4%
raw / 13.7% weighted **as measured at the time** — before the shell search
channel below was read — and the verdict inverted to HALT.

One constant, and the phase gate inverted. That class is now reported as its
own `non_code` row, and
[`tests/test_navigation_value.py`](../tests/test_navigation_value.py) pins it
with a pair of fixtures identical except for the file extension: the same read
mass reads PROCEED in `.py` and HALT in `.md`.

Reading what `is_symbol_query` actually *accepted* over 1,470 real patterns
found two more false positives in the same load-bearing class, both now
rejected and both in the test table: a quote character anywhere disqualifies (the
fragment `"Test`, the quoted-literal search `'"version"'`), and the `=`/`:` tail
is only cut when a declaration keyword introduced it (`kind="tool_result"` and
`risk:` were being reported as name lookups).

## The measurement (2026-09-10)

`python scripts/navigation_value.py --source transcripts` over 114 Claude Code
sessions / 116 context windows / 835 reads on one contributor's machine:

```
search channel: 1477 search call(s), 1026 with an extractable pattern, 450 symbol-shaped

         class   reads         chars   %chars          weighted     %wtd
------------------------------------------------------------------------
           all     835     2,925,259   100.0%       293,497,457   100.0%
 symbol_lookup      61       208,815     7.1%        24,234,243     8.3%
    whole_file      17       307,137    10.5%        42,964,293    14.6%
     navigable      74       442,572    15.1%        57,447,023    19.6%
          both       4        73,380     2.5%         9,751,513     3.3%
      non_code     445     1,498,884    51.2%       108,452,315    37.0%
        repeat     249       608,894    20.8%        68,164,824    23.2%
      windowed     317       690,540    23.6%        71,276,530    24.3%
     sidechain       0             0     0.0%                 0     0.0%

net addressable: 327,085 chars (11.2% of read mass), 42,706,973 weighted (14.6%)
```

**VERDICT: HALT** — 11.2% raw and 14.6% weighted, both below the pre-registered
15% gate. The weighted figure misses by 0.4 points.

Three facts in that table matter more than the headline:

1. **Half the read mass is not code at all** (`non_code`, 51.2%). Nothing a
   symbol server does touches it.
2. **Reading the shell channel quadrupled the strongest class** — `symbol_lookup`
   went from 15 reads / 2.4% of read mass to 61 reads / 7.1%. That is the
   difference between a verdict resting on one signal and on two.
3. **Re-reads are still three times the addressable class** (`repeat`, 20.8%
   against 7.1%). If a lever is visible in this table it is context retention,
   and that is the compaction lever COST_LEVERS already measured, not a new tool.

## The verdict is not robust to any of its three estimates

Every one of the instrument's estimated parameters moves the decision. The
script re-decides at half and double the large-read threshold automatically and
prints a `CAUTION` when the answer changes; the other two rows were taken by
hand.

| large-read threshold | navigable reads | raw | weighted | decision |
|---|---:|---:|---:|---|
| 6,000 chars | 115 | 19.5% | 25.0% | **PROCEED** |
| **12,000 chars** (pre-registered) | 74 | 11.2% | 14.6% | **HALT** |
| 24,000 chars | 63 | 6.0% | 5.8% | **HALT** |

| `--symbol-response-chars` | raw | weighted | decision |
|---|---:|---:|---|
| 0 | 15.1% | 19.6% | **PROCEED** |
| 1,000 | 12.9% | 16.7% | INCONCLUSIVE |
| **2,000** (default) | 11.2% | 14.6% | **HALT** |
| 4,000 | 9.0% | 11.7% | **HALT** |

| `--lookback` | symbol_lookup reads | raw | weighted | decision |
|---|---:|---:|---:|---|
| 1 call | 28 | 9.8% | 13.2% | **HALT** |
| **3 calls** (default) | 61 | 11.2% | 14.6% | **HALT** |
| 6 calls | 111 | 13.9% | 16.8% | INCONCLUSIVE |

Every probe also carries the count of reads that **qualified** at its
threshold, and that number is what makes a probe evidence or not: raise the
threshold far enough and almost nothing clears it, so the probe can only return
HALT and its agreement with the shipped decision means nothing. On the fleet
database the doubled probe qualified 21 reads out of 35,557.

Pre-registering a threshold and its derivation stops the number being tuned to
the answer. It does not make the answer a property of the data — and on this
corpus, it is not.

## What this does not know

- **This is the wrong population, and it is the only one available here.** The
  authoritative source is `task_events` — no_human's own unattended coder. It is
  **empty** in this checkout, so the figures above come from interactive Claude
  Code sessions instead. That is the same substitution
  `agent/tool_result_cap.py` made, with the same caveat: the directional shape
  is likelier to carry than the percentages. An interactive session reads
  differently from an unattended coder — more markdown, more one-off
  inspection, a human steering it away from dead ends.
- **A bare English word is indistinguishable from a symbol name.** This is the
  largest known over-count in `symbol_lookup`. Read over the 1,470 real
  patterns, it accepts `warning`, `snapshots` and `node_modules` alongside
  `parse_config` and `_run_at_commit`, because nothing in a pattern says whether
  the identifier-shaped thing searched for is a name in the code or a word in a
  string. No rule over the pattern alone can separate them.
- **The shell parse is best-effort.** The option tables are hand-written, so a
  flag's value read as a pattern, or a pattern missed behind an unlisted flag,
  is possible. A segment that cannot be tokenized cleanly contributes a search
  with **no** pattern rather than a guessed one, which is why
  `searches_with_pattern` is reported separately — it is the instrument's own
  coverage, not a fact about the agent. PowerShell's `Select-String` is not
  parsed at all; a PowerShell-only corpus reports zero searches and is refused.
- **Purpose is not recorded.** Neither source stores *why* a read happened, so
  every class is a proxy. `symbol_lookup` is **adjacency**, not causation:
  whether the read's path was among the search's hits cannot be checked, because
  the product records a search result's size and never its text, by design.
- **The language allowlist is closed.** A repo in a language outside it reads as
  entirely non-addressable, which understates rather than overstates the case
  for building. Widening it is exactly the polyglot question phase 4 defers.
- **`n=1` machine, and a live corpus.** 114 sessions, one contributor, one set
  of repos, mostly TypeScript and Go. The transcript corpus also **grows as the
  machine is used**, so the census drifts between runs — the figures above are a
  dated snapshot, not a fixed dataset. The class shares moved by less than 0.1
  point across re-runs during this work, but re-derive them rather than quoting
  them.

## Decision

**Phase 3 halts here, and does not close.** No symbol server is built, no A/B is
launched, and phase 4 stays contingent.

The HALT is the answer the pre-registered rule gives on the only corpus
available — but it is **not a finding this phase should be closed on**, and the
reason is stronger than it was before the search channel was fixed: the decision
flips on every one of the three estimated parameters, the weighted share misses
the gate by 0.4 points, and the population is a substitute for the one the phase
is about.

**What would settle it**, in order:

1. Run the instrument on a `task_events` database with real coder volume:
   `python scripts/navigation_value.py --source events --db ~/.no_human/no_human.db`.
   That is the population the phase is about. It needs no credential and no
   token spend: the coder's reads, its result sizes, and — now that the shell
   channel is read — its searches are all already in `task_events`, the search
   patterns inside the recorded Bash commands. What is *not* recorded there is
   any search's **hits**, which is why `symbol_lookup` remains adjacency rather
   than causation on either source.
2. If that run clears the gate in both units and holds across the parameter
   ranges above, the issue's A/B is authorised: an in-process SDK MCP server
   with read-only symbol tools, scored on success rate, cost and wall-clock
   against a control. Nothing beyond it — the benchmark is an instrument, not a
   target.
3. If it does not clear the gate, the `repeat` row is where the next lever
   plausibly is, and it belongs to the compaction work COST_LEVERS already
   started rather than to this issue.
