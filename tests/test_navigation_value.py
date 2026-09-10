"""The #114 phase-3 measurement, on fixtures whose verdict is known in advance.

``scripts/navigation_value.py`` decides whether this project builds a symbol
server at all. A measurement nobody can check is worse than no measurement, and
this file is the check: every fixture below is a corpus whose correct verdict is
fixed by construction, so an instrument that reads the data wrongly cannot come
back with a comfortable answer.

Three defects reached this file from real data rather than from reasoning, and
each has a named test below.

``non_code`` is the one that matters most. An early run against this machine's
transcript corpus returned PROCEED at 27.6% addressable read mass — and it was
wrong, because ``.md`` was the largest extension in that corpus by read mass and
no definition/references/hover call answers a question about a README. With
markdown, JSON, logs and lockfiles excluded, that same corpus dropped to 10.4%
raw / 13.7% weighted and the verdict inverted to HALT. One constant flipped the
phase gate, so the regression test for it is the first pair below: fixtures
identical except for the file extension.

**The search channel** is the one that mattered most on the population the
phase is actually about. The script recognised searches only from the ``Grep``
tool, and no_human's coder never emits it — measured on the fleet database,
Grep/Glob/Search are 0 against 115,776 Bash calls, of which 50,459 contain
grep/rg/ag/ack, because ``core/prompt_blocks.py`` tells the coder to "locate the
relevant lines with `grep -n`". So ``symbol_lookup`` was STRUCTURALLY EMPTY on
`--source events` while the report still rendered as though it had two signals,
and the corpus cleared every floor, so the script decided anyway. Two tests pin
the fix and two pin the refusal that now backs it up.

**The headline denominator** had no test at all. Pointing ``share`` at the
navigable class instead of total read mass turned a 12.1% HALT into an 88.2%
PROCEED on the fleet database, and using ``chars`` for both units produced a
319.8% "share", with every other test in this file still green.

Expected counts are written as literals derived by hand from each fixture, not
recomputed from the module under test: a test that asks the code what the
answer should be agrees with the code by construction, including when both are
wrong.

The script is loaded by path rather than imported: it lives in ``scripts/`` and
is not part of the ``no_human`` package.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "navigation_value.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("_nh_navigation_value", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


nv = _load_script()


# --------------------------------------------------------------------------- #
# Fixture builders                                                            #
# --------------------------------------------------------------------------- #

def _events_db(path: Path, events: list[dict]) -> Path:
    """A database shaped like ``migrations/0005_task_events.sql``.

    The real column list, including the AUTOINCREMENT primary key, because the
    reader orders by ``ts, rowid`` to break the tie between a tool call and its
    result written inside one clock tick. A fixture table without that key
    would order by ts alone and the tie-break would go untested.

    Every event here shares one ``ts`` on purpose, so insertion order is the
    only thing that can produce the right sequence.
    """
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id TEXT NOT NULL, ts REAL NOT NULL, data TEXT NOT NULL)"
    )
    for event in events:
        con.execute(
            "INSERT INTO task_events (task_id, ts, data) VALUES (?,?,?)",
            (event.get("task_id", "t1"), event.get("ts", 1000.0),
             event.get("raw", json.dumps(event))),
        )
    con.commit()
    con.close()
    return path


def _use(tool: str, use_id: str, *, task_id: str = "t1",
         source: str = "agent", **inp) -> dict:
    return {"task_id": task_id, "source": source, "kind": "tool_use",
            "tool_name": tool, "tool_use_id": use_id, "tool_input": inp}


def _result(use_id: str, chars: int, *, task_id: str = "t1",
            source: str = "agent", parent: str | None = None) -> dict:
    event = {"task_id": task_id, "source": source, "kind": "tool_result",
             "tool_use_id": use_id, "result_chars": chars}
    if parent is not None:
        event["parent_tool_use_id"] = parent
    return event


def _boundary(kind: str, *, task_id: str = "t1") -> dict:
    return {"task_id": task_id, "source": "orchestrator", "kind": kind}


def _transcript(root: Path, name: str, entries: list[dict | str]) -> Path:
    """One session log under ``<root>/<project>/<session>.jsonl``.

    The reader globs ``*/*.jsonl``, so the project directory level is part of
    the contract and the fixture reproduces it. A plain ``str`` entry is
    written verbatim, which is how the malformed-line cases are built.
    """
    project = root / "proj"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{name}.jsonl"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for entry in entries:
            handle.write(entry if isinstance(entry, str) else json.dumps(entry))
            handle.write("\n")
    return path


def _t_use(tool: str, use_id: str, *, sidechain: bool = False, **inp) -> dict:
    return {"type": "assistant", "isSidechain": sidechain,
            "message": {"content": [
                {"type": "tool_use", "id": use_id, "name": tool, "input": inp}]}}


def _t_result(use_id: str, content, *, sidechain: bool = False) -> dict:
    return {"type": "user", "isSidechain": sidechain,
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": use_id,
                 "content": content}]}}


def _report(corpus, **kwargs) -> dict:
    """``collect`` with the script's own defaults unless a test overrides one."""
    options = {
        "large_read_chars": nv.DEFAULT_LARGE_READ_CHARS,
        "lookback": nv.DEFAULT_LOOKBACK,
        "symbol_response_chars": nv.DEFAULT_SYMBOL_RESPONSE_CHARS,
        "min_reads": nv.DEFAULT_MIN_READS,
        "min_sessions": nv.DEFAULT_MIN_SESSIONS,
    }
    options.update(kwargs)
    return nv.collect(corpus, **options)


def _one_window(*calls) -> "nv.Corpus":
    """A single-window corpus from ``(tool, use_id, input)`` triples."""
    corpus = nv.Corpus(source="unit", sessions=1)
    window = nv.Window()
    for tool, use_id, inp in calls:
        window.calls.append(nv._call_from(tool, inp, use_id, sidechain=False))
    corpus.windows.append(window)
    return corpus


# One session of the shape whose arithmetic every floor-clearing fixture below
# reuses. Ten reads: one whole-file 40,000-char read first, then nine windowed
# 1,000-char reads.
#
#   chars     40,000 + 9 x 1,000                      = 49,000
#   weighted  40,000x9 + 1,000x(8+7+...+0) = 360,000 + 36,000 = 396,000
#   navigable the first read only                     = 40,000 chars
#   addressable 40,000 - 2,000                        = 38,000 chars
#               38,000 x 9                            = 342,000 weighted
#   shares    38,000/49,000 = 77.6%   342,000/396,000 = 86.4%
_BIG = 40_000
_SMALL = 1_000


def _session(root: Path, name: str, ext: str, *, big_last: bool = False,
             with_search: bool = False):
    """One session of the fixed shape above.

    ``with_search`` appends a trailing shell grep. It is OFF by default so the
    hand-computed arithmetic in the comment stays valid (an extra call shifts
    every read's turns_after), and ON for the tests that go through ``main``,
    which refuses a corpus with no search channel at all.
    """
    reads = [(f"{name}-big", {"file_path": f"/r/mod{ext}"}, _BIG)]
    reads += [(f"{name}-s{i}", {"file_path": f"/r/s{i}{ext}", "offset": 1,
                                "limit": 20}, _SMALL) for i in range(9)]
    if big_last:
        reads = reads[1:] + reads[:1]
    entries: list[dict] = []
    for use_id, inp, chars in reads:
        entries.append(_t_use("Read", use_id, **inp))
        entries.append(_t_result(use_id, "x" * chars))
    if with_search:
        entries.append(_t_use("Bash", f"{name}-grep",
                              command="grep -rn parse_config src/"))
    return _transcript(root, name, entries)


def _corpus_of(tmp_path: Path, *, sessions: int, ext: str,
               big_last: bool = False, with_search: bool = False):
    root = tmp_path / "roots"
    for i in range(sessions):
        _session(root, f"s{i}", ext, big_last=big_last,
                 with_search=with_search)
    return nv.read_transcripts(nv._transcript_files((root,)))


# --------------------------------------------------------------------------- #
# The refutation pair — the verdict must turn on WHAT was read                #
# --------------------------------------------------------------------------- #

def test_large_code_reads_clear_the_gate_and_proceed(tmp_path):
    """25 sessions whose read mass is dominated by whole-file .py reads.

    77.6% raw and 86.4% weighted addressable, both far above the 15% gate, so
    the only verdict this data supports is PROCEED.
    """
    report = _report(_corpus_of(tmp_path, sessions=25, ext=".py"))
    measurement = report["measurement"]

    assert report["verdict"]["decision"] == "PROCEED", report["verdict"]
    assert report["sessions"] == 25
    assert measurement["classes"]["all"]["reads"] == 250
    assert measurement["classes"]["navigable"]["reads"] == 25
    assert measurement["classes"]["non_code"]["reads"] == 0
    # The arithmetic in the fixture comment, at 25x.
    assert measurement["classes"]["all"]["chars"] == 25 * 49_000
    assert measurement["classes"]["all"]["weighted"] == 25 * 396_000
    assert measurement["addressable"]["chars"] == 25 * 38_000
    assert measurement["addressable"]["weighted"] == 25 * 342_000


def test_the_same_mass_in_markdown_is_not_addressable_at_all(tmp_path):
    """The regression test for the bug that flipped the real corpus's verdict.

    Byte-for-byte the reads of the PROCEED fixture above, with `.md` in place
    of `.py`. No definition/references/hover call answers a question about a
    README at any size, so navigable must be EMPTY and the verdict HALT — not
    the 77.6% PROCEED the same fixture produces in Python.
    """
    report = _report(_corpus_of(tmp_path, sessions=25, ext=".md"))
    measurement = report["measurement"]

    assert report["verdict"]["decision"] == "HALT", report["verdict"]
    assert measurement["classes"]["all"]["reads"] == 250
    assert measurement["classes"]["all"]["chars"] == 25 * 49_000
    assert measurement["classes"]["navigable"]["reads"] == 0
    assert measurement["classes"]["whole_file"]["reads"] == 0
    assert measurement["classes"]["non_code"]["reads"] == 250
    assert measurement["addressable"] == {"chars": 0, "weighted": 0}
    assert measurement["share_chars"] == 0.0
    assert measurement["share_weighted"] == 0.0


# --------------------------------------------------------------------------- #
# The three verdicts are three different answers                              #
# --------------------------------------------------------------------------- #

def test_units_on_opposite_sides_of_the_gate_do_not_decide(tmp_path):
    """The same session with the big read LAST instead of first.

    A read at the end of its window is re-sent zero more times, so it carries
    77.6% of the raw mass and none of the weighted. Raw says PROCEED, weighted
    says HALT, and a rule that trusted either alone would let the choice of
    unit pick the answer. The honest verdict is that the measurement does not
    decide.
    """
    report = _report(_corpus_of(tmp_path, sessions=25, ext=".py",
                                big_last=True))
    measurement = report["measurement"]

    assert measurement["share_chars"] >= nv.ADDRESSABLE_SHARE_GATE
    assert measurement["share_weighted"] < nv.ADDRESSABLE_SHARE_GATE
    assert report["verdict"]["decision"] == "INCONCLUSIVE"
    assert "disagree" in report["verdict"]["reasons"][0]


def test_a_corpus_under_the_floor_is_inconclusive_not_halt(tmp_path):
    """Two sessions of the PROCEED shape.

    The shares are identical to the PROCEED fixture's — the data has not
    changed shape, only size. "Too little data to say" and "measured, and the
    answer is no" are different answers, and a floor that returned HALT would
    let a quiet corpus close the phase.
    """
    report = _report(_corpus_of(tmp_path, sessions=2, ext=".py"))

    assert report["measurement"]["share_chars"] > nv.ADDRESSABLE_SHARE_GATE
    assert report["verdict"]["decision"] == "INCONCLUSIVE"
    assert "corpus floor not met" in report["verdict"]["reasons"][0]


@pytest.mark.parametrize("reads,sessions", [(250, 19), (199, 25)])
def test_either_floor_alone_is_enough_to_withhold_a_decision(reads, sessions):
    """Both floors are load-bearing, so each is checked with the other met."""
    data = {"classes": {"all": {"reads": reads, "chars": 1, "weighted": 1}},
            "reads_without_result": 0, "share_chars": 0.9,
            "share_weighted": 0.9}
    result = nv.verdict(data, sessions, min_reads=200, min_sessions=20)
    assert result["decision"] == "INCONCLUSIVE"


def test_an_empty_corpus_fails_closed_instead_of_halting(tmp_path):
    """An empty database must not be able to close the phase.

    This is the issue's own non-negotiable — "failures produce no evidence
    rather than false clean verdicts" — applied to the instrument that decides
    the phase. Zero reads is not a negative result, so `main` exits non-zero
    with no verdict at all rather than printing a comfortable 0% HALT.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit) as excinfo:
        nv.main(["--source", "transcripts", "--transcript-root", str(empty)])
    assert "FAIL (empty input set)" in str(excinfo.value)
    assert "HALT" not in str(excinfo.value)


def test_reads_with_no_recorded_result_do_not_shrink_the_denominator(tmp_path):
    """An unmeasured read and a zero-length read are different facts."""
    corpus = _one_window(
        ("Read", "a", {"file_path": "/r/mod.py"}),   # no result recorded
        ("Read", "b", {"file_path": "/r/other.py"}),
    )
    corpus.sizes["b"] = (30_000, False)
    report = _report(corpus, min_reads=0, min_sessions=0)
    measurement = report["measurement"]

    assert measurement["reads_without_result"] == 1
    assert measurement["classes"]["all"]["reads"] == 2
    # The unsized read contributes no mass, and is not navigable: a size it
    # never had cannot clear the large-read threshold.
    assert measurement["classes"]["all"]["chars"] == 30_000
    assert measurement["classes"]["navigable"]["reads"] == 1


# --------------------------------------------------------------------------- #
# The coder's real search channel: a grep inside a shell command              #
# --------------------------------------------------------------------------- #

# Every row is (command, is_a_search, extracted patterns). The pairs that
# matter are the ones a single shared option table gets wrong:
#   * `grep -r PATTERN .` vs `rg -r NEW PATTERN` -- `-r` takes no value in grep
#     and IS `--replace` in ripgrep, so one table either eats grep's pattern or
#     misreads ripgrep's.
#   * `grep --color PATTERN` vs `--color=auto` -- an OPTIONAL-argument flag, so
#     listing it as value-taking would eat the pattern in the commoner shape.
#   * `grep -f patterns.txt` -- a real search whose pattern this script cannot
#     see, which must count as a search and NOT as a symbol one.
_SHELL_SEARCHES = [
    ("grep -n 'def parse_config' src/", True, ("def parse_config",)),
    ("grep -rn parse_config .", True, ("parse_config",)),
    ("grep -r parse_config .", True, ("parse_config",)),
    ("rg -r NEW parse_config", True, ("parse_config",)),
    ("rg -l parse_config", True, ("parse_config",)),
    ("rg -g '*.py' parse_config", True, ("parse_config",)),
    ("rg -e 'class Foo' -t py", True, ("class Foo",)),
    ("grep -A3 -B3 parse_config file.py", True, ("parse_config",)),
    ("grep -m 5 parse_config file", True, ("parse_config",)),
    ("grep -rn --include=*.py parse_config", True, ("parse_config",)),
    ("grep -i -e foo -e bar file", True, ("foo",)),
    ("grep --color parse_config file", True, ("parse_config",)),
    ("grep --color=auto parse_config file", True, ("parse_config",)),
    ("grep -- -weird file", True, ("-weird",)),
    ("/usr/bin/grep -rn parse_config .", True, ("parse_config",)),
    ("sudo grep -rn parse_config /etc", True, ("parse_config",)),
    ("FOO=1 grep -rn parse_config .", True, ("parse_config",)),
    ("cat x | grep parse_config", True, ("parse_config",)),
    ("ls -la && grep -rn TODO .", True, ("TODO",)),
    ("grep -rn 'def foo' a | grep bar", True, ("def foo", "bar")),
    ("git grep -n parse_config", True, ("parse_config",)),
    ("grep -f patterns.txt src/", True, ()),
    ("git log --oneline", False, ()),
    ("pytest -q", False, ()),
    ("Select-String -Pattern parse_config", False, ()),
    ("", False, ()),
]


@pytest.mark.parametrize("command,is_search,patterns", _SHELL_SEARCHES)
def test_the_shell_search_extractor_reads_the_command_the_coder_wrote(
        command, is_search, patterns):
    assert nv.shell_search(command) == (is_search, patterns)


def test_a_nested_quote_is_parsed_rather_than_fumbled():
    """`grep -rn "unbalanced 'quote" .` is BALANCED -- the `'` sits inside the
    `"` pair -- so `shlex` handles it and the pattern is one token.

    Here as a guard against the fallback being reached too eagerly: the
    cheap-looking test for "does this command have matched quotes" is a quote
    count, and a quote count is wrong on exactly this shape.
    """
    assert nv.shell_search("grep -rn \"unbalanced 'quote\" .") == (
        True, ("unbalanced 'quote",))


@pytest.mark.parametrize("command", [
    'grep -rn "def foo .',            # one unmatched double quote
    "grep -rn 'parse_config",         # one unmatched single quote
])
def test_a_genuinely_unbalanced_command_is_a_search_with_no_pattern(command):
    """Both halves matter, and they pull opposite ways.

    Dropping the segment would shrink the search census silently, and the
    census decides whether this script may return a verdict at all -- so the
    fallback is a plain split. But a split is not a parse: whitespace-splitting
    makes the pattern operand a fragment (`"def`), which is not the thing
    searched for. So it is a SEARCH carrying NO pattern.
    """
    assert nv.shell_search(command) == (True, ())


def test_a_fabricated_fragment_cannot_become_a_symbol_lookup():
    """The case where the guard changes the SYMBOL verdict, not just a count.

    `grep -rn parse_config " .` is unbalanced, so the fallback yields
    `parse_config` as its operand -- identifier-shaped, and therefore a
    symbol lookup -- off a command that was never parsed. Rejecting the
    pattern from an untokenizable segment is what stops a parse failure
    manufacturing evidence in the class the verdict leans on hardest.
    """
    assert nv.shell_search('grep -rn parse_config " .') == (True, ())

    corpus = _one_window(
        ("Bash", "g", {"command": 'grep -rn parse_config " .'}),
        ("Read", "r", {"file_path": "/r/mod.py"}),
    )
    corpus.sizes["r"] = (500, False)
    report = _report(corpus, min_reads=0, min_sessions=0)
    assert report["measurement"]["classes"]["symbol_lookup"]["reads"] == 0
    # A search, and honestly reported as one this script could not read.
    assert report["search_census"] == {"searches": 1,
                                       "searches_with_pattern": 0,
                                       "symbol_searches": 0}


def test_cleanliness_is_judged_per_segment_not_per_command():
    """One bad segment must not discard a sibling that parsed fine.

    `grep -rn "def foo bar" . | grep "x` has a clean first segment and an
    unbalanced second. The whole command is unbalanced, so a command-level
    check would throw away the pattern that was right there.
    """
    assert nv.shell_search('grep -rn "def foo bar" . | grep "x') == (
        True, ("def foo bar",))


def test_a_shell_grep_before_a_read_is_a_symbol_lookup():
    """The whole point of the fix.

    no_human's coder emits no Grep tool at all -- measured on the fleet
    database, Grep/Glob/Search are 0 against 115,776 Bash calls, of which
    50,459 contain grep/rg/ag/ack -- because `core/prompt_blocks.py` tells it
    to "locate the relevant lines with `grep -n`". Reading only the Grep TOOL
    made this class structurally unreachable on the product's own telemetry.
    """
    corpus = _one_window(
        ("Bash", "g", {"command": "grep -rn 'def parse_config' src/"}),
        ("Read", "r", {"file_path": "/r/mod.py"}),
    )
    corpus.sizes["r"] = (500, False)
    report = _report(corpus, min_reads=0, min_sessions=0)

    assert report["measurement"]["classes"]["symbol_lookup"]["reads"] == 1
    assert report["search_census"] == {"searches": 1,
                                       "searches_with_pattern": 1,
                                       "symbol_searches": 1}


def test_a_shell_grep_for_prose_is_a_search_but_not_a_symbol_one():
    """The census separates "searched" from "searched for a symbol"."""
    corpus = _one_window(
        ("Bash", "g", {"command": "grep -rn 'TODO before the release' ."}),
        ("Read", "r", {"file_path": "/r/mod.py"}),
    )
    corpus.sizes["r"] = (500, False)
    report = _report(corpus, min_reads=0, min_sessions=0)

    assert report["measurement"]["classes"]["symbol_lookup"]["reads"] == 0
    assert report["search_census"] == {"searches": 1,
                                       "searches_with_pattern": 1,
                                       "symbol_searches": 0}


def test_a_search_whose_pattern_cannot_be_read_is_still_a_search():
    """`grep -f patterns.txt` searches; the patterns are in a file.

    Counting it as "not a search" would let a gap in this script's option
    tables render as "this corpus does not search", which is the false
    negative the refusal below exists to prevent.
    """
    corpus = _one_window(
        ("Bash", "g", {"command": "grep -f patterns.txt src/"}),
        ("Read", "r", {"file_path": "/r/mod.py"}),
    )
    corpus.sizes["r"] = (500, False)
    census = _report(corpus, min_reads=0, min_sessions=0)["search_census"]
    assert census == {"searches": 1, "searches_with_pattern": 0,
                      "symbol_searches": 0}


def test_a_non_search_shell_command_is_not_counted(tmp_path):
    corpus = _one_window(
        ("Bash", "b", {"command": "pytest -q && git log --oneline"}),
        ("Read", "r", {"file_path": "/r/mod.py"}),
    )
    corpus.sizes["r"] = (500, False)
    assert _report(corpus, min_reads=0,
                   min_sessions=0)["search_census"]["searches"] == 0


def test_the_events_reader_finds_searches_in_bash_commands(tmp_path):
    """End to end on the product's own event shape, which is what regressed.

    `task_events` records the Bash command verbatim in `tool_input`, so the
    search channel was always in the data -- the instrument just was not
    reading it.
    """
    db = _events_db(tmp_path / "bashsearch.db", [
        _use("Bash", "g", command="grep -rn 'def parse_config' src/"),
        _result("g", 400),
        _use("Read", "r", file_path="/r/mod.py"),
        _result("r", 900),
    ])
    con = nv._connect(db)
    try:
        corpus = nv.read_events(con)
    finally:
        con.close()
    report = _report(corpus, min_reads=0, min_sessions=0)

    assert report["search_census"]["symbol_searches"] == 1
    assert report["measurement"]["classes"]["symbol_lookup"]["reads"] == 1


def test_a_corpus_with_reads_and_no_searches_is_refused_not_decided(tmp_path):
    """The defect this fix exists for, pinned end to end.

    Reads present, both floors cleared, and NO search channel -- so
    symbol_lookup can only be 0 and the verdict would rest entirely on the
    large-read heuristic while rendering as though it had two signals. That is
    exactly what the first version of this script did on the fleet database.
    An absent channel is a gap in the source or the parser, never evidence
    about the agent, so it gets no verdict at all.
    """
    root = tmp_path / "roots"
    for i in range(25):
        _session(root, f"s{i}", ".py")          # no with_search
    with pytest.raises(nv.NoSearchChannel) as excinfo:
        nv.main(["--source", "transcripts", "--transcript-root", str(root)])

    message = str(excinfo.value)
    assert "FAIL (no search channel)" in message
    # It must not be mistakable for the empty-corpus refusal, and it must not
    # leak the verdict it declined to give.
    assert "empty input set" not in message
    for decision in ("HALT", "PROCEED", "INCONCLUSIVE"):
        assert decision not in message


def test_the_no_search_refusal_is_distinct_from_the_empty_one(tmp_path):
    """Two different failures must not share an exception class.

    A caller that catches one and not the other has to be able to tell "no
    data" from "no search channel"; they call for different fixes.
    """
    assert not issubclass(nv.NoSearchChannel, nv.EmptyInputSet)
    assert not issubclass(nv.EmptyInputSet, nv.NoSearchChannel)
    assert issubclass(nv.NoSearchChannel, SystemExit)


@pytest.mark.parametrize("command,expected", [
    # No search calls at all: a gap in the source or the instrument.
    (None, "recorded NO search calls at all"),
    # Searched, but no pattern came out: a gap in the option tables.
    ("grep -f patterns.txt src/", "a pattern could be extracted from none"),
    # Searched, patterns read, all text: the only one that is a fact about
    # the agent.
    ("grep -rn 'TODO before the release' .", "none was symbol-shaped"),
])
def test_a_zero_symbol_lookup_says_which_of_its_causes_applies(
        command, expected):
    """Three different facts that all render `symbol_lookup 0`.

    The first version printed the same NOTE for all three -- "a symbol
    question answered from memory leaves no search behind" -- which on the
    fleet database explained the zero exactly backwards: the real reason was
    that the source recorded no search calls the instrument could read. A
    reviewer cannot tell those apart from an identical line.
    """
    calls = [("Read", "r", {"file_path": "/r/mod.py"})]
    if command is not None:
        calls.insert(0, ("Bash", "g", {"command": command}))
    corpus = _one_window(*calls)
    corpus.sizes["r"] = (500, False)
    rendered = nv.render(_report(corpus, min_reads=0, min_sessions=0))

    assert rendered.count("NOTE:") == 1
    assert expected in rendered


def test_the_fourth_cause_of_a_zero_is_the_lookback_not_the_channel():
    """A symbol-shaped search that no read followed inside the window."""
    corpus = _one_window(
        ("Bash", "g", {"command": "grep -rn parse_config src/"}),
        ("Bash", "b1", {"command": "pytest -q"}),
        ("Bash", "b2", {"command": "pytest -q"}),
        ("Bash", "b3", {"command": "pytest -q"}),
        ("Read", "r", {"file_path": "/r/mod.py"}),
    )
    corpus.sizes["r"] = (500, False)
    rendered = nv.render(_report(corpus, min_reads=0, min_sessions=0))

    assert "symbol-shaped search(es)" in rendered
    assert "no read followed one within the 3-call lookback" in rendered


def test_the_search_census_is_printed_even_when_it_is_healthy(tmp_path):
    """"0 searches seen" and "0 of N were symbol-shaped" must not render the
    same, which means the census has to be on the page unconditionally."""
    rendered = nv.render(_report(
        _corpus_of(tmp_path, sessions=25, ext=".py", with_search=True)))
    assert "search channel: 25 search call(s), 25 with an extractable "\
           "pattern, 25 symbol-shaped" in rendered


# --------------------------------------------------------------------------- #
# Is this Grep pattern a symbol query?                                        #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("pattern", [
    "parse_config",
    "def parse_config",
    r"def\s+parse_config",
    r"\bparse_config\b",
    "^class TypeEvidence",
    "function renderBoard",
    "export default function AppShell",
    "interface ReviewFinding",
    "async def resume_task",
    "parse_config(",
    r"def parse_config\(self",
    # A declaration keyword is what licenses the `:`/`=` tail split, so an
    # annotated declaration still resolves to its name.
    "const parseConfig: string",
    "let parseConfig = 1",
])
def test_symbol_shaped_patterns_are_recognised(pattern):
    assert nv.is_symbol_query(pattern) is True


@pytest.mark.parametrize("pattern", [
    "",
    "parse_config|render_board",                        # alternation
    "parse_[cC]onfig",                                  # character class
    "id",                                               # two chars
    "**/*.py",                                          # a file glob
    "the quick brown fox",                              # multi-token
    "cannot merge",                                     # two words
    "x" * 81,                                           # over the length bound
    "1234",                                             # not an identifier
    "-n 4",
    # Prose carrying a colon. This one was CLASSIFIED AS A SYMBOL LOOKUP until
    # the token bound landed: the tail split kept only `TODO`, so any sentence
    # with a colon in it filed itself into the strongest class.
    "TODO: fix the reviewer before the next release",
    # A Java/C# signature, which this rule declines rather than parses — four
    # tokens, and the return type is not a keyword the prefix can anchor on.
    # Under-counting the strongest class biases the verdict toward HALT, which
    # is the safe direction here.
    "public static void main",
    # ---- Measured false positives. Every row below was CLASSIFIED AS A -----
    # ---- SYMBOL LOOKUP until the two rules in `is_symbol_query`'s      -----
    # ---- docstring landed, found by reading what it accepted across    -----
    # ---- 1,470 real search patterns rather than by reasoning about it. -----
    #
    # A search for a quoted string literal - a JSON key, not a definition.
    "'parse_config'",
    '"version"',
    '"typescript"',
    # Fragments left by the unbalanced-quote fallback in `_segment_argv`.
    # A symbol's NAME never carries a quote, which is what disqualifies them.
    '"Test',
    '"^export',
    "from '",
    "'\"action",
    # A key or attribute VALUE, which no definition call answers. These
    # survived because the `=`/`:` tail split kept their left-hand side even
    # with no declaration keyword in sight.
    'kind="tool_result"',
    "risk:",
    "_ON = ",
])
def test_text_searches_are_not_symbol_queries(pattern):
    assert nv.is_symbol_query(pattern) is False


