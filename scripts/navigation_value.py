#!/usr/bin/env python3
"""Do exploration reads target what a symbol tool could answer? (#114 phase 3.)

Phase 3 of issue #114 is a GATE, not a feature: "measure navigation value
before building". The phases before it shipped a signal each (net-new type
diagnostics at the review gate, then the same signal one turn earlier at edit
time). This phase ships an INSTRUMENT and a verdict, and the thing it gates —
an in-process SDK MCP server exposing read-only definition/references/hover —
is deliberately NOT in this change. The issue states the reason: three years
of LSP-for-agents work has produced no published controlled experiment showing
navigation tools improve resolve rates, and independent benchmarks show
frontier models underusing them. A symbol server is real infrastructure; the
case for paying for it has to be made from this repo's own recorded behaviour
first.

THE QUESTION, stated so that NO is a possible answer: of the characters the
coder spends on file reads, how many sit in reads that a
definition/references/hover call could have answered instead — net of what the
symbol call would itself have cost to answer?

WHAT IT READS (read-only; this script writes nothing and imports nothing from
the product, so it runs against a database or a log written by any version):

  * ``--source events`` — ``task_events`` in a no_human SQLite DB. The
    product's OWN coder telemetry: `orchestrator._agent_sink` persists every
    coder ``tool_use`` with its `tool_name`/`tool_input`, and every
    ``tool_result`` with `result_chars` and a `tool_use_id` join key. This is
    the authoritative population — it is no_human's coder, not a human's
    session — and on a fresh checkout it is empty. Its searches are Bash
    commands, not `Grep` calls, and the command text is in `tool_input`
    verbatim; reading only the `Grep` tool made this source's strongest class
    structurally empty. See ``SHELL_TOOLS``.
  * ``--source transcripts`` — Claude Code's own session logs under
    ``~/.claude/projects/*/*.jsonl`` (and ``~/.claude-personal``). The same
    harness, one JSON object per line, carrying the same `tool_use` /
    `tool_result` blocks. This is the corpus that has data TODAY, and it is
    the same substitution `agent/tool_result_cap.py` made for the same reason —
    with the same caveat, restated here rather than inherited: a population
    measured on interactive sessions is not the unattended coder. Directional
    shares are likely to carry; absolute figures are not.
  * ``--source auto`` (default) — events when the DB holds coder reads, else
    transcripts. The chosen source is named in the output, always, because a
    verdict whose population is unstated is not a measurement.

WHAT COUNTS AS NAVIGATION-ANSWERABLE, and why each class is reported on its
own line rather than folded into one number:

  ``symbol_lookup``   A read within ``--lookback`` tool calls of a SEARCH
                      whose pattern reads as a symbol query rather than a text
                      search (an identifier, optionally behind a
                      `def`/`class`/`function`/`interface`/... keyword). This
                      is go-to-definition done by hand, and it is the
                      strongest class here because the substitute tool is
                      exact: the agent asked "where is S", by the only means
                      it has. "A search" means the `Grep` tool OR a
                      grep/rg/ag/ack inside a shell command, and the second
                      half of that is not an extra — it is the only channel
                      no_human's coder actually uses. See ``SHELL_TOOLS``.
  ``whole_file``      A read with no `offset`/`limit` whose result exceeds
                      ``--large-read-chars``. The issue's own wording —
                      "exploration tokens target large-file reads". Weaker
                      than the class above, because a whole-file read of a
                      large module is sometimes exactly the right call and no
                      symbol response replaces it.
  ``navigable``       The UNION of the two, deduped, and only for a file in a
                      language a symbol server actually serves (see
                      ``CODE_EXTENSIONS``). ``both`` reports the overlap so
                      nobody has to assume the union was not a sum.
  ``non_code``        A read of a file no definition/references/hover call can
                      answer at any size: a README, a JSON fixture, a log, a
                      lockfile. This row is not a caveat, it is a REFUTATION
                      the first run of this script actually delivered — on the
                      transcript corpus, `.md` was the single largest
                      extension by read mass, and every character of it had
                      been counted as addressable until this class existed.
  ``repeat``          A read of a file the same context window already read.
                      ORTHOGONAL to the classes above and overlapping them: it
                      neither adds to nor removes from ``navigable``. A
                      re-read is mostly evidence the first read did not stick,
                      which is a compaction question, and it is reported here
                      so it is not mistaken for one.

THE COST UNIT IS NOT THE READ. ``docs/COST_LEVERS.md`` measured 95.6% of the
bill as cache READ — the conversation re-sent every turn — so a read's cost is
not its size but its size times how long it then sits in context. Both units
are reported: ``chars`` is a fact, ``weighted`` (chars x tool calls remaining
in the same context window) is a MODEL, labelled as one. The verdict requires
the two to agree in direction; when they disagree it reports INCONCLUSIVE
rather than picking the flattering one.

A CONTEXT WINDOW, not a session, is the weighting unit. An attempt boundary
(``attempt_start``) and a compaction (``compaction``, or a transcript's
``isCompactSummary``/``compact_boundary``) both reset what gets re-sent, so
weighting across either would charge a read for turns that never re-read it.
Subagent reads are excluded from ``weighted`` entirely and counted on their
own row, for the reason `tool_result_cap.py` records: a subagent's result is
re-read in the SUBAGENT's context, not the main conversation whose re-read
cost is the target — and how many turns that subagent had left is not
recorded on either source.

NET OF WHAT THE FIX WOULD COST. A `definition` call is not free: it returns a
symbol body. So the addressable mass is ``max(0, read_chars -
--symbol-response-chars)``, not the read. This is the number the verdict uses,
and it is the one that can refute the whole phase: if reads are large but a
symbol response would be nearly as large, there is nothing to win.

THE VERDICT CHECKS ITSELF. Every decision is re-taken at half and double the
large-read threshold, and when the answer changes the report says so. This is
not hypothetical caution: on the transcript corpus the pre-registered 12,000
chars gives HALT at 11.2% / 14.6% and 6,000 gives PROCEED. Pre-registering a
threshold stops it being tuned to the answer; it does not make the answer a
property of the data, and only the probe can tell the two apart. Each probe
also carries the number of reads that QUALIFIED at its threshold, because a
probe that qualified almost nothing could not have disagreed and its agreement
is not corroboration.

THE SEARCH CENSUS GATES THE VERDICT. ``symbol_lookup`` reaching 0 has several
causes that the class alone renders identically — no search calls in the
source at all, searches this script could not extract a pattern from, patterns
that were all text, or symbol searches that no read followed. A corpus with
reads and NO searches is REFUSED rather than decided (`NoSearchChannel`),
because it can clear every floor and render a confident negative resting
entirely on the size heuristic. That is not hypothetical either: it is what
this script did on the product's own telemetry until the shell channel was
read.

HONEST LIMITS, so nobody reads more out of the table than is in it:

  * PURPOSE IS NOT RECORDED. Neither source stores why a read happened. Every
    class above is a PROXY, and ``symbol_lookup`` errs in both directions — it
    counts a Grep followed by an unrelated read (over), and it cannot see a
    symbol question the agent answered from memory with no Grep at all
    (under). Neither error's size is known, and no aggregate here should be
    read as "the agent wanted a symbol".
  * A BARE ENGLISH WORD IS INDISTINGUISHABLE FROM A SYMBOL NAME, and this is
    the largest known over-count in the class the verdict now leans on. Read
    over 1,470 real patterns, `symbol_lookup` accepts `warning`, `snapshots`
    and `node_modules` alongside `parse_config` and `_run_at_commit`, because
    nothing in a pattern says whether the identifier-shaped thing being
    searched for is a name in the code or a word in a string. No rule over the
    pattern ALONE can separate them; separating them needs the search's hits,
    which the product deliberately does not record (next bullet).
  * THE SHELL PARSE IS BEST-EFFORT. The option tables in ``_VALUE_SHORTS`` and
    ``_VALUE_LONGS`` are hand-written, so a flag whose value is read as a
    pattern, or a pattern missed behind an unlisted flag, is possible. Both
    only move ``symbol_lookup``, and a segment that could not be tokenized
    cleanly contributes a SEARCH with no pattern rather than a guessed one —
    which is why ``searches_with_pattern`` is reported separately from
    ``searches``, as this script's own coverage rather than as a fact about
    the agent.
  * ``symbol_lookup`` is ADJACENCY, not causation. Whether the read's path was
    actually among the Grep's hits cannot be checked on both sources: the
    product records a search result's SIZE and never its text (deliberately —
    see `claude_backend._result_size`), so a classifier that needed the hits
    would work on transcripts and silently do something else on the DB. One
    rule over both sources was worth more than a sharper rule over one.
  * A READ WITH NO RECORDED RESULT contributes no mass and is counted in its
    own column. It is not treated as zero: an unmeasured read and a
    zero-length read are different facts, and the first must not be able to
    quietly shrink a share's denominator.
  * THE LANGUAGE LIST IS A CLOSED ALLOWLIST, not a guess at what LSP exists
    for. A repo in a language outside ``CODE_EXTENSIONS`` reads as entirely
    non-addressable here, which understates rather than overstates the case
    for building — the safe direction for a gate. Widening that list is
    exactly the polyglot question the issue defers to phase 4.
  * NOTHING IDENTIFYING IS PRINTED OR EMITTED. Not a path, not a Grep pattern,
    not a session id, not a byte of file content. Paths are used for an
    extension and for in-memory repeat detection only. `--json` carries the
    same aggregates the table does and nothing else.

WHAT THIS SCRIPT MUST NOT BE USED FOR: shipping the symbol server as a side
effect. Phase 3's own terms — "benchmark is an instrument, not a target;
negative results halt development" — mean a PROCEED here authorises the A/B
described in the issue (success rate, cost and wall-clock against a control),
not the tool. A HALT closes the phase.

USAGE

    python scripts/navigation_value.py                    # auto-pick a source
    python scripts/navigation_value.py --source events --db PATH
    python scripts/navigation_value.py --source transcripts
    python scripts/navigation_value.py --json             # machine-readable

Fails CLOSED and loud on an empty corpus: exit 1 with no verdict. "No data"
and "measured, and it is small" are different answers, and an empty database
must never be able to close this phase — the same rule the issue's
non-negotiables state for the type checkers ("failures produce no evidence
rather than false clean verdicts").
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: `orchestrator.CODER_ROLE` — a literal, not an import, so this script has no
#: import-time dependency on the app package (the same reason
#: `measure_cache_burn.py` restates it).
CODER_SOURCE = "agent"

#: `orchestrator`'s own event kinds. An attempt starts a fresh session and a
#: compaction truncates one, so both end a context window.
ATTEMPT_START_KIND = "attempt_start"
COMPACTION_KIND = "compaction"

DEFAULT_DB = Path(
    os.environ.get("NO_HUMAN_HOME", Path.home() / ".no_human")) / "no_human.db"

#: Claude Code's session logs. Both roots for the reason
#: `history/claude_code.py` gives: a machine can hold an enterprise and a
#: personal config dir, and "all sessions" means both.
TRANSCRIPT_ROOTS = (
    Path.home() / ".claude" / "projects",
    Path.home() / ".claude-personal" / "projects",
)

#: The read tools, and the search tools whose pattern can be a symbol query.
#: `Glob` is deliberately NOT in the second set: `**/auth.py` is a FILE search,
#: and counting it would let path globbing masquerade as go-to-definition.
READ_TOOLS = frozenset({"Read", "View"})
SYMBOL_QUERY_TOOLS = frozenset({"Grep", "Search"})

#: Tools whose input is a SHELL COMMAND, which for no_human's coder is where
#: every search actually happens.
#:
#: 🔴 THE SEARCH-TOOL SET ALONE MAKES `symbol_lookup` STRUCTURALLY EMPTY ON THE
#: PRODUCT'S OWN TELEMETRY, and the first version of this script shipped that
#: way. Measured on the fleet database, coder events only:
#:
#:     Bash 115,776 | Read 35,557 | Edit 18,425 | Write 3,010
#:     Grep 0 | Glob 0 | Search 0
#:     Bash calls containing grep/rg/ag/ack: 50,459
#:
#: The coder never emits `Grep`, so the strongest class could only ever be 0
#: and the verdict fell back entirely to the size heuristic while still
#: rendering as though it had two signals. That is not an accident of one
#: install either — `core/prompt_blocks.py` INSTRUCTS the coder to "locate the
#: relevant lines with `grep -n`" for a large file, so the shell is the
#: sanctioned search channel and always was.
SHELL_TOOLS = frozenset({"Bash", "Terminal", "Shell"})

#: Search binaries recognised inside a shell command, matched on the basename
#: so `/usr/bin/grep` counts. `git grep` is handled as a two-token special
#: case. Scoped to the POSIX grep family because that is what the coder's own
#: prompt tells it to use and what the 50,459 figure above measured;
#: PowerShell's `Select-String` is deliberately NOT parsed — its parameter
#: syntax needs a different tokenizer, and guessing at it would put made-up
#: patterns into the class the verdict leans on hardest. A PowerShell-only
#: corpus therefore reports zero searches and is REFUSED rather than decided.
_SEARCH_BINARIES = frozenset({
    "grep", "egrep", "fgrep", "rg", "ripgrep", "ag", "ack", "ack-grep", "ugrep",
})

#: Splits a shell command on its control operators, so `cat x | grep foo` is
#: judged on the segment that actually searches. Same expression, for the same
#: reason, as `orchestrator._SHELL_SEGMENT_RE`.
_SHELL_SEGMENT_RE = re.compile(r"&&|\|\||[|;]")

#: Wrappers that precede the real binary and carry no pattern of their own.
_SHELL_WRAPPERS = frozenset({
    "sudo", "command", "time", "env", "nice", "ionice", "xargs", "nohup",
    "stdbuf", "builtin", "exec", "then", "do", "!",
})

#: Short options that consume the NEXT token (or the rest of their own bundle),
#: per binary. `-r` is the reason this is per-binary rather than one set: in
#: grep it means recursive and takes nothing, in ripgrep it is `--replace` and
#: takes a value — one shared table would either eat `grep -r PATTERN`'s
#: pattern or misread `rg -r NEW PATTERN`.
_VALUE_SHORTS = {
    "grep": frozenset("efmABCdD"),
    "rg": frozenset("efgtTMABCmjr"),
    "ag": frozenset("efmABCGp"),
    "ack": frozenset("efmABC"),
}

#: Long options that consume the next token when not written with `=`.
#: `--color`/`--colour` are deliberately ABSENT: their argument is optional and
#: is almost always written `--color=auto`, so listing them would make
#: `grep --color PATTERN` eat its own pattern — the more common shape of the
#: two. The option tables are best-effort, and both ways of being wrong (a
#: missed pattern, a flag's value read as one) only move `symbol_lookup`; the
#: doc states that rather than implying the parse is exact.
_VALUE_LONGS = frozenset({
    "--regexp", "--file", "--max-count", "--after-context", "--before-context",
    "--context", "--include", "--exclude", "--exclude-dir", "--exclude-from",
    "--glob", "--iglob", "--type", "--type-not", "--type-add", "--replace",
    "--max-columns", "--max-depth", "--max-filesize", "--threads",
    "--binary-files", "--devices", "--directories", "--label", "--pre",
})

#: Long options that name a PATTERN rather than a file or a limit.
_PATTERN_LONGS = frozenset({"--regexp"})

#: Extensions a definition/references/hover call can actually answer for. A
#: CLOSED allowlist, and the most consequential constant in this file: the
#: first run of this script counted `.md` as the largest addressable extension
#: by read mass, and no symbol server answers a question about a README. A read
#: outside this list is reported in the ``non_code`` class and is never
#: navigable at any size.
#:
#: Scoped to languages with a real, widely deployed symbol server, because the
#: fix being gated is "stand up a symbol server" — a language whose tooling
#: this product would have to invent cannot be counted as addressed by it. The
#: omissions are deliberate and they all bias toward HALT.
CODE_EXTENSIONS = frozenset({
    ".py", ".pyi",
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts",
    ".go", ".rs", ".java", ".kt", ".kts", ".scala",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh",
    ".cs", ".rb", ".php", ".swift", ".m", ".mm",
    ".lua", ".dart", ".ex", ".exs", ".hs", ".ml", ".zig",
})

#: Where "large" starts. Not a round number picked for looking sensible:
#: `agent/tool_result_cap.py` measured the Read result distribution over 4,775
#: real tool results at median 3,714 chars and p90 17,023. 12,000 sits between
#: them, so this selects the genuinely large tail — roughly the top sixth of
#: reads — rather than the ordinary read the median describes.
DEFAULT_LARGE_READ_CHARS = 12_000

#: What a definition/hover answer would itself cost, so the addressable mass is
#: a saving and not a gross read total. ~2,000 chars is a function with its
#: signature and docstring; it is an ESTIMATE and the output says so, because
#: no symbol server exists here to measure one from. Raise it to see the
#: verdict under a more pessimistic fix.
DEFAULT_SYMBOL_RESPONSE_CHARS = 2_000

#: How far back a read looks for the Grep that motivated it. Three, because the
#: pattern being detected is "search, then open a hit" — a read six calls after
#: a search is a different act, and a wide window would make the class mean
#: "this session ever grepped".
DEFAULT_LOOKBACK = 3

#: Corpus floor. Below either number a share is arithmetic, not evidence, and
#: the verdict says so instead of deciding.
DEFAULT_MIN_READS = 200
DEFAULT_MIN_SESSIONS = 20

#: The decision threshold, PRE-REGISTERED: chosen before this script was first
#: run against any corpus, and stated here so a reader can disagree with it in
#: the open rather than reverse-engineer it from the answer.
#:
#: Why 15%: the ceiling on any fix is the mass it can address, and this fix is
#: not cheap — the issue's own architecture note is that the Agent SDK exposes
#: no LSP tool and cloud sessions start no language server, so a symbol server
#: is infrastructure this product would own and carry, polyglot, forever. The
#: lever it competes with is already measured: lowering the coder's compaction
#: window cut modelled burn 30.9% (`docs/COST_LEVERS.md`) by configuring
#: something the SDK already owns. A net addressable share below 15% cannot pay
#: for owned infrastructure when a configuration change moved twice that.
ADDRESSABLE_SHARE_GATE = 0.15


class EmptyInputSet(SystemExit):
    """Fail CLOSED and loud, never a quiet "0% — halt"."""

    def __init__(self, message: str):
        super().__init__(f"FAIL (empty input set): {message}")


class NoSearchChannel(SystemExit):
    """No search calls in the corpus, so `symbol_lookup` cannot exist.

    The same refusal as `EmptyInputSet` and for the same reason, one signal
    over: a corpus this instrument cannot see searches in can still produce
    reads, clear both floors, and render a confident HALT resting entirely on
    the size heuristic. That is what the first version of this script did on
    the product's own telemetry. An absent channel is a gap in the instrument
    or the source, never evidence about the agent, so it gets no verdict.
    """

    def __init__(self, message: str):
        super().__init__(f"FAIL (no search channel): {message}")


# --------------------------------------------------------------------------- #
# Is this Grep pattern a symbol query, or a text search?                      #
# --------------------------------------------------------------------------- #

#: Declaration keywords a symbol search is commonly anchored on, across the
#: languages this repo and its targets actually contain. Stripped before the
#: remainder is tested for being a bare identifier, so `def parse_config` and
#: `parse_config` classify alike.
_DECL_PREFIX = re.compile(
    r"^(?:async\s+)?(?:export\s+(?:default\s+)?)?"
    r"(?:(?:public|private|protected|static|abstract|final)\s+)?"
    r"(?:def|class|function|interface|type|struct|enum|impl|trait|fn|func|"
    r"const|let|var)\s+",
    re.IGNORECASE,
)

#: A bare identifier. Three characters minimum: a one- or two-letter pattern is
#: a text search that happens to be short, and calling it a symbol lookup would
#: inflate the strongest class with the least evidence.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,}$")

#: An argument list is part of how a name is SPELLED wherever it appears, so
#: `foo(` and `foo\(self` yield `foo` with or without a declaration keyword.
_CALL_NAME_TAIL = re.compile(r"\\?[(]")

#: `=` and `:` only end a name when a DECLARATION keyword introduced it
#: (`const foo: string`, `let foo = 1`). Without one, `kind="tool_result"` and
#: `risk:` are searches for a key or an attribute value, and keeping their
#: left-hand side reported both as go-to-definition — measured as real false
#: positives on 1,470 patterns.
_DECL_NAME_TAIL = re.compile(r"\\?[(=:]")


def is_symbol_query(pattern: str) -> bool:
    """True when ``pattern`` reads as "where is symbol S", not "find this text".

    The test is deliberately CONSERVATIVE — what survives normalisation must be
    one bare identifier — because this is the class the verdict leans on
    hardest. A prose search, an alternation, a character class or a multi-token
    phrase is a text search and stays out, even though some of those are symbol
    questions asked awkwardly. Under-counting the strongest class biases the
    verdict toward HALT, which is the safe direction for a gate that authorises
    building infrastructure.

    `_IDENTIFIER` IS ANCHORED, AND THAT IS THE WHOLE FILTER. What survives
    normalisation must match `^[A-Za-z_][A-Za-z0-9_]{2,}$` end to end, so
    whitespace, quotes, alternations and character classes are all rejected by
    that one test rather than by separate guards. Two earlier versions carried
    an explicit quote check and a token-count bound; once quotes stopped being
    stripped and the `=`/`:` tail became declaration-gated, neither could
    change an outcome, and an unreachable guard whose comment claims to
    prevent a defect is worse than no guard — it tells the next reader the
    wrong thing about what protects what. Both were removed and the cases they
    named are pinned as behaviour in the test table instead.

    TWO RULES CAME OUT OF READING WHAT THIS ACTUALLY ACCEPTED over 1,470 real
    search patterns, and both were false positives in this very class:

    * QUOTES ARE NOT STRIPPED. Stripping them off both ends turned the
      fragment `"Test` (a token `_segment_argv`'s unbalanced-quote fallback
      produced) into `Test`, and the deliberate `'"version"'` — a search for a
      quoted JSON key — into `version`. A symbol's NAME never contains a
      quote, so leaving them in place lets `_IDENTIFIER` reject both: a
      pattern carrying a quote is either a fragment or a search for a string
      literal, and it is text under either reading.
    * THE `=`/`:` TAIL IS ONLY A DECLARATION'S TAIL. Cutting it
      unconditionally kept the left-hand side of `kind="tool_result"` and
      `risk:` and reported them as name lookups, when both search for a key or
      an attribute VALUE that no definition call answers. It is cut only when
      a declaration keyword was actually stripped, which is the case it was
      written for (`const foo: string`). `foo(` needs no keyword and keeps its
      own split, and prose keeps its spaces and so cannot match.
    """
    if not pattern or len(pattern) > 80:
        # A long pattern is prose or a composed regex, and a long single token
        # is not a name anyone typed. The bound also keeps this cheap on a
        # corpus carrying a pathological pattern.
        return False
    # `\s+`/`\s*` are how a symbol search spells the space in `def  foo`, and
    # `\b` is how it anchors one. Both are word-boundary noise, not structure.
    p = pattern.strip()
    p = p.replace("\\s+", " ").replace("\\s*", " ").replace("\\b", "")
    p = p.strip().lstrip("^").rstrip("$").strip()
    stripped = _DECL_PREFIX.sub("", p, count=1).strip()
    declared = stripped != p
    tail = _DECL_NAME_TAIL if declared else _CALL_NAME_TAIL
    return bool(_IDENTIFIER.match(tail.split(stripped, maxsplit=1)[0].strip()))


# --------------------------------------------------------------------------- #
# The coder's real search channel: a grep inside a shell command              #
# --------------------------------------------------------------------------- #

def _basename(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1]


def _segment_argv(segment: str) -> tuple[list[str], bool]:
    """``(tokens, tokenized_cleanly)`` for one shell segment.

    `shlex` is used so `grep -n "def parse_config" src/` yields the pattern as
    ONE token rather than two. It raises on an unbalanced quote, which a real
    command legitimately has (an embedded regex, a partial heredoc), and the
    fallback is a plain split rather than dropping the segment — dropping it
    would shrink the search census, and the census is what decides whether
    this script may return a verdict at all.

    But a fallback split's tokens are NOT a parse, and the second element of
    the return is what says so. Splitting `grep -rn "def foo bar" .` on
    whitespace makes the pattern operand `"def`, a fragment that is not the
    thing searched for. Such a segment is reported as a SEARCH carrying NO
    pattern, which is the honest reading and is already a column
    (``searches_with_pattern``) — a fabricated pattern would land in the class
    the verdict leans on hardest.
    """
    try:
        return shlex.split(segment, posix=True), True
    except ValueError:
        return segment.split(), False


def _pattern_from_argv(binary: str, argv: list[str]) -> str | None:
    """The search PATTERN in one search invocation's arguments, or ``None``.

    Explicit `-e`/`--regexp` wins over the positional operand, because when
    both are present the operand is a PATH. `-f FILE` means the patterns live
    in a file this script cannot see, so it yields ``None`` — an unextractable
    pattern is reported as such and never guessed at.
    """
    value_shorts = _VALUE_SHORTS.get(binary, _VALUE_SHORTS["grep"])
    explicit: list[str] = []
    operand: str | None = None
    from_file = False
    index = 0
    while index < len(argv):
        token = argv[index]
        index += 1
        if token == "--":
            # Everything after `--` is an operand: the first is the pattern.
            if operand is None and index < len(argv):
                operand = argv[index]
            break
        if token.startswith("--"):
            name, sep, inline = token.partition("=")
            if name in _PATTERN_LONGS:
                if sep:
                    explicit.append(inline)
                elif index < len(argv):
                    explicit.append(argv[index])
                    index += 1
            elif name in ("--file",):
                from_file = True
                if not sep and index < len(argv):
                    index += 1
            elif name in _VALUE_LONGS and not sep and index < len(argv):
                index += 1
            continue
        if token.startswith("-") and len(token) > 1:
            # A bundle of short options: `-rn`, `-A3`, `-rne PATTERN`. A letter
            # that takes a value consumes the REST of the bundle when there is
            # one, else the next token.
            letters = token[1:]
            for position, letter in enumerate(letters):
                if letter not in value_shorts:
                    continue
                rest = letters[position + 1:]
                if letter == "e":
                    if rest:
                        explicit.append(rest)
                    elif index < len(argv):
                        explicit.append(argv[index])
                        index += 1
                elif letter == "f":
                    from_file = True
                    if not rest and index < len(argv):
                        index += 1
                elif not rest and index < len(argv):
                    index += 1
                break
            continue
        if operand is None:
            operand = token
    if explicit:
        return explicit[0]
    if from_file:
        # The pattern exists but not where this script can read it. Saying so
        # keeps `searches` and `searches_with_pattern` honestly different.
        return None
    return operand


def shell_search(command: str) -> tuple[bool, tuple[str, ...]]:
    """``(is_a_search, patterns)`` for one shell command.

    Per SEGMENT, so a search anywhere in a pipeline counts and a compound
    command is not judged by its first word alone — the same decomposition
    `orchestrator._looks_like_test_run` makes, and for the same measured
    reason: a corpus replay found `rg pytest` misread as a test RUN when the
    command was inspected whole.

    The two halves of the return are deliberately independent. A recognised
    search whose pattern cannot be extracted (`grep -f patterns.txt`) is still
    a SEARCH, and collapsing the two would let a parser gap read as "this
    corpus does not search" — the exact false negative that made the first
    version of this script decide the phase on one signal.
    """
    if not command:
        return False, ()
    found = False
    patterns: list[str] = []
    for segment in _SHELL_SEGMENT_RE.split(command):
        argv, clean = _segment_argv(segment.strip())
        cursor = 0
        # Step past `FOO=bar` env assignments and wrappers to the real binary.
        while cursor < len(argv) and (
                _SHELL_WRAPPERS.__contains__(_basename(argv[cursor]))
                or ("=" in argv[cursor] and not argv[cursor].startswith("-")
                    and argv[cursor].split("=", 1)[0].isidentifier())):
            cursor += 1
        if cursor >= len(argv):
            continue
        binary = _basename(argv[cursor])
        rest = argv[cursor + 1:]
        if binary == "git":
            if not rest or rest[0] != "grep":
                continue
            binary, rest = "grep", rest[1:]
        elif binary not in _SEARCH_BINARIES:
            continue
        found = True
        if not clean:
            # A search, and one whose pattern this script will not invent.
            continue
        table = "rg" if binary in ("rg", "ripgrep") else (
            binary if binary in _VALUE_SHORTS else "grep")
        pattern = _pattern_from_argv(table, rest)
        if pattern:
            patterns.append(pattern)
    return found, tuple(patterns)


# --------------------------------------------------------------------------- #
# The normalized shapes both readers produce                                  #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Call:
    """One tool call, as much of it as both sources agree on."""

    tool: str
    use_id: str | None
    path: str = ""
    #: Every search pattern this one call carried. A tuple because one shell
    #: command can search twice (`grep a src | grep b`), and because a search
    #: whose pattern could not be extracted carries none while still being a
    #: search — see `is_search`.
    patterns: tuple[str, ...] = ()
    #: This call searched, whether or not a pattern came out of it.
    is_search: bool = False
    windowed: bool = False
    sidechain: bool = False

    @property
    def symbol_search(self) -> bool:
        return any(is_symbol_query(p) for p in self.patterns)


@dataclass
class Window:
    """One context window: the calls between two context resets.

    An attempt boundary or a compaction closes one. Nothing is weighted across
    a window edge, because nothing is re-sent across it.
    """

    calls: list[Call] = field(default_factory=list)


@dataclass
class Corpus:
    """What a reader returns: windows, result sizes, and its own census.

    ``sizes`` maps a ``tool_use_id`` to ``(result_chars, from_subagent)``. The
    subagent flag rides with the RESULT because that is where both sources put
    it: the product records `parent_tool_use_id` on the result event only.
    """

    source: str
    windows: list[Window] = field(default_factory=list)
    sizes: dict[str, tuple[int, bool]] = field(default_factory=dict)
    sessions: int = 0
    malformed: int = 0


def _windowed(inp: dict) -> bool:
    """True when the read asked for a slice rather than the whole file.

    `is not None` rather than truthiness, and that distinction is real in both
    directions: `offset=0` with a `limit` IS a window, and `limit=0` is not a
    request for one. Same reading of the same input as
    `orchestrator._summarize_tool_sig`.
    """
    return inp.get("offset") is not None or inp.get("limit") is not None


def _read_path(inp: dict) -> str:
    return str(inp.get("file_path") or inp.get("path")
               or inp.get("notebook_path") or "")


def _search_pattern(inp: dict) -> str:
    return str(inp.get("pattern") or inp.get("query") or "")


def _shell_command(inp: dict) -> str:
    """The command text, under either key the two sources use."""
    return str(inp.get("command") or inp.get("cmd") or "")


def _call_from(tool: str, inp: dict, use_id: str | None,
               *, sidechain: bool) -> Call:
    """Normalize one tool call.

    Every tool is kept, not just the read and search ones, and that is
    load-bearing: a dropped call still has to COUNT toward the turns remaining
    in its window, because a Bash turn re-sends the conversation exactly like a
    Read turn does. Only the fields each class needs are extracted.
    """
    if tool in READ_TOOLS:
        return Call(tool=tool, use_id=use_id, path=_read_path(inp),
                    windowed=_windowed(inp), sidechain=sidechain)
    if tool in SYMBOL_QUERY_TOOLS:
        pattern = _search_pattern(inp)
        return Call(tool=tool, use_id=use_id, is_search=True,
                    patterns=(pattern,) if pattern else (),
                    sidechain=sidechain)
    if tool in SHELL_TOOLS:
        found, patterns = shell_search(_shell_command(inp))
        return Call(tool=tool, use_id=use_id, is_search=found,
                    patterns=patterns, sidechain=sidechain)
    return Call(tool=tool, use_id=use_id, sidechain=sidechain)


# --------------------------------------------------------------------------- #
# Reader 1 — the product's own coder telemetry (`task_events`)                #
# --------------------------------------------------------------------------- #

def _connect(db_path: Path) -> sqlite3.Connection:
    """Open the DB strictly read-only. A measurement may never write."""
    if not db_path.exists():
        raise SystemExit(f"no database at {db_path} (pass --db PATH)")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def read_events(con: sqlite3.Connection) -> Corpus:
    """Coder tool calls from ``task_events``, split into context windows.

    Ordered by ``ts`` then ``rowid``: a tool call and its result are routinely
    written inside one clock tick, and ordering by ts alone would leave their
    sequence to SQLite's scan order — which is exactly what the lookback and
    the turns-remaining count read.

    One pass over every event, not a query per kind, because the window
    boundaries (``attempt_start``, ``compaction``) are emitted by the
    ORCHESTRATOR under a different `source` than the coder's own calls and
    interleaved with them by timestamp. Fetching them separately would mean
    re-deriving the interleaving this ordering already gives.
    """
    corpus = Corpus(source="events")
    per_task: dict[str, list[Window]] = {}
    current: dict[str, Window] = {}

    for task_id, data in con.execute(
        "SELECT task_id, data FROM task_events ORDER BY ts, rowid"
    ):
        try:
            event = json.loads(data)
        except (ValueError, TypeError):
            corpus.malformed += 1
            continue
        if not isinstance(event, dict):
            corpus.malformed += 1
            continue
        kind = event.get("kind")
        key = str(task_id)

        if kind in (ATTEMPT_START_KIND, COMPACTION_KIND):
            # A boundary from ANY source closes this task's window: the coder
            # session being reset is the same one either way.
            window = current.pop(key, None)
            if window is not None and window.calls:
                per_task.setdefault(key, []).append(window)
            continue

        if event.get("source") != CODER_SOURCE:
            continue

        if kind == "tool_result":
            use_id = event.get("tool_use_id")
            chars = event.get("result_chars")
            # The bool test is not redundant: `True` IS an `int` in Python, and
            # a `result_chars: true` folded in as 1 would be phantom mass at
            # the low end of the very distribution being measured.
            if (isinstance(use_id, str) and isinstance(chars, int)
                    and not isinstance(chars, bool)):
                corpus.sizes[use_id] = (
                    chars, event.get("parent_tool_use_id") is not None)
            continue

        if kind != "tool_use":
            continue
        tool = event.get("tool_name")
        if not isinstance(tool, str):
            continue
        inp = event.get("tool_input")
        if not isinstance(inp, dict):
            inp = {}
        use_id = event.get("tool_use_id")
        current.setdefault(key, Window()).calls.append(_call_from(
            tool, inp, use_id if isinstance(use_id, str) else None,
            sidechain=False,
        ))

    for key, window in current.items():
        if window.calls:
            per_task.setdefault(key, []).append(window)

    corpus.sessions = len(per_task)
    corpus.windows = [w for windows in per_task.values() for w in windows]
    return corpus


# --------------------------------------------------------------------------- #
# Reader 2 — Claude Code's own session logs                                   #
# --------------------------------------------------------------------------- #

def _transcript_files(roots: tuple[Path, ...]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(sorted(root.glob("*/*.jsonl")))
    return files


def _result_chars(content: object) -> int:
    """Result size as the MODEL sees it, mirroring `claude_backend._result_size`.

    Restated rather than imported (this script imports nothing from the
    product), and it has to stay faithful: `str(content)` over a block list
    counts dict-repr punctuation as payload, which plants phantom mass at the
    low end of the very distribution being measured. An image block carries no
    text and contributes 0 here, same as there.
    """
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" or "text" in block:
                    total += len(str(block.get("text") or ""))
            else:
                total += len(str(block))
        return total
    return len(str(content))


def _is_compact_boundary(entry: dict) -> bool:
    """A compaction, in either of the two shapes the CLI writes it."""
    return bool(entry.get("isCompactSummary")
                or entry.get("subtype") == "compact_boundary")


def read_transcripts(files: list[Path]) -> Corpus:
    """Tool calls from Claude Code session logs, split into context windows.

    One file is one session; a compaction inside it closes a window. Lines are
    read individually and a malformed one is COUNTED and skipped — a
    half-written last line is the normal state of a log belonging to a live
    session, and a corpus that refused to load because of it would be
    unmeasurable exactly while it is most current.
    """
    corpus = Corpus(source="transcripts")
    for path in files:
        windows: list[Window] = []
        current = Window()
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            corpus.malformed += 1
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    corpus.malformed += 1
                    continue
                if not isinstance(entry, dict):
                    corpus.malformed += 1
                    continue
                if _is_compact_boundary(entry):
                    if current.calls:
                        windows.append(current)
                    current = Window()
                    continue
                message = entry.get("message")
                blocks = (message.get("content")
                          if isinstance(message, dict) else None)
                if not isinstance(blocks, list):
                    continue
                sidechain = bool(entry.get("isSidechain"))
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    kind = block.get("type")
                    if kind == "tool_use":
                        tool = block.get("name")
                        if not isinstance(tool, str):
                            continue
                        inp = block.get("input")
                        if not isinstance(inp, dict):
                            inp = {}
                        use_id = block.get("id")
                        current.calls.append(_call_from(
                            tool, inp,
                            use_id if isinstance(use_id, str) else None,
                            sidechain=sidechain,
                        ))
                    elif kind == "tool_result":
                        use_id = block.get("tool_use_id")
                        if isinstance(use_id, str):
                            corpus.sizes[use_id] = (
                                _result_chars(block.get("content")), sidechain)
        if current.calls:
            windows.append(current)
        if windows:
            corpus.sessions += 1
            corpus.windows.extend(windows)
    return corpus


# --------------------------------------------------------------------------- #
# Classification                                                              #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ReadCall:
    """One read, with everything the aggregation needs and nothing else.

    No path and no pattern survive this far on purpose. The identifying
    material is dropped at the last point that still needs it, rather than
    carried to the print and filtered there — a filter is something a later
    edit can forget.
    """

    ext: str
    chars: int | None
    windowed: bool
    sidechain: bool
    symbol_lookup: bool
    whole_file: bool
    repeat: bool
    turns_after: int

    @property
    def navigable(self) -> bool:
        """The union of the two addressable classes.

        No ``CODE_EXTENSIONS`` test here: `classify` is the single place that
        gate is applied, to both classes at once, so a read outside the
        allowlist reaches this property with both flags already false. A
        second copy of the test would be a second thing to keep in agreement.
        """
        return self.symbol_lookup or self.whole_file

    @property
    def non_code(self) -> bool:
        """The complement of the gate `classify` applied, off the same field.

        Re-derived rather than stored, so it cannot drift out of step with the
        gate: both read ``ext`` against the same allowlist, so they cannot
        disagree about one read.
        """
        return self.ext not in CODE_EXTENSIONS


def _extension(path: str) -> str:
    """Lowercased suffix, or ``"(none)"``. The only path-derived thing reported.

    Taken off the basename, so a dotted DIRECTORY (`.venv`, `.github`) cannot
    be reported as the language of a file that has no suffix of its own.
    """
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, ext = base.rpartition(".")
    # `stem` empty means the dot was leading (`.gitignore`): a dotfile's name
    # is not an extension, and reporting it as one would invent a language.
    return f".{ext.lower()}" if dot and ext and stem else "(none)"


def search_census(corpus: Corpus) -> dict[str, int]:
    """How much of a search channel this corpus actually has.

    THE VERDICT IS NOT ALLOWED TO RUN WITHOUT THIS. `symbol_lookup` reaching 0
    has two completely different causes — the agent never asked a symbol
    question, or this source records no searches for the instrument to read —
    and the class alone renders them identically. The first version of this
    script had no census, so on the product's own telemetry it reported a
    confident negative built on a signal that could not exist.

    Three numbers, not one, because the middle one is the parser's own
    coverage: a recognised search whose pattern could not be extracted is
    counted as a search and not as a symbol one, so a gap in the option tables
    shows up as a gap rather than as evidence about the agent.
    """
    searches = with_pattern = symbol = 0
    for window in corpus.windows:
        for call in window.calls:
            if not call.is_search:
                continue
            searches += 1
            if call.patterns:
                with_pattern += 1
            if call.symbol_search:
                symbol += 1
    return {"searches": searches, "searches_with_pattern": with_pattern,
            "symbol_searches": symbol}


def classify(corpus: Corpus, *, large_read_chars: int,
             lookback: int) -> list[ReadCall]:
    """Every read in the corpus, classified within its own context window."""
    out: list[ReadCall] = []
    for window in corpus.windows:
        calls = window.calls
        seen_paths: set[str] = set()
        for index, call in enumerate(calls):
            if call.tool not in READ_TOOLS:
                continue
            size = corpus.sizes.get(call.use_id) if call.use_id else None
            chars = size[0] if size is not None else None
            # A subagent's read is one if EITHER end says so: transcripts flag
            # the call, `task_events` flags only the result.
            sidechain = call.sidechain or bool(size is not None and size[1])
            ext = _extension(call.path)
            # A symbol server answers questions about CODE. Gating both
            # addressable classes here, once, is what stops a whole-file read
            # of a README from being counted as a definition lookup.
            symbol_served = ext in CODE_EXTENSIONS
            # The window's own earlier calls, bounded — so this class means
            # "just searched for a symbol", not "ever searched".
            start = max(0, index - lookback) if lookback else index
            symbol_lookup = symbol_served and any(
                prior.symbol_search for prior in calls[start:index]
            )
            whole_file = (symbol_served and not call.windowed
                          and chars is not None
                          and chars >= large_read_chars)
            key = call.path.replace("\\", "/").lower()
            repeat = bool(key) and key in seen_paths
            if key:
                seen_paths.add(key)
            out.append(ReadCall(
                ext=ext,
                chars=chars,
                windowed=call.windowed,
                sidechain=sidechain,
                symbol_lookup=symbol_lookup,
                whole_file=whole_file,
                repeat=repeat,
                turns_after=len(calls) - index - 1,
            ))
    return out


# --------------------------------------------------------------------------- #
# Aggregation                                                                 #
# --------------------------------------------------------------------------- #

#: Every key an aggregate carries, so a zero is emitted as a zero. A dict
#: assembled only from what was incremented changes SHAPE with the data, and
#: the reader most likely to be automated is the one least able to tell an
#: absent key from a zero (`review_round_value.py` learned this the same way).
_CLASSES = ("all", "symbol_lookup", "whole_file", "navigable", "both",
            "non_code", "repeat", "windowed", "sidechain")


def _bucket() -> dict[str, int]:
    return {"reads": 0, "chars": 0, "weighted": 0}


def aggregate(reads: list[ReadCall], *, symbol_response_chars: int) -> dict:
    """Counts and mass per class, plus the net addressable share in both units.

    ``chars`` is measured. ``weighted`` is ``chars x turns_after`` — the model,
    and the reason a read's position in its window matters at all. Subagent
    reads carry mass in ``chars`` and ZERO in ``weighted``: their result is
    re-read in the subagent's own context, whose remaining turns neither source
    records. They keep their own row so the exclusion is visible, not silent.
    """
    classes = {name: _bucket() for name in _CLASSES}
    unsized = 0
    addressable = {"chars": 0, "weighted": 0}
    by_ext: dict[str, dict[str, int]] = {}

    for read in reads:
        weight = 0 if read.sidechain else read.turns_after
        chars = read.chars or 0
        mass = chars * weight
        if read.chars is None:
            unsized += 1

        # Every class this read belongs to, collected before anything is
        # incremented. The classes OVERLAP by design (a read can be a
        # whole-file read of a file already read, in a subagent), so this is a
        # membership list and never a partition — the one arithmetic
        # relationship that does hold is navigable = symbol + whole - both.
        members = ["all"]
        if read.symbol_lookup:
            members.append("symbol_lookup")
        if read.whole_file:
            members.append("whole_file")
        if read.symbol_lookup and read.whole_file:
            members.append("both")
        if read.navigable:
            members.append("navigable")
        if read.non_code:
            members.append("non_code")
        if read.repeat:
            members.append("repeat")
        if read.windowed:
            members.append("windowed")
        if read.sidechain:
            members.append("sidechain")
        for name in members:
            bucket = classes[name]
            bucket["reads"] += 1
            bucket["chars"] += chars
            bucket["weighted"] += mass

        if read.navigable:
            # The saving, not the read: a symbol call answers with a symbol.
            net = max(0, chars - symbol_response_chars)
            addressable["chars"] += net
            addressable["weighted"] += net * weight

        ext = by_ext.setdefault(read.ext, _bucket())
        ext["reads"] += 1
        ext["chars"] += chars
        ext["weighted"] += mass

    def share(unit: str) -> float:
        total = classes["all"][unit]
        return addressable[unit] / total if total else 0.0

    return {
        "classes": classes,
        "reads_without_result": unsized,
        "addressable": dict(addressable),
        "share_chars": share("chars"),
        "share_weighted": share("weighted"),
        "by_extension": dict(sorted(
            by_ext.items(), key=lambda kv: (-kv[1]["chars"], kv[0]))),
        "symbol_response_chars": symbol_response_chars,
    }


# --------------------------------------------------------------------------- #
# Verdict                                                                     #
# --------------------------------------------------------------------------- #

def verdict(data: dict, corpus_sessions: int, *, min_reads: int,
            min_sessions: int, gate: float = ADDRESSABLE_SHARE_GATE) -> dict:
    """PROCEED / HALT / INCONCLUSIVE against the pre-registered rule.

    Three conditions, all stated in this module's docstring before any corpus
    was read:

      (a) the corpus clears its floor, else the share is arithmetic;
      (b) the net addressable share clears ``gate`` in BOTH units;
      (c) the two units agree in direction, else the honest answer is that the
          measurement does not decide.

    (c) is not decoration. ``chars`` and ``weighted`` can disagree in a real
    way — many small navigable reads early in long windows carry little raw
    mass and a great deal of weighted mass, and the opposite shape exists too.
    A rule that took either one alone would let the choice of unit pick the
    answer.
    """
    sized = data["classes"]["all"]["reads"] - data["reads_without_result"]
    chars_ok = data["share_chars"] >= gate
    weighted_ok = data["share_weighted"] >= gate

    if sized < min_reads or corpus_sessions < min_sessions:
        return {
            "decision": "INCONCLUSIVE",
            "reasons": [
                f"corpus floor not met: {sized} sized read(s) across "
                f"{corpus_sessions} session(s), against a floor of "
                f"{min_reads} reads and {min_sessions} sessions. The shares "
                "above are computed but must not be used to decide the phase."
            ],
        }
    if chars_ok != weighted_ok:
        return {
            "decision": "INCONCLUSIVE",
            "reasons": [
                f"the two units disagree: raw share "
                f"{data['share_chars']:.1%} and weighted share "
                f"{data['share_weighted']:.1%} fall on opposite sides of the "
                f"{gate:.0%} gate. Which unit is right would decide the "
                "phase, so the measurement does not."
            ],
        }
    if chars_ok:
        return {
            "decision": "PROCEED",
            "reasons": [
                f"net addressable read mass is {data['share_chars']:.1%} raw "
                f"and {data['share_weighted']:.1%} weighted, both at or above "
                f"the pre-registered {gate:.0%} gate. This authorises the A/B "
                "in the issue: an in-process SDK MCP server with read-only "
                "symbol tools, scored on success rate, cost and wall-clock "
                "against a control. It authorises nothing beyond it."
            ],
        }
    return {
        "decision": "HALT",
        "reasons": [
            f"net addressable read mass is {data['share_chars']:.1%} raw and "
            f"{data['share_weighted']:.1%} weighted, both below the "
            f"pre-registered {gate:.0%} gate. Phase 3's terms halt "
            "development on a negative result: no symbol server, and phase 4 "
            "(polyglot wiring evidence), which the issue makes contingent on "
            "this, stays closed."
        ],
    }


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #

def _pct(part: int, whole: int) -> str:
    return f"{part / whole:.1%}" if whole else "n/a"


def render(report: dict) -> str:
    data = report["measurement"]
    classes = data["classes"]
    total = classes["all"]
    out: list[str] = []
    out.append(
        f"source: {report['source']} (read-only), {report['sessions']} "
        f"session(s), {report['windows']} context window(s), "
        f"{total['reads']} read(s), {data['reads_without_result']} with no "
        "recorded result"
    )
    census = report["search_census"]
    out.append(
        f"search channel: {census['searches']} search call(s), "
        f"{census['searches_with_pattern']} with an extractable pattern, "
        f"{census['symbol_searches']} symbol-shaped"
    )
    out.append(
        f"thresholds: large read >= {report['large_read_chars']:,} chars, "
        f"lookback {report['lookback']} call(s), symbol answer estimated at "
        f"{data['symbol_response_chars']:,} chars"
    )
    if report["malformed"]:
        out.append(f"skipped {report['malformed']} malformed record(s)")
    out.append("")
    header = (f"{'class':>14}  {'reads':>6}  {'chars':>12}  {'%chars':>7}  "
              f"{'weighted':>16}  {'%wtd':>7}")
    out.append(header)
    out.append("-" * len(header))
    for name in _CLASSES:
        bucket = classes[name]
        out.append(
            f"{name:>14}  {bucket['reads']:>6}  {bucket['chars']:>12,}  "
            f"{_pct(bucket['chars'], total['chars']):>7}  "
            f"{bucket['weighted']:>16,}  "
            f"{_pct(bucket['weighted'], total['weighted']):>7}"
        )
    out.append("")
    out.append(
        "net addressable (navigable mass less the estimated symbol answer): "
        f"{data['addressable']['chars']:,} chars "
        f"({data['share_chars']:.1%} of read mass), "
        f"{data['addressable']['weighted']:,} weighted "
        f"({data['share_weighted']:.1%})"
    )
    if data["by_extension"]:
        out.append("")
        out.append("read mass by extension (top 8):")
        for ext, bucket in list(data["by_extension"].items())[:8]:
            out.append(
                f"{ext:>14}  {bucket['reads']:>6}  {bucket['chars']:>12,}  "
                f"{_pct(bucket['chars'], total['chars']):>7}"
            )
    out.append("")
    out.append(f"VERDICT: {report['verdict']['decision']}")
    for reason in report["verdict"]["reasons"]:
        out.append(f"  {reason}")
    probes = report["robustness"]["probed"]

    def probe_line(threshold: str) -> str:
        probe = probes[threshold]
        return (f"{int(threshold):,} chars gives {probe['decision']} on "
                f"{probe['navigable_reads']} navigable read(s)")

    ordered = sorted(probes, key=int)
    if report["robustness"]["fragile"]:
        out.append(
            "  CAUTION: this verdict is not robust to the large-read "
            "threshold. Re-deciding at "
            + ", ".join(probe_line(t) for t in ordered)
            + ". The decision above is the one the pre-registered threshold "
              "gives, which stops the number being tuned to the answer -- it "
              "does not make the answer a property of the data. Do not close "
              "the phase on it without the authoritative population."
        )
    elif probes:
        out.append(
            "  robustness: the same decision at "
            + ", ".join(probe_line(t) for t in ordered)
            + ". A probe qualifying far fewer reads than the shipped "
              "threshold could not have disagreed, so read its count before "
              "reading its agreement as corroboration."
        )
    if classes["sidechain"]["reads"]:
        out.append(
            f"NOTE: {classes['sidechain']['reads']} read(s) came from a "
            "subagent. They carry raw chars and zero weighted mass: a "
            "subagent's result is re-read in its own context, whose remaining "
            "turns neither source records."
        )
    # The three ways `symbol_lookup` reaches zero are three different facts,
    # and the first version of this script printed the last of them for all
    # three. An absent channel is a gap in the source or the parser; searches
    # that yielded no pattern are a gap in the option tables; searches that
    # were all text is the only one of the three that says anything about the
    # agent. Rendering them identically is what let a confident negative be
    # read off a signal that could not exist.
    if not classes["symbol_lookup"]["reads"]:
        if not census["searches"]:
            out.append(
                "NOTE: this source recorded NO search calls at all, so "
                "symbol_lookup could not have been anything but zero. That is "
                "a gap in the source or in this instrument, never evidence "
                "about the agent."
            )
        elif not census["searches_with_pattern"]:
            out.append(
                f"NOTE: {census['searches']} search call(s) were seen and a "
                "pattern could be extracted from none of them, so "
                "symbol_lookup is measuring this script's option tables "
                "rather than the agent."
            )
        elif not census["symbol_searches"]:
            out.append(
                f"NOTE: {census['searches_with_pattern']} search pattern(s) "
                "were read and none was symbol-shaped. Every search this "
                "corpus records is a text search, which IS a fact about the "
                "agent -- unlike a zero produced by an absent channel."
            )
        else:
            out.append(
                f"NOTE: {census['symbol_searches']} symbol-shaped search(es) "
                "were seen, but no read followed one within the "
                f"{report['lookback']}-call lookback. A symbol question "
                "answered from memory, or answered by the search itself, "
                "leaves no read behind."
            )
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

def _has_reads(corpus: Corpus) -> bool:
    return any(call.tool in READ_TOOLS
               for window in corpus.windows for call in window.calls)


def pick_source(requested: str, db: Path, roots: tuple[Path, ...]) -> Corpus:
    """Load the requested source, or auto-pick — and name what was taken.

    ``auto`` prefers the product's own telemetry and falls back to the
    transcripts, because `task_events` is the population the phase is actually
    about — the unattended coder — and the transcripts are a substitute for it,
    never the other way round. A missing DB under ``auto`` is not an error;
    under ``--source events`` it is, because there the caller named the
    population and silently measuring a different one would be the worst
    outcome available.
    """
    if requested == "events":
        con = _connect(db)
        try:
            return read_events(con)
        finally:
            con.close()
    if requested == "auto" and db.exists():
        con = _connect(db)
        try:
            corpus = read_events(con)
        finally:
            con.close()
        if _has_reads(corpus):
            return corpus
    return read_transcripts(_transcript_files(roots))


#: Factors the large-read threshold is re-probed at, to see whether the verdict
#: is a property of the DATA or of that one constant. Half and double, because
#: half of the shipped 12,000 lands near the measured MEDIAN read (3,714) and
#: double lands past the measured p90 (17,023) — the two ends of the range a
#: reader could reasonably have picked instead.
_ROBUSTNESS_FACTORS = (0.5, 2.0)


def robustness(corpus: Corpus, *, decision: str, large_read_chars: int,
               lookback: int, symbol_response_chars: int, min_reads: int,
               min_sessions: int) -> dict:
    """Re-decide at half and double the large-read threshold.

    THIS EXISTS BECAUSE THE FIRST REAL RUN NEEDED IT. On the transcript
    corpus the pre-registered 12,000-char threshold gives HALT at 11.2% / 14.6%
    — and 6,000 gives PROCEED. Same data, same corpus, one
    constant, opposite decisions about whether this project builds a symbol
    server. A verdict that turns on a threshold rather than on the data has to
    say so out loud, or the pre-registration is decorative: it stops anyone
    tuning the number AFTER seeing the answer, which is not the same as the
    answer being robust.

    Reported as a CAUTION on the verdict rather than as a fourth decision.
    The decision vocabulary stays three-valued because a fragile HALT is still
    a HALT under the terms that were registered — it is just a HALT nobody
    should close the phase on without the authoritative population.

    A PROBE CARRIES ITS QUALIFYING COUNT, and that number is what makes it
    evidence or not. Raise the threshold far enough and almost no read clears
    it — on the fleet database the doubled probe qualified 21 reads out of
    35,557 — so that probe can only ever return HALT and agreeing with the
    shipped decision tells a reader nothing. Printing the count is what stops
    "the same decision at both probes" being read as corroboration when one
    side of it was arithmetically incapable of disagreeing.
    """
    probes: dict[str, dict] = {}
    for factor in _ROBUSTNESS_FACTORS:
        threshold = int(large_read_chars * factor)
        if threshold == large_read_chars:
            continue
        data = aggregate(
            classify(corpus, large_read_chars=threshold, lookback=lookback),
            symbol_response_chars=symbol_response_chars)
        probes[str(threshold)] = {
            "decision": verdict(data, corpus.sessions, min_reads=min_reads,
                                min_sessions=min_sessions)["decision"],
            "navigable_reads": data["classes"]["navigable"]["reads"],
            "whole_file_reads": data["classes"]["whole_file"]["reads"],
            "share_chars": data["share_chars"],
            "share_weighted": data["share_weighted"],
        }
    return {
        "probed": probes,
        "fragile": any(probe["decision"] != decision
                       for probe in probes.values()),
    }


def collect(corpus: Corpus, *, large_read_chars: int, lookback: int,
            symbol_response_chars: int, min_reads: int,
            min_sessions: int) -> dict:
    reads = classify(corpus, large_read_chars=large_read_chars,
                     lookback=lookback)
    data = aggregate(reads, symbol_response_chars=symbol_response_chars)
    decision = verdict(data, corpus.sessions, min_reads=min_reads,
                       min_sessions=min_sessions)
    return {
        "search_census": search_census(corpus),
        "source": corpus.source,
        "sessions": corpus.sessions,
        "windows": len(corpus.windows),
        "malformed": corpus.malformed,
        "large_read_chars": large_read_chars,
        "lookback": lookback,
        "measurement": data,
        "verdict": decision,
        "robustness": robustness(
            corpus, decision=decision["decision"],
            large_read_chars=large_read_chars, lookback=lookback,
            symbol_response_chars=symbol_response_chars,
            min_reads=min_reads, min_sessions=min_sessions),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", choices=("auto", "events", "transcripts"),
                    default="auto",
                    help="which recorded population to measure (default: auto)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB,
                    help=f"SQLite database to read (default: {DEFAULT_DB})")
    ap.add_argument("--transcript-root", type=Path, action="append",
                    default=None,
                    help="Claude Code projects dir; repeatable "
                         "(default: ~/.claude and ~/.claude-personal)")
    ap.add_argument("--large-read-chars", type=int,
                    default=DEFAULT_LARGE_READ_CHARS,
                    help="where a whole-file read counts as large "
                         f"(default: {DEFAULT_LARGE_READ_CHARS})")
    ap.add_argument("--symbol-response-chars", type=int,
                    default=DEFAULT_SYMBOL_RESPONSE_CHARS,
                    help="estimated cost of a symbol answer, subtracted from "
                         "every navigable read (default: "
                         f"{DEFAULT_SYMBOL_RESPONSE_CHARS})")
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK,
                    help="tool calls a read looks back for a symbol search "
                         f"(default: {DEFAULT_LOOKBACK})")
    ap.add_argument("--min-reads", type=int, default=DEFAULT_MIN_READS,
                    help="corpus floor, sized reads "
                         f"(default: {DEFAULT_MIN_READS})")
    ap.add_argument("--min-sessions", type=int, default=DEFAULT_MIN_SESSIONS,
                    help="corpus floor, sessions "
                         f"(default: {DEFAULT_MIN_SESSIONS})")
    ap.add_argument("--json", action="store_true",
                    help="emit the aggregates as JSON")
    args = ap.parse_args(argv)

    # Negatives are rejected rather than clamped: every one of these would
    # still produce a plausible-looking table, and a threshold silently
    # corrected to 0 is a measurement of something nobody asked for.
    for name, value in (("--large-read-chars", args.large_read_chars),
                        ("--symbol-response-chars", args.symbol_response_chars),
                        ("--lookback", args.lookback),
                        ("--min-reads", args.min_reads),
                        ("--min-sessions", args.min_sessions)):
        if value < 0:
            raise SystemExit(f"{name} must not be negative (got {value})")

    roots = (tuple(args.transcript_root) if args.transcript_root
             else TRANSCRIPT_ROOTS)
    corpus = pick_source(args.source, args.db, roots)
    report = collect(
        corpus,
        large_read_chars=args.large_read_chars,
        lookback=args.lookback,
        symbol_response_chars=args.symbol_response_chars,
        min_reads=args.min_reads,
        min_sessions=args.min_sessions,
    )

    if report["measurement"]["classes"]["all"]["reads"] == 0:
        raise EmptyInputSet(
            f"no reads recorded in the {report['source']} population "
            f"({report['sessions']} session(s) scanned). An empty corpus is "
            "not a negative result and must not close this phase."
        )

    # Reads without searches is the shape that produced a wrong confident
    # answer once already: both floors clear, the table renders, and the whole
    # verdict rests on the size heuristic because the other signal was
    # structurally unreachable. Refused for the same reason zero reads is.
    if report["search_census"]["searches"] == 0:
        raise NoSearchChannel(
            f"the {report['source']} population records "
            f"{report['measurement']['classes']['all']['reads']} read(s) and "
            "no search calls, so symbol_lookup could only ever be 0 and the "
            "verdict would rest entirely on the large-read heuristic. If this "
            "is `--source events`, the coder searches through the shell and "
            "`SHELL_TOOLS`/`_SEARCH_BINARIES` should have caught it; a "
            "PowerShell-only or Select-String-only corpus is a known "
            "unparsed channel. Fix the channel, do not read this as a "
            "negative result."
        )

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