def test_a_read_after_a_prose_search_is_not_a_symbol_lookup():
    corpus = _one_window(
        ("Grep", "g", {"pattern": "TODO before the next release"}),
        ("Read", "r", {"file_path": "/r/mod.py"}),
    )
    corpus.sizes["r"] = (500, False)
    classes = _report(corpus, min_reads=0,
                      min_sessions=0)["measurement"]["classes"]
    assert classes["symbol_lookup"]["reads"] == 0


def test_a_glob_is_a_file_search_not_a_definition_lookup():
    """`Glob` is excluded by tool, not only by pattern shape.

    A glob for a bare directory name (`components`) is identifier-shaped, so
    pattern matching alone would let path globbing count as the strongest
    class. The tool set is what stops it.
    """
    corpus = _one_window(
        ("Glob", "g", {"pattern": "components"}),
        ("Read", "r", {"file_path": "/r/mod.py"}),
    )
    corpus.sizes["r"] = (500, False)
    classes = _report(corpus, min_reads=0,
                      min_sessions=0)["measurement"]["classes"]
    assert classes["symbol_lookup"]["reads"] == 0


@pytest.mark.parametrize("gap,expected", [(0, 1), (2, 1), (3, 0), (6, 0)])
def test_the_lookback_window_is_bounded_at_its_stated_size(gap, expected):
    """A read three calls past the search is a different act.

    ``gap`` is how many unrelated calls sit between the Grep and the Read; at
    the default lookback of 3 the Read sees the Grep while the gap is under 3.
    Without the bound this class would mean "this session ever grepped".
    """
    calls = [("Grep", "g", {"pattern": "parse_config"})]
    calls += [("Bash", f"b{i}", {"command": "ls"}) for i in range(gap)]
    calls.append(("Read", "r", {"file_path": "/r/mod.py"}))
    corpus = _one_window(*calls)
    corpus.sizes["r"] = (500, False)
    classes = _report(corpus, min_reads=0,
                      min_sessions=0)["measurement"]["classes"]
    assert classes["symbol_lookup"]["reads"] == expected


def test_a_symbol_lookup_still_needs_a_language_a_symbol_server_serves():
    corpus = _one_window(
        ("Grep", "g", {"pattern": "def parse_config"}),
        ("Read", "r", {"file_path": "/r/CHANGELOG.md"}),
    )
    corpus.sizes["r"] = (500, False)
    classes = _report(corpus, min_reads=0,
                      min_sessions=0)["measurement"]["classes"]
    assert classes["symbol_lookup"]["reads"] == 0
    assert classes["non_code"]["reads"] == 1


# --------------------------------------------------------------------------- #
# whole_file, repeat, overlap, extensions                                     #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("inp,expected", [
    ({"file_path": "/r/mod.py"}, 1),                              # whole file
    ({"file_path": "/r/mod.py", "offset": 400}, 0),               # a window
    ({"file_path": "/r/mod.py", "limit": 200}, 0),                # a window
    ({"file_path": "/r/mod.py", "offset": 0, "limit": 200}, 0),   # offset 0 IS
    ({"file_path": "/r/mod.py", "limit": 0}, 0),                  # limit 0 too
])
def test_only_an_unwindowed_read_can_be_a_whole_file_read(inp, expected):
    """`offset=0` is a window and `limit=0` is a request for one.

    Both are falsy, so a truthiness test would call them whole-file reads. The
    reading here matches `orchestrator._summarize_tool_sig`, which distinguishes
    a re-read of one window from a scan across nine.
    """
    corpus = _one_window(("Read", "r", inp))
    corpus.sizes["r"] = (30_000, False)
    classes = _report(corpus, min_reads=0,
                      min_sessions=0)["measurement"]["classes"]
    assert classes["whole_file"]["reads"] == expected


def test_the_large_read_threshold_is_inclusive_at_its_boundary():
    for chars, expected in ((11_999, 0), (12_000, 1)):
        corpus = _one_window(("Read", "r", {"file_path": "/r/mod.py"}))
        corpus.sizes["r"] = (chars, False)
        classes = _report(corpus, min_reads=0,
                          min_sessions=0)["measurement"]["classes"]
        assert classes["whole_file"]["reads"] == expected, chars


def test_the_union_is_reported_with_its_overlap_and_is_not_a_sum():
    """One read in both classes, one in each. navigable is 3, not 4.

    The three Bash calls are load-bearing, not padding: they push the
    large-only read outside the lookback window of the FIRST Grep. Without
    them that read is also a symbol lookup — which is correct behaviour and
    was how the first version of this fixture accidentally tested nothing.
    """
    corpus = _one_window(
        ("Grep", "g1", {"pattern": "def parse_config"}),
        ("Read", "both", {"file_path": "/r/a.py"}),        # symbol + large
        ("Bash", "x1", {"command": "pytest"}),
        ("Bash", "x2", {"command": "pytest"}),
        ("Bash", "x3", {"command": "pytest"}),
        ("Read", "large", {"file_path": "/r/b.py"}),       # large only
        ("Grep", "g2", {"pattern": "render_board"}),
        ("Read", "symbol", {"file_path": "/r/c.py"}),      # symbol only
    )
    corpus.sizes.update({"both": (30_000, False), "large": (30_000, False),
                         "symbol": (500, False)})
    classes = _report(corpus, min_reads=0,
                      min_sessions=0)["measurement"]["classes"]

    assert classes["symbol_lookup"]["reads"] == 2
    assert classes["whole_file"]["reads"] == 2
    assert classes["both"]["reads"] == 1
    assert classes["navigable"]["reads"] == 3
    assert (classes["navigable"]["reads"]
            == classes["symbol_lookup"]["reads"]
            + classes["whole_file"]["reads"] - classes["both"]["reads"])


def test_a_repeat_read_is_orthogonal_and_changes_no_other_class():
    """Two whole-file reads of one path: both navigable, the second a repeat.

    ``repeat`` overlaps the addressable classes rather than competing with
    them. The docstring says the row neither adds to nor removes from
    ``navigable``, and this is what pins that claim to the code.
    """
    corpus = _one_window(
        ("Read", "a", {"file_path": "/r/mod.py"}),
        ("Read", "b", {"file_path": "/R/MOD.PY"}),   # same file, other casing
    )
    corpus.sizes.update({"a": (30_000, False), "b": (30_000, False)})
    classes = _report(corpus, min_reads=0,
                      min_sessions=0)["measurement"]["classes"]

    assert classes["repeat"]["reads"] == 1
    assert classes["navigable"]["reads"] == 2


def test_a_repeat_is_counted_per_context_window_not_per_corpus(tmp_path):
    """The same file read once in each of two windows is not a repeat.

    A compaction throws the first read away, so reading it again afterwards is
    a fresh read, not a redundant one.
    """
    root = tmp_path / "roots"
    _transcript(root, "s0", [
        _t_use("Read", "a", file_path="/r/mod.py"),
        _t_result("a", "x" * 500),
        {"type": "system", "isCompactSummary": True},
        _t_use("Read", "b", file_path="/r/mod.py"),
        _t_result("b", "x" * 500),
    ])
    report = _report(nv.read_transcripts(nv._transcript_files((root,))),
                     min_reads=0, min_sessions=0)

    assert report["windows"] == 2
    assert report["measurement"]["classes"]["repeat"]["reads"] == 0


@pytest.mark.parametrize("path,ext", [
    ("/r/mod.py", ".py"),
    ("/r/Mod.PY", ".py"),
    (r"C:\r\mod.py", ".py"),
    ("/r/app.tsx", ".tsx"),
    ("/r/README.md", ".md"),
    ("/r/Makefile", "(none)"),
    ("/r/.gitignore", "(none)"),
    ("/r/.venv/pyvenv.cfg", ".cfg"),
    ("/r/.github/CODEOWNERS", "(none)"),
    ("", "(none)"),
])
def test_the_extension_comes_off_the_basename(path, ext):
    """A dotted DIRECTORY must not be reported as a file's language.

    ``/r/.github/CODEOWNERS`` has a dot in its path and none in its name.
    Taking the suffix off the whole path would report that file as `.github`,
    inventing a language for it — and `.gitignore` is a name, not an extension.
    """
    assert nv._extension(path) == ext


def test_an_extensionless_file_is_non_code():
    corpus = _one_window(("Read", "r", {"file_path": "/r/Makefile"}))
    corpus.sizes["r"] = (30_000, False)
    classes = _report(corpus, min_reads=0,
                      min_sessions=0)["measurement"]["classes"]
    assert classes["non_code"]["reads"] == 1
    assert classes["navigable"]["reads"] == 0


def test_the_net_saving_never_goes_negative():
    """A navigable read smaller than the symbol answer that would replace it.

    Clamped at zero rather than allowed to subtract: a fix that costs more
    than the read it replaces saves nothing, and letting it net out negative
    would credit it with removing mass from OTHER reads.
    """
    corpus = _one_window(
        ("Grep", "g", {"pattern": "parse_config"}),
        ("Read", "r", {"file_path": "/r/mod.py"}),
    )
    corpus.sizes["r"] = (100, False)
    measurement = _report(corpus, min_reads=0,
                          min_sessions=0)["measurement"]

    assert measurement["classes"]["navigable"]["reads"] == 1
    assert measurement["addressable"] == {"chars": 0, "weighted": 0}


def test_raising_the_symbol_answer_estimate_can_only_shrink_the_saving(tmp_path):
    """Monotonicity, on one corpus, across the estimate's whole range.

    The estimate is the parameter a reader is most likely to disagree with, so
    the direction it moves the answer must be a property and not a coincidence
    of one value.
    """
    corpus = _corpus_of(tmp_path, sessions=25, ext=".py")
    shares = [_report(corpus, symbol_response_chars=n)["measurement"]
              ["share_chars"] for n in (0, 1_000, 2_000, 10_000, 50_000)]
    assert shares == sorted(shares, reverse=True)
    assert shares[-1] == 0.0   # an estimate above every read saves nothing


# --------------------------------------------------------------------------- #
# The headline denominator                                                    #
# --------------------------------------------------------------------------- #

def _denominator_corpus():
    """One window whose every figure is hand-derivable, with real
    non-navigable mass in it so the denominator choice actually shows.

      idx 0  .py  40,000 chars, unwindowed, turns_after 2  -> navigable
      idx 1  .md  10,000 chars, unwindowed, turns_after 1  -> non_code
      idx 2  Bash (not a read, but it is a turn)

      all         chars 40,000 + 10,000              =  50,000
                  weighted 40,000x2 + 10,000x1       =  90,000
      navigable   chars 40,000, weighted 80,000
      addressable 40,000 - 2,000                     =  38,000 chars
                  38,000 x 2                         =  76,000 weighted
      share_chars     38,000 /  50,000 = 0.76
      share_weighted  76,000 /  90,000 = 0.8444...
    """
    corpus = _one_window(
        ("Read", "code", {"file_path": "/r/mod.py"}),
        ("Read", "doc", {"file_path": "/r/README.md"}),
        ("Bash", "b", {"command": "pytest -q"}),
    )
    corpus.sizes.update({"code": (40_000, False), "doc": (10_000, False)})
    return corpus


def test_the_share_denominator_is_total_read_mass():
    """`total = classes["all"][unit]` had no test at all.

    Pointing it at ``classes["navigable"]`` turns this fixture's 76.0% into
    95.0% -- and on the fleet database turned a 12.1% HALT into an 88.2%
    PROCEED -- while every other assertion in this file stays green, because
    nothing else reads the share. Literals below are hand-derived in
    `_denominator_corpus`, not recomputed from the module.
    """
    measurement = _report(_denominator_corpus(), min_reads=0,
                          min_sessions=0)["measurement"]

    assert measurement["classes"]["all"]["chars"] == 50_000
    assert measurement["classes"]["all"]["weighted"] == 90_000
    assert measurement["classes"]["navigable"]["chars"] == 40_000
    assert measurement["classes"]["navigable"]["weighted"] == 80_000
    assert measurement["addressable"] == {"chars": 38_000, "weighted": 76_000}

    assert measurement["share_chars"] == pytest.approx(0.76)
    assert measurement["share_weighted"] == pytest.approx(76_000 / 90_000)
    # Named explicitly, because this is the mutant: the navigable class as the
    # denominator reads as a far larger opportunity than the corpus holds.
    assert measurement["share_chars"] != pytest.approx(38_000 / 40_000)
    assert measurement["share_weighted"] != pytest.approx(76_000 / 80_000)


def test_each_unit_divides_by_its_own_total():
    """The second mutant: `chars` as the denominator for BOTH units.

    That is not a small error -- weighted mass is larger than raw mass by a
    factor of the window length, so the "share" it produces exceeded 100% on
    the fleet database (319.8%). A share above 1.0 is arithmetically
    impossible for a subset of the mass it divides, so it is also asserted
    directly.
    """
    measurement = _report(_denominator_corpus(), min_reads=0,
                          min_sessions=0)["measurement"]

    assert measurement["share_weighted"] != pytest.approx(76_000 / 50_000)
    assert measurement["share_chars"] != pytest.approx(38_000 / 90_000)
    for unit in ("share_chars", "share_weighted"):
        assert 0.0 <= measurement[unit] <= 1.0, unit


def test_the_share_cannot_exceed_one_on_the_floor_clearing_fixtures(tmp_path):
    """A property, over the fixtures whose verdicts this file already pins.

    Addressable mass is a subset of read mass by construction (it is a
    per-read clamp of a per-read quantity), so any share over 1.0 is a
    denominator bug rather than a finding.
    """
    for ext in (".py", ".md"):
        for big_last in (False, True):
            measurement = _report(_corpus_of(
                tmp_path / f"{ext}{big_last}", sessions=25, ext=ext,
                big_last=big_last))["measurement"]
            assert 0.0 <= measurement["share_chars"] <= 1.0
            assert 0.0 <= measurement["share_weighted"] <= 1.0


def test_a_zero_denominator_does_not_raise():
    """Every read sized zero: the share is 0.0, not a ZeroDivisionError."""
    corpus = _one_window(("Read", "r", {"file_path": "/r/mod.py"}))
    corpus.sizes["r"] = (0, False)
    measurement = _report(corpus, min_reads=0, min_sessions=0)["measurement"]
    assert measurement["share_chars"] == 0.0
    assert measurement["share_weighted"] == 0.0


# --------------------------------------------------------------------------- #
# Is the verdict a property of the data, or of one constant?                   #
# --------------------------------------------------------------------------- #

def test_a_verdict_that_flips_at_half_the_threshold_says_so(tmp_path):
    """The real corpus needed this, which is why it exists.

    25 sessions whose big read is 8,000 chars: below the shipped 12,000
    threshold (so HALT) and above half of it (so PROCEED at 6,000). Same data,
    one constant, opposite decisions about whether this project builds a symbol
    server — and the verdict has to disclose that rather than present the
    pre-registered answer as if the data had settled it.
    """
    root = tmp_path / "roots"
    for i in range(25):
        entries: list[dict] = []
        for use_id, inp, chars in (
            [(f"s{i}-big", {"file_path": "/r/mod.py"}, 8_000)]
            + [(f"s{i}-{j}", {"file_path": f"/r/s{j}.py", "offset": 1,
                              "limit": 9}, 100) for j in range(9)]
        ):
            entries.append(_t_use("Read", use_id, **inp))
            entries.append(_t_result(use_id, "x" * chars))
        _transcript(root, f"s{i}", entries)
    report = _report(nv.read_transcripts(nv._transcript_files((root,))))

    assert report["verdict"]["decision"] == "HALT"
    assert report["robustness"]["fragile"] is True
    probed = report["robustness"]["probed"]
    assert {t: p["decision"] for t, p in probed.items()} == {
        "6000": "PROCEED", "24000": "HALT"}
    # The qualifying count rides with every probe, so a probe that agreed only
    # because almost nothing cleared its threshold is visible as such.
    assert probed["6000"]["navigable_reads"] == 25
    assert probed["24000"]["navigable_reads"] == 0
    caution = nv.render(report)
    assert "CAUTION: this verdict is not robust" in caution
    assert "6,000 chars gives PROCEED" in caution


def test_a_verdict_that_holds_at_both_probes_says_that_too(tmp_path):
    """The markdown corpus: nothing is addressable at any threshold.

    The reassuring case has to be reported as explicitly as the fragile one,
    or a reader cannot tell "checked and robust" from "not checked".
    """
    report = _report(_corpus_of(tmp_path, sessions=25, ext=".md"))

    assert report["verdict"]["decision"] == "HALT"
    assert report["robustness"]["fragile"] is False
    probed = report["robustness"]["probed"]
    assert {p["decision"] for p in probed.values()} == {"HALT"}
    assert all(p["navigable_reads"] == 0 for p in probed.values())
    rendered = nv.render(report)
    assert "robustness: the same decision at" in rendered
    assert "CAUTION" not in rendered


def test_the_robustness_probe_does_not_move_the_decision_it_probes(tmp_path):
    """The probe re-classifies the corpus twice; the reported verdict must
    still be the one the pre-registered threshold gives."""
    corpus = _corpus_of(tmp_path, sessions=25, ext=".py")
    report = _report(corpus)
    bare = nv.verdict(
        nv.aggregate(nv.classify(corpus, large_read_chars=12_000, lookback=3),
                     symbol_response_chars=2_000),
        25, min_reads=200, min_sessions=20)
    assert report["verdict"]["decision"] == bare["decision"] == "PROCEED"


# --------------------------------------------------------------------------- #
# Weighting                                                                   #
# --------------------------------------------------------------------------- #

def test_every_tool_call_re_sends_the_conversation_not_only_reads():
    """A Bash turn after a read re-sends that read exactly like a Read turn.

    Counting only reads toward the remaining turns would understate a read
    followed by a long test-and-fix stretch — which is the ordinary shape of
    an attempt.
    """
    corpus = _one_window(
        ("Read", "r", {"file_path": "/r/mod.py"}),
        ("Bash", "b1", {"command": "pytest"}),
        ("Bash", "b2", {"command": "pytest"}),
    )
    corpus.sizes["r"] = (1_000, False)
    classes = _report(corpus, min_reads=0,
                      min_sessions=0)["measurement"]["classes"]
    assert classes["all"]["weighted"] == 2_000


def test_nothing_is_weighted_across_an_attempt_boundary(tmp_path):
    """An attempt starts a fresh session, so the first attempt's reads are not
    re-sent by the second attempt's turns."""
    db = _events_db(tmp_path / "boundary.db", [
        _boundary("attempt_start"),
        _use("Read", "a", file_path="/r/mod.py"),
        _result("a", 1_000),
        _boundary("attempt_start"),
        _use("Bash", "b", command="pytest"),
        _use("Bash", "c", command="pytest"),
    ])
    con = nv._connect(db)
    try:
        corpus = nv.read_events(con)
    finally:
        con.close()
    report = _report(corpus, min_reads=0, min_sessions=0)

    assert report["windows"] == 2
    # The read is last in its own window; the two Bash calls are in the next.
    assert report["measurement"]["classes"]["all"]["weighted"] == 0


def test_a_compaction_closes_the_window_it_truncates(tmp_path):
    db = _events_db(tmp_path / "compact.db", [
        _use("Read", "a", file_path="/r/mod.py"),
        _result("a", 1_000),
        _boundary("compaction"),
        _use("Bash", "b", command="pytest"),
    ])
    con = nv._connect(db)
    try:
        corpus = nv.read_events(con)
    finally:
        con.close()
    report = _report(corpus, min_reads=0, min_sessions=0)

    assert report["windows"] == 2
    assert report["measurement"]["classes"]["all"]["weighted"] == 0


def test_a_subagent_read_carries_raw_chars_and_no_weighted_mass(tmp_path):
    """Both halves of the exclusion, and the row that makes it visible.

    A subagent's result is re-read in the subagent's own context, whose
    remaining turns neither source records — so a weight taken from the MAIN
    window's remaining turns would be a number about the wrong conversation.
    """
    db = _events_db(tmp_path / "sub.db", [
        _use("Read", "a", file_path="/r/mod.py"),
        _result("a", 30_000, parent="toolu_parent"),
        _use("Bash", "b", command="pytest"),
    ])
    con = nv._connect(db)
    try:
        corpus = nv.read_events(con)
    finally:
        con.close()
    classes = _report(corpus, min_reads=0,
                      min_sessions=0)["measurement"]["classes"]

    assert classes["sidechain"]["reads"] == 1
    assert classes["sidechain"]["chars"] == 30_000
    assert classes["all"]["weighted"] == 0
    assert classes["navigable"]["reads"] == 1   # still addressable, just unweighted


def test_a_transcript_marks_its_subagent_reads_on_the_call(tmp_path):
    """The other half of the same fact: transcripts flag the CALL, and
    `task_events` flags only the RESULT, so the classifier has to accept
    either end."""
    root = tmp_path / "roots"
    _transcript(root, "s0", [
        _t_use("Read", "a", sidechain=True, file_path="/r/mod.py"),
        _t_result("a", "x" * 30_000, sidechain=True),
    ])
    classes = _report(nv.read_transcripts(nv._transcript_files((root,))),
                      min_reads=0, min_sessions=0)["measurement"]["classes"]
    assert classes["sidechain"]["reads"] == 1
    assert classes["all"]["weighted"] == 0


# --------------------------------------------------------------------------- #
# The events reader                                                           #
# --------------------------------------------------------------------------- #

def test_only_the_coder_role_is_measured(tmp_path):
    """The reviewer, planner and utility tiers read files too.

    They share `_agent_sink`, so their calls land in the same table under a
    different `source`. Counting them would measure a population the phase is
    not about.
    """
    db = _events_db(tmp_path / "roles.db", [
        _use("Read", "a", source="reviewer", file_path="/r/mod.py"),
        _result("a", 30_000, source="reviewer"),
        _use("Read", "b", source="planner", file_path="/r/mod.py"),
        _result("b", 30_000, source="planner"),
        _use("Read", "c", source="agent", file_path="/r/mod.py"),
        _result("c", 30_000, source="agent"),
    ])
    con = nv._connect(db)
    try:
        corpus = nv.read_events(con)
    finally:
        con.close()
    classes = _report(corpus, min_reads=0,
                      min_sessions=0)["measurement"]["classes"]
    assert classes["all"]["reads"] == 1


def test_one_task_is_one_session_and_windows_are_per_task(tmp_path):
    """Two tasks interleaved in one table, with their own boundaries.

    Events from concurrent tasks are interleaved by timestamp in the real
    table (the worker pool runs several at once), so a reader that tracked one
    current window would attribute one task's reads to another's turns.
    """
    db = _events_db(tmp_path / "tasks.db", [
        _use("Read", "a1", task_id="t1", file_path="/r/a.py"),
        _use("Read", "b1", task_id="t2", file_path="/r/b.py"),
        _result("a1", 1_000, task_id="t1"),
        _result("b1", 1_000, task_id="t2"),
        _boundary("attempt_start", task_id="t1"),
        _use("Read", "a2", task_id="t1", file_path="/r/a.py"),
        _result("a2", 1_000, task_id="t1"),
        _use("Bash", "b2", task_id="t2", command="pytest"),
    ])
    con = nv._connect(db)
    try:
        corpus = nv.read_events(con)
    finally:
        con.close()

    assert corpus.sessions == 2
    assert len(corpus.windows) == 3
    report = _report(corpus, min_reads=0, min_sessions=0)
    # t2's read is followed by one call in the SAME window; t1's two reads sit
    # in separate windows and are each last in theirs.
    assert report["measurement"]["classes"]["all"]["weighted"] == 1_000


def test_the_result_size_joins_by_tool_use_id_not_by_position(tmp_path):
    """Two reads and their results, delivered out of order.

    One assistant turn can carry several tool calls, so pairing by position is
    unsound — the same reason `claude_backend` carries the id at all.
    """
    db = _events_db(tmp_path / "join.db", [
        _use("Read", "first", file_path="/r/a.py"),
        _use("Read", "second", file_path="/r/b.py"),
        _result("second", 30_000),
        _result("first", 100),
    ])
    con = nv._connect(db)
    try:
        corpus = nv.read_events(con)
    finally:
        con.close()
    assert corpus.sizes == {"second": (30_000, False), "first": (100, False)}
    classes = _report(corpus, min_reads=0,
                      min_sessions=0)["measurement"]["classes"]
    assert classes["whole_file"]["reads"] == 1


def test_a_boolean_result_size_is_not_a_size(tmp_path):
    """`True` is an `int` in Python, and folding it in as 1 is phantom mass at
    the low end of the distribution being measured."""
    db = _events_db(tmp_path / "bool.db", [
        _use("Read", "a", file_path="/r/mod.py"),
        _result("a", True),   # type: ignore[arg-type]
    ])
    con = nv._connect(db)
    try:
        corpus = nv.read_events(con)
    finally:
        con.close()
    assert corpus.sizes == {}
    measurement = _report(corpus, min_reads=0, min_sessions=0)["measurement"]
    assert measurement["reads_without_result"] == 1
    assert measurement["classes"]["all"]["chars"] == 0


@pytest.mark.parametrize("raw", ["{not json", '"a string"', "null", "[]"])
def test_a_malformed_event_row_is_counted_and_skipped(tmp_path, raw):
    """A reader that raised here would be unable to measure a live table."""
    db = _events_db(tmp_path / f"bad{abs(hash(raw))}.db", [
        {"raw": raw},
        _use("Read", "a", file_path="/r/mod.py"),
        _result("a", 30_000),
    ])
    con = nv._connect(db)
    try:
        corpus = nv.read_events(con)
    finally:
        con.close()
    assert corpus.malformed == 1
    assert _report(corpus, min_reads=0, min_sessions=0)[
        "measurement"]["classes"]["all"]["reads"] == 1


def test_a_tool_use_with_no_input_or_a_junk_input_is_still_a_turn(tmp_path):
    """A call whose `tool_input` is absent or the wrong type still re-sends the
    conversation, so it must not be dropped from the turn count."""
    db = _events_db(tmp_path / "junk.db", [
        _use("Read", "a", file_path="/r/mod.py"),
        _result("a", 1_000),
        {"task_id": "t1", "source": "agent", "kind": "tool_use",
         "tool_name": "Bash", "tool_use_id": "b", "tool_input": "not a dict"},
        {"task_id": "t1", "source": "agent", "kind": "tool_use",
         "tool_name": "Bash", "tool_use_id": "c"},
        # No `tool_name` at all: not a call this reader can place.
        {"task_id": "t1", "source": "agent", "kind": "tool_use",
         "tool_use_id": "d"},
    ])
    con = nv._connect(db)
    try:
        corpus = nv.read_events(con)
    finally:
        con.close()
    classes = _report(corpus, min_reads=0,
                      min_sessions=0)["measurement"]["classes"]
    assert classes["all"]["weighted"] == 2_000


def test_a_missing_database_is_an_error_when_the_source_was_named(tmp_path):
    """Under `--source events` the caller named the population.

    Silently measuring a different one would be the worst outcome available,
    so a missing DB is an error there — while `auto` is allowed to fall back.
    """
    with pytest.raises(SystemExit) as excinfo:
        nv.pick_source("events", tmp_path / "nope.db", ())
    assert "no database" in str(excinfo.value)


def test_auto_falls_back_to_transcripts_when_the_db_holds_no_coder_reads(tmp_path):
    """A fresh checkout's DB exists and is empty, which is the common case."""
    db = _events_db(tmp_path / "fresh.db", [_boundary("attempt_start")])
    root = tmp_path / "roots"
    _transcript(root, "s0", [
        _t_use("Read", "a", file_path="/r/mod.py"),
        _t_result("a", "x" * 500),
    ])
    corpus = nv.pick_source("auto", db, (root,))
    assert corpus.source == "transcripts"
    assert len(corpus.windows) == 1


def test_auto_prefers_the_products_own_telemetry_when_it_has_reads(tmp_path):
    db = _events_db(tmp_path / "live.db", [
        _use("Read", "a", file_path="/r/mod.py"),
        _result("a", 500),
    ])
    root = tmp_path / "roots"
    _transcript(root, "s0", [_t_use("Read", "z", file_path="/r/other.py")])
    assert nv.pick_source("auto", db, (root,)).source == "events"


# --------------------------------------------------------------------------- #
# The transcript reader                                                       #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("content,expected", [
    ("hello world", 11),
    (None, 0),
    ([{"type": "text", "text": "hello world"}], 11),
    ([{"type": "text", "text": None}], 0),
    ([{"type": "image", "source": {"data": "x" * 5000}}], 0),
    ([{"type": "text", "text": "ab"}, {"type": "text", "text": "cde"}], 5),
])
def test_result_size_is_what_the_model_sees_not_a_python_repr(content, expected):
    """Mirrors `claude_backend._result_size`, including both of its corrections.

    `str(content)` over a block list counts ~30 chars of dict-repr punctuation
    as payload and records `None` as 4, planting phantom mass at the low end of
    the distribution. An image block carries a large base64 payload and zero
    text, and is counted as zero here for the same reason it is there.
    """
    assert nv._result_chars(content) == expected


@pytest.mark.parametrize("line", ["{not json", "null", '"text"', "[1,2]"])
def test_a_half_written_transcript_line_is_counted_and_skipped(tmp_path, line):
    """The last line of a LIVE session's log is routinely half-written.

    A reader that refused the file would be unmeasurable exactly while the
    data is most current.
    """
    root = tmp_path / "roots"
    _transcript(root, "s0", [
        _t_use("Read", "a", file_path="/r/mod.py"),
        _t_result("a", "x" * 30_000),
        line,
    ])
    corpus = nv.read_transcripts(nv._transcript_files((root,)))
    assert corpus.malformed == 1
    assert _report(corpus, min_reads=0, min_sessions=0)[
        "measurement"]["classes"]["whole_file"]["reads"] == 1


def test_blank_lines_are_not_malformed(tmp_path):
    root = tmp_path / "roots"
    _transcript(root, "s0", [
        "",
        _t_use("Read", "a", file_path="/r/mod.py"),
        "   ",
        _t_result("a", "x" * 500),
    ])
    corpus = nv.read_transcripts(nv._transcript_files((root,)))
    assert corpus.malformed == 0
    assert len(corpus.windows) == 1


def test_a_session_with_no_tool_calls_is_not_counted_as_a_session(tmp_path):
    """A chat that ran no tools tells this measurement nothing, and counting it
    would dilute the session floor with sessions that could not contribute."""
    root = tmp_path / "roots"
    _transcript(root, "empty", [{"type": "user",
                                 "message": {"content": "hello"}}])
    _transcript(root, "real", [
        _t_use("Read", "a", file_path="/r/mod.py"),
        _t_result("a", "x" * 500),
    ])
    corpus = nv.read_transcripts(nv._transcript_files((root,)))
    assert corpus.sessions == 1


def test_a_missing_transcript_root_is_not_an_error(tmp_path):
    """Only one of the two default roots exists on most machines."""
    assert nv._transcript_files((tmp_path / "absent",)) == []


def test_both_compaction_markers_close_a_window(tmp_path):
    for marker in ({"isCompactSummary": True},
                   {"subtype": "compact_boundary"}):
        root = tmp_path / f"roots{len(marker)}{list(marker)[0]}"
        _transcript(root, "s0", [
            _t_use("Read", "a", file_path="/r/mod.py"),
            _t_result("a", "x" * 500),
            {"type": "system", **marker},
            _t_use("Bash", "b", command="pytest"),
        ])
        corpus = nv.read_transcripts(nv._transcript_files((root,)))
        assert len(corpus.windows) == 2, marker


# --------------------------------------------------------------------------- #
# The output contract                                                         #
# --------------------------------------------------------------------------- #

def test_no_path_pattern_or_session_id_reaches_the_output(tmp_path):
    """The privacy claim in the module docstring, pinned to the output.

    Both surfaces are checked, because the aggregates are what makes this safe
    and a future column carrying a path would be a leak nothing else here
    would catch. The fixture's paths and Grep pattern are deliberately
    distinctive strings.
    """
    root = tmp_path / "roots"
    _transcript(root, "session-ffffffff", [
        _t_use("Grep", "g", pattern="parse_secret_config"),
        _t_use("Read", "a", file_path="/home/operator/private-repo/vault.py"),
        _t_result("a", "SUPERSECRETCONTENT" * 100),
    ])
    corpus = nv.read_transcripts(nv._transcript_files((root,)))
    report = _report(corpus, min_reads=0, min_sessions=0)
    surfaces = nv.render(report) + json.dumps(report)

    for secret in ("private-repo", "vault.py", "parse_secret_config",
                   "SUPERSECRETCONTENT", "session-ffffffff", "operator"):
        assert secret not in surfaces, secret
    # The extension is the one path-derived thing that IS reported.
    assert ".py" in surfaces


def test_the_json_shape_does_not_change_with_the_data(tmp_path):
    """Every class key present with zeros, so an automated reader cannot
    mistake an absent key for a zero."""
    corpus = _one_window(("Read", "a", {"file_path": "/r/mod.py"}))
    corpus.sizes["a"] = (10, False)
    report = _report(corpus, min_reads=0, min_sessions=0)
    measurement = report["measurement"]

    assert set(report) == {"source", "sessions", "windows", "malformed",
                           "large_read_chars", "lookback", "measurement",
                           "verdict", "robustness", "search_census"}
    assert set(report["search_census"]) == {
        "searches", "searches_with_pattern", "symbol_searches"}
    assert set(report["verdict"]) == {"decision", "reasons"}
    assert set(report["robustness"]) == {"probed", "fragile"}
    for probe in report["robustness"]["probed"].values():
        assert set(probe) == {"decision", "navigable_reads",
                              "whole_file_reads", "share_chars",
                              "share_weighted"}
    assert set(measurement["classes"]) == set(nv._CLASSES)
    for name in nv._CLASSES:
        assert set(measurement["classes"][name]) == {"reads", "chars", "weighted"}
    assert measurement["classes"]["sidechain"] == {
        "reads": 0, "chars": 0, "weighted": 0}


def test_the_report_names_its_population_and_its_thresholds(tmp_path):
    """A verdict whose population and thresholds are unstated is not a
    measurement — it cannot be reproduced or disagreed with."""
    report = _report(_corpus_of(tmp_path, sessions=25, ext=".py"))
    rendered = nv.render(report)

    assert "source: transcripts" in rendered
    assert "25 session(s)" in rendered
    assert "large read >= 12,000 chars" in rendered
    assert "lookback 3 call(s)" in rendered
    assert "symbol answer estimated at 2,000 chars" in rendered
    assert "VERDICT: PROCEED" in rendered


def test_the_rendered_report_is_ascii(tmp_path):
    """A verdict must survive a Windows console.

    The default code page on the `windows` CI runner is not UTF-8, and this
    output is the whole product of the script — a mangled or raising verdict
    line is a measurement nobody can read.
    """
    report = _report(_corpus_of(tmp_path, sessions=25, ext=".py"))
    nv.render(report).encode("ascii")


def test_an_empty_class_row_renders_a_dash_rather_than_dividing_by_zero():
    corpus = _one_window(("Read", "a", {"file_path": "/r/mod.py"}))
    corpus.sizes["a"] = (0, False)
    rendered = nv.render(_report(corpus, min_reads=0, min_sessions=0))
    assert "n/a" in rendered


@pytest.mark.parametrize("flag", [
    "--large-read-chars", "--symbol-response-chars", "--lookback",
    "--min-reads", "--min-sessions",
])
def test_a_negative_threshold_is_rejected_not_clamped(tmp_path, flag):
    """Each of these would still produce a plausible-looking table.

    A threshold silently corrected to 0 is a measurement of something nobody
    asked for, and the table would not say so.
    """
    root = tmp_path / "roots"
    _transcript(root, "s0", [
        _t_use("Read", "a", file_path="/r/mod.py"),
        _t_result("a", "x" * 500),
    ])
    with pytest.raises(SystemExit) as excinfo:
        nv.main(["--source", "transcripts", "--transcript-root", str(root),
                 flag, "-1"])
    assert "must not be negative" in str(excinfo.value)


def test_main_prints_a_verdict_and_exits_zero_on_a_measured_corpus(
        tmp_path, capsys):
    """End to end through `main`, on the argv a reader would actually type."""
    root = tmp_path / "roots"
    for i in range(25):
        _session(root, f"s{i}", ".py", with_search=True)

    assert nv.main(["--source", "transcripts",
                    "--transcript-root", str(root)]) == 0
    assert "VERDICT: PROCEED" in capsys.readouterr().out


def test_main_json_is_parseable_and_carries_the_verdict(tmp_path, capsys):
    root = tmp_path / "roots"
    for i in range(25):
        _session(root, f"s{i}", ".md", with_search=True)

    assert nv.main(["--source", "transcripts", "--transcript-root", str(root),
                    "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["verdict"]["decision"] == "HALT"
    assert report["source"] == "transcripts"
    assert report["measurement"]["classes"]["navigable"]["reads"] == 0


def test_the_script_writes_nothing_to_the_database_it_reads(tmp_path):
    """Opened `mode=ro`, so a measurement cannot mutate its own subject."""
    db = _events_db(tmp_path / "ro.db", [
        _use("Read", "a", file_path="/r/mod.py"),
        _result("a", 500),
    ])
    con = nv._connect(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO task_events (task_id, ts, data) "
                        "VALUES ('t9', 1.0, '{}')")
    finally:
        con.close()


def test_the_script_imports_nothing_from_the_product():
    """It must run against a DB or a log written by any version.

    A `no_human` import would tie the instrument to the checkout it runs in,
    and the corpus most worth measuring is the one an older build wrote.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    assert "import no_human" not in source
    assert "from no_human" not in source
