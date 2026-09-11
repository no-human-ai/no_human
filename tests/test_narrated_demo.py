"""Guards for the narrated sprint demo (e2e/demo_video: narration, sprint,
voiceover, compose, sprint_gui, sprint_cli).

Nothing here needs a browser, `say` or ffmpeg — the renders take minutes and
run by hand. What IS checked is everything that can rot silently and only
show up months later as a narrated line talking over the wrong screen:

* the timing map is self-consistent, inside the 60-90 s brief, and every
  line has room to be SPOKEN in its window
* narration.md — the doc a VO artist reads — agrees with narration.py
* the sprint fixture is cut to the narration: five real-looking tickets,
  created in the handoff, running in the parallel beat, all five PRs before
  the payoff, exactly one review bounce
* every scripted event survives the shipped serialisers, every review
  citation is a line of the diff, every line fits the shell's panes — the
  same bar test_demo_video.py holds the 26 s cut to
* both recorders' scripts and the audio assembly are functions of the map,
  not copies of it
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from e2e.demo_video import _freeze_probe as fp  # noqa: E402
from e2e.demo_video import compose, narration as nr  # noqa: E402
from e2e.demo_video import fixture as fx  # noqa: E402
from e2e.demo_video import sprint as sp  # noqa: E402
from e2e.demo_video import sprint_cli, sprint_gui, voiceover  # noqa: E402
from e2e.demo_video import verify_sync as vs  # noqa: E402

DEMO_DIR = Path(__file__).resolve().parents[1] / "e2e" / "demo_video"
NARRATION_MD = DEMO_DIR / "narration.md"

# --------------------------------------------------------------------------- #
# The timing map                                                               #
# --------------------------------------------------------------------------- #


def test_the_piece_fits_the_brief():
    """60-90 seconds, whole frames, black at both ends."""
    assert 60.0 <= nr.DURATION <= 90.0
    assert nr.N_FRAMES == int(nr.FPS * nr.DURATION)
    assert nr.fade_at(0.0) == 0.0
    assert nr.fade_at(nr.t_of(nr.N_FRAMES - 1)) == 0.0
    assert nr.FADE_OUT_END < nr.DURATION


def test_the_beats_are_the_briefed_arc_in_order():
    """hook -> handoff -> parallel -> gate -> payoff -> close, the operator's
    structure, in the operator's order."""
    names = [b.name for b in nr.BEATS]
    assert names == ["hook", "handoff", "parallel", "gate", "payoff", "close"]
    starts = [b.start for b in nr.BEATS]
    assert starts == sorted(starts)
    assert starts[0] == nr.FADE_IN_END


def test_every_line_lives_inside_its_beat():
    for i, beat in enumerate(nr.BEATS):
        end = nr.BEATS[i + 1].start if i + 1 < len(nr.BEATS) \
            else nr.FADE_OUT_START
        assert beat.lines, beat.name
        for line in beat.lines:
            assert beat.start <= line.start < end, (beat.name, line.start)


def test_lines_are_strictly_ordered_with_room_to_breathe():
    starts = [ln.start for ln in nr.LINES]
    assert starts == sorted(set(starts))
    for line, window in nr.line_windows():
        assert window >= 1.2, (line.start, window, line.text)


@pytest.mark.parametrize("line,window", nr.line_windows(),
                         ids=[f"{ln.start:05.2f}s" for ln, _w in
                              nr.line_windows()])
def test_every_line_can_be_spoken_in_its_window(line, window):
    """~2.75 words/s is voiceover.RATE's measured pace; a line denser than
    3.0 words per second of window cannot be read without racing the screen,
    no matter what the renderer measures later. This is the static version of
    voiceover.check_fit — it holds even on machines with no `say`."""
    words = len(line.tts_text.split())
    assert words / window <= 3.0, (line.text, words, window)


def test_the_gate_beat_outweighs_every_pitch_beat():
    """The independent reviewer is the differentiating claim and the timing
    has to show it: the gate runs longer than the hook and the handoff
    COMBINED, and only the parallel beat (which carries four lines) may
    exceed it. A recut that squeezes the gate to make room for more sizzle
    is the exact trade this test refuses."""
    spans = {}
    for i, beat in enumerate(nr.BEATS[:-1]):
        spans[beat.name] = nr.BEATS[i + 1].start - beat.start
    assert spans["gate"] > spans["hook"] + spans["handoff"], spans
    for name in ("hook", "handoff", "payoff"):
        assert spans["gate"] > spans[name], spans


def test_the_copy_carries_no_hype_vocabulary():
    """The brief bans the words a senior dev would smell. This is a floor,
    not a style checker — but the floor is load-bearing: one 'revolutionary'
    in a narrated demo undoes the whole evidence-first register."""
    banned = ("revolutionary", "supercharge", "game-chang", "10x", "magic",
              "blazing", "unleash", "effortless", "seamless", "incredible")
    text = " ".join(ln.text for ln in nr.LINES).lower()
    for word in banned:
        assert word not in text, word


def test_the_underscore_is_never_read_aloud():
    """Every line whose written text says "no_human" must carry a spoken
    variant saying "no human" — a TTS reading "no underscore human" in the
    close is the single worst frame the piece could ship."""
    for line in nr.LINES:
        if "no_human" in line.text:
            assert line.spoken, line.text
        assert "no_human" not in line.tts_text, line.text
        assert "_" not in line.tts_text, line.text


def test_the_close_names_the_product_and_makes_one_promise():
    last = nr.LINES[-1]
    assert last.text.startswith("no_human.")
    assert "merge" in last.text


def test_every_beat_describes_both_screens():
    for beat in nr.BEATS:
        assert beat.gui and beat.cli and beat.caption, beat.name
        # The caption bar clips past ~70 chars (captions.py's measured bar).
        assert len(beat.caption) <= 70, beat.caption


# --------------------------------------------------------------------------- #
# narration.md — the doc must agree with the data                              #
# --------------------------------------------------------------------------- #


#: A script line in the doc: `> **12.40** — Tag them in your tracker, ...`,
#: continued on any following `>` lines until a blank one.
_DOC_LINE_RE = re.compile(r"^>\s*\*\*(\d+\.\d{2})\*\*\s*—\s*(.*)$")
_DOC_CONT_RE = re.compile(r"^>\s+(\S.*)$")


def _parse_doc_script() -> tuple[tuple[tuple[float, str], ...], tuple[str, ...]]:
    """The document's narration lines IN ORDER, plus every `>` line that was
    not part of one.

    Both halves are needed and the second is the one the first draft dropped on
    the floor. A `>` line placed BEFORE the first `> **t.tt** —` head has no
    line to attach to; the draft silently discarded it, so a sentence sitting
    at the top of the script — which a VO artist would simply read — was
    invisible to every check. It is now returned as an orphan and refused.
    """
    found: list[tuple[float, list[str]]] = []
    orphans: list[str] = []
    for raw in NARRATION_MD.read_text(encoding="utf-8").splitlines():
        head = _DOC_LINE_RE.match(raw)
        if head:
            found.append((float(head.group(1)), [head.group(2).strip()]))
            continue
        cont = _DOC_CONT_RE.match(raw)
        if cont:
            if found:
                found[-1][1].append(cont.group(1).strip())
            else:
                orphans.append(cont.group(1).strip())
    lines = tuple((t, " ".join(p for p in parts if p)) for t, parts in found)
    return lines, tuple(orphans)


def _doc_script_lines() -> tuple[tuple[float, str], ...]:
    """Every narration line the DOCUMENT claims, parsed out of its blockquotes.

    Reading the doc's own claims — rather than only looking up the map's claims
    inside it — is what makes the agreement check bidirectional.
    """
    return _parse_doc_script()[0]


def test_the_doc_carries_every_line_at_its_time():
    """MAP -> DOC: every line the map schedules is written in the document."""
    doc = NARRATION_MD.read_text(encoding="utf-8")
    # The doc wraps prose at ~76 cols inside `>` blockquotes; strip the
    # markers and collapse whitespace before comparing.
    flat = " ".join(doc.replace(">", " ").split())
    for line in nr.LINES:
        stamp = f"{line.start:.2f}"
        assert stamp in doc, f"missing time {stamp}"
        assert line.text in flat, line.text[:50]


def test_the_doc_claims_no_line_the_map_does_not_schedule():
    """DOC -> MAP: the other direction, which was missing.

    The document said of itself that "a test asserts this document agrees with
    it line for line, so neither can drift" — and only one direction was
    checked. Appending

        > **99.00** — Ships with Klarna and Adyen integrations out of the box.

    to a SHIP-classified file that gets published left the whole suite green
    (measured). A VO artist reads this document, not `narration.py`, so a line
    that exists only here is a line that gets recorded.
    """
    scheduled = [(round(ln.start, 2), ln.text) for ln in nr.LINES]
    claimed, orphans = _parse_doc_script()
    claimed = [(round(t, 2), text) for t, text in claimed]

    # A LIST, not a set. Compared as sets, an exact duplicate of a line
    # collapses into the one already there and the document can say a sentence
    # twice — which is twice as much audio — with nothing going red. Order is
    # worth holding for the same reason: the document is read top to bottom.
    assert claimed == scheduled, (
        f"only in the document: {sorted(set(claimed) - set(scheduled))}\n"
        f"only in narration.py: {sorted(set(scheduled) - set(claimed))}\n"
        f"document order: {[t for t, _x in claimed]}\n"
        f"map order:      {[t for t, _x in scheduled]}")
    assert not orphans, (
        f"{len(orphans)} quoted line(s) in the script that belong to no "
        f"narration line: {orphans}. A VO artist reads what is in the "
        f"blockquote; text with no timestamp gets read anyway.")


#: HOW A TIMING GATE NAMES THE LINE IT IS ABOUT — the one pattern, used by
#: every gate below, because three different ways of pointing at a sentence had
#: three different silent failure modes.
#:
#:   `beat.lines[0]`   POSITIONAL, and the defect this pair of helpers replaces.
#:       "the ramp must finish before the line that counts five" was written as
#:       `parallel.lines[0]`. Insert one sentence at the top of that beat — a
#:       perfectly ordinary copy edit — and the assertion silently re-points at
#:       a different claim and keeps passing. It never goes red; it just stops
#:       being about the thing it is named for.
#:   `next(... if fragment in ln.text)`   raises StopIteration on a copy edit,
#:       which reads as a broken test rather than as "the line this gate guards
#:       is gone", and quietly takes the FIRST of several matches if the phrase
#:       is ever repeated.
#:   `next(... if abs(ln.start - 17.60) < 0.01)`   the same, keyed on a
#:       hand-written second that the map is free to move.
#:
#: Both helpers below assert a COUNT. Exactly one line may answer, so a gate
#: whose subject has been edited away, or duplicated, fails NAMING ITS SUBJECT
#: instead of dying on an iterator.

def _line_saying(fragment: str) -> nr.Line:
    """The one narration line whose text contains ``fragment``."""
    hits = [ln for ln in nr.LINES if fragment.lower() in ln.text.lower()]
    assert len(hits) == 1, (
        f"{len(hits)} narration line(s) say {fragment!r}; a timing gate is "
        f"anchored on there being exactly one. Matches: "
        f"{[ln.text for ln in hits]}")
    return hits[0]


def _line_at(second: float) -> nr.Line:
    """The one narration line that starts at ``second``."""
    hits = [ln for ln in nr.LINES if abs(ln.start - second) < 0.01]
    assert len(hits) == 1, (
        f"{len(hits)} narration line(s) start at {second:.2f}s; a timing gate "
        f"is anchored on there being exactly one. Lines: "
        f"{[(ln.start, ln.text) for ln in hits]}")
    return hits[0]


def _ramp_window() -> tuple[float, float]:
    """When the five actually start running, computed from `STAGES`.

    "Actually" excludes `pending`, exactly as `sprint.inflight_at` does — a
    queued task is on the board but is not one of the five the voice counts.
    """
    firsts = [next(t for t, status, *_ in sp.STAGES[key]
                   if status in sp.RUNNING and status != "pending")
              for key in sp.CHOSEN]
    return min(firsts), max(firsts)


def test_the_doc_states_the_ramp_the_fixture_actually_runs():
    """The synchronised-moments table is a claim about the code, so it is
    checked against the code.

    It said `17.20–19.60`, and that window had been DELETED: `sprint.py`
    records removing it because a ramp running through "Five agents start" at
    17.60 made the header read "5 running" beside "4/5 workers busy - 1 queued".
    The fixture moved to 16.40–17.40; the document kept the number the fix had
    just removed, and shipped it publicly for a round. Deriving it is the only
    version of this that cannot happen twice.
    """
    start, end = _ramp_window()
    row = f"| {start:.2f}–{end:.2f} |"
    assert row in NARRATION_MD.read_text(encoding="utf-8"), (
        f"the synchronised-moments table must say {row} — the ramp computed "
        f"from STAGES. A hand-written window here is a claim about timing "
        f"that no longer has to be true.")
    # The reason the window matters: the ramp finishes before the sentence that
    # claims every ticket already has agents on it. Anchored on THAT SENTENCE,
    # with a count assertion — see `_line_saying`. It used to be
    # `parallel.lines[0]`, so one inserted line would have re-pointed this at a
    # different claim without anything going red.
    claim = _line_saying("Each one gets a team of agents")
    assert end < claim.start, (end, claim.start, claim.text)
    # ...and that sentence is genuinely the parallel beat's opener, which is
    # the property `lines[0]` was standing in for. Stated separately so the two
    # cannot be confused again: this one is about STRUCTURE, the one above is
    # about TIMING.
    parallel = next(b for b in nr.BEATS if b.name == "parallel")
    assert claim in parallel.lines, claim.text
    assert claim.start == min(ln.start for ln in parallel.lines)


def test_an_anchor_refuses_to_be_ambiguous_instead_of_taking_the_first(
        monkeypatch):
    """The helpers every timing gate points through, checked rather than
    assumed — a gate is only as good as its ability to say what it is about.

    Both directions: nothing answering, and more than one answering. The second
    is the one `next(...)` got wrong silently — it takes the first match and
    the gate quietly becomes about a different sentence.
    """
    with pytest.raises(AssertionError) as err:
        _line_saying("a sentence this script does not contain")
    assert "0 narration line(s)" in str(err.value)
    with pytest.raises(AssertionError) as err:
        _line_at(99.99)
    assert "0 narration line(s)" in str(err.value)

    real = _line_saying("did not pass")
    dup = nr.Line(99.00, real.text + " (a duplicate)")
    monkeypatch.setattr(nr, "LINES", nr.LINES + (dup,))
    with pytest.raises(AssertionError) as err:
        _line_saying("did not pass")
    assert "2 narration line(s)" in str(err.value)
    monkeypatch.setattr(nr, "LINES", nr.LINES + (nr.Line(real.start, "x"),))
    with pytest.raises(AssertionError) as err:
        _line_at(real.start)
    assert "2 narration line(s)" in str(err.value)


def test_the_ramp_gate_is_anchored_on_its_sentence_not_on_a_position(
        monkeypatch):
    """The defect, reproduced: a positional anchor survives a copy edit by
    silently changing what it is about.

    `test_the_doc_states_the_ramp_the_fixture_actually_runs` asserted
    `end < parallel.lines[0].start`. Prepend one ordinary sentence to the
    parallel beat and `lines[0]` is a DIFFERENT line — the gate keeps passing
    and has stopped guarding the claim it is named for. Anchored on the
    sentence, the same edit leaves the gate pointing where it always did.
    """
    parallel = next(b for b in nr.BEATS if b.name == "parallel")
    claim = _line_saying("Each one gets a team of agents")
    assert parallel.lines[0] is claim, "the precondition the old gate relied on"

    # Placed BETWEEN the ramp's end and the claim, which is what makes the
    # re-point silent: the positional comparison still holds against it.
    intruder = nr.Line(round((_ramp_window()[1] + claim.start) / 2, 2),
                       "First, about branches.")
    assert parallel.start <= intruder.start < claim.start
    edited = nr.Beat(parallel.name, parallel.start, parallel.caption,
                     parallel.gui, parallel.cli, (intruder,) + parallel.lines)
    beats = tuple(edited if b.name == "parallel" else b for b in nr.BEATS)
    monkeypatch.setattr(nr, "BEATS", beats)
    monkeypatch.setattr(nr, "LINES",
                        tuple(ln for b in beats for ln in b.lines))

    # The intruder is now `lines[0]`, and a POSITIONAL gate's comparison still
    # holds against it — which is precisely why the re-point was silent.
    moved = next(b for b in nr.BEATS if b.name == "parallel")
    assert moved.lines[0] is intruder
    assert _ramp_window()[1] < moved.lines[0].start

    # So RUN THE REAL GATE under the edited map and require it to go red.
    #
    # Calling the shipped test function is the whole point: a version of this
    # that re-checked the comparison inline was written first and MEASURED —
    # reverting the gate to `parallel.lines[0]` left it green, because it was
    # observing its own copy of the logic and not the gate. A check nothing
    # observes is not a check.
    with pytest.raises(AssertionError):
        test_the_doc_states_the_ramp_the_fixture_actually_runs()

    # And what it goes red ABOUT: the sentence is still found, unmoved — the
    # gate fails on the STRUCTURAL claim (this sentence opens the beat), which
    # is a real editorial change a human should look at, not on a broken
    # iterator or a silently different subject.
    assert _line_saying("Each one gets a team of agents") is claim
    assert claim.start != min(ln.start for ln in moved.lines)


def test_the_document_names_no_real_company_either():
    """The brand denylist, over the document's OWN prose.

    The line-for-line check above constrains the script; the commentary around
    it — voice direction, the timing table, "why the copy is shaped like this"
    — is free text in a file the export publishes, and nothing was reading it.
    """
    text = NARRATION_MD.read_text(encoding="utf-8").lower()
    for word in _REAL_COMPANIES:
        assert word not in text, word


#: Proper nouns the DOCUMENT needs in order to describe the demo, as opposed to
#: anything the demo puts on screen. Reviewed by reading every one: the six beat
#: names, the table headers, the two surface names, ordinary sentence openers,
#: and the status words the timing table quotes. `Jira` is here deliberately —
#: it is the tracker the piece depicts handing tickets to, it is already the
#: `source` field of every synthetic ticket, and naming it is the point of that
#: beat rather than an accident.
_DOC_PROSE_NOUNS = frozenset("""
    And Nobody They
    All BOTH Beat CLI Close Duration Event Every FAILS GUI Gate Handed Handoff
    Hook Jira Kept Key Narration Only PASSED PRs Parallel Pause Payoff Queued
    Second Short Starts Statuses Ten Tests Timing Voice Why
""".split())


def test_the_documents_prose_uses_no_unreviewed_proper_noun():
    """The ALLOWLIST — not just the denylist — over the whole published document.

    The denylist above only refuses brands somebody already thought of, and the
    document's prose is the one published surface it was guarding alone: a
    sentence in "why the copy is shaped like this" naming a real vendor passes
    a denylist that has never heard of it. The script itself is pinned to the
    map line for line, so this is aimed at everything around the script.

    The reviewed set is the demo's own plus the vocabulary this document needs
    to talk ABOUT the demo — beat names, column headers, the two surfaces.
    """
    prose_words = _DOC_PROSE_NOUNS | _ALLOWED_PROPER_NOUNS
    seen = set(_PROPER_NOUN_RE.findall(NARRATION_MD.read_text(encoding="utf-8")))
    unreviewed = sorted(seen - prose_words)
    assert not unreviewed, (
        f"{len(unreviewed)} proper noun(s) in the published document that "
        f"nobody reviewed: {unreviewed}\n\nThis file ships. If they are part "
        f"of the invented world or the vocabulary the document needs to "
        f"describe it, add them to _DOC_PROSE_NOUNS; if they name anything "
        f"real, they must not be in a document that gets published.")


def test_the_doc_carries_every_beat_and_the_duration():
    doc = " ".join(NARRATION_MD.read_text(encoding="utf-8").split())
    assert f"{nr.DURATION:.3f}" in doc
    assert str(nr.N_FRAMES) in doc
    for beat in nr.BEATS:
        assert f"{beat.start:.2f}" in doc, beat.name
        assert beat.gui in doc, beat.name
        assert beat.cli in doc, beat.name


# --------------------------------------------------------------------------- #
# The sprint fixture                                                           #
# --------------------------------------------------------------------------- #


def test_the_backlog_is_ten_real_looking_tickets_five_chosen():
    assert len(sp.BACKLOG) == 10
    assert len(sp.CHOSEN) == 5
    keys = [k for k, *_ in sp.BACKLOG]
    assert len(set(keys)) == 10
    for key in keys:
        assert re.fullmatch(r"[A-Z]{2,6}-\d{2,5}", key), key
    for key, title, kind, _chosen in sp.BACKLOG:
        assert kind in ("feature", "bugfix", "ci_fix", "traceability",
                        "test_gap"), (key, kind)
        assert 10 <= len(title) <= 50, (key, title)
    # The chosen five cover the brief's spread, not five of a kind.
    assert len({kind for k, _t, kind, chosen in sp.BACKLOG if chosen}) >= 3


#: WHERE THE EMPLOYER / MAINTAINER HALF OF THIS CHECK LIVES — deliberately NOT
#: here.
#:
#: This test used to carry its own denylist of private terms as plain string
#: literals. That put the operator's employer name into a SHIP-classified file,
#: which is the leak the whole de-identification programme exists to stop.
#: Hex-encoding them was the next attempt and is not sufficient either:
#: `scripts/export_guard.py` decodes encoded runs and refused the file
#: ("2 scan hit(s) ... decoded from an encoded run"). It was right to.
#:
#: The duplication was the real defect. `tests/test_no_banned_terms.py` already
#: scans EVERY tracked file against the private inventories, and this fixture is
#: tracked: seeding the employer's name into `sprint.py:71` makes
#: `test_no_banned_terms_in_shipped_files` fail with that exact path (measured).
#: So the protection is not weakened by removing it from here — it is asserted
#: once, by the guard that owns the inventory, in a file classified `drop` so it
#: may legitimately hold the terms.
#:
#: What this file keeps is the half the repo-wide guard does NOT cover: that the
#: demo's invented world stays inside its own synthetic namespace. That is an
#: ALLOWLIST, so a name nobody thought to ban still fails.


#: A fixture constant, by shape rather than by name.
_FIXTURE_CONST_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _strings(value) -> list[str]:
    """Every string leaf inside an arbitrarily nested fixture value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items()
                for s in _strings(k) + _strings(v)]
    if isinstance(value, (tuple, list, set, frozenset)):
        return [s for v in value for s in _strings(v)]
    return []


def _fixture_surfaces() -> tuple[str, ...]:
    """Names of the public `sprint.py` constants that carry text.

    This is ONE of the two things the corpus reads, and on its own it is the
    narrower one: module-level `UPPER_CASE` names in `sprint.py` and nothing
    else. It exists because module constants are the fixture's *runtime* values
    — `BRANCH` is an f-string over `HERO_ID`, so its actual text exists only
    once the module is imported and no source scan can see it.

    Everything a source scan CAN see is covered by `_source_string_literals`
    below, which is what closes the holes this function alone left open:
    literals inside functions, lowercase module names, and the other two
    modules entirely.
    """
    return tuple(sorted(
        name for name, value in vars(sp).items()
        if _FIXTURE_CONST_RE.match(name) and _strings(value)))


#: The three modules that build the narrated sprint's screens. `fixture.py` is
#: deliberately NOT here: it is the 26 s cut's fixture (a calc.py/mul world with
#: no overlap with this one), it is guarded by `test_demo_video.py`, and pulling
#: it in adds eleven proper nouns from a different film to this film's reviewed
#: set. That is the scope boundary, stated rather than left to be discovered.
_FIXTURE_MODULES = ("sprint.py", "sprint_gui.py", "sprint_cli.py")


def _source_string_literals(path: Path) -> list[str]:
    """Every string literal in a module's source, except docstrings.

    WHY THE SOURCE AND NOT THE MODULE OBJECT. The runtime scan above sees only
    what `sprint.py` binds to an UPPER_CASE name. Three kinds of on-camera text
    escape it, and all three were shown to smuggle a brand name through:

      * a literal inside a function — `sprint.py`'s `hero_detail()` builds
        strings the drawer renders and binds none of them to a constant;
      * a lowercase module-level name;
      * anything at all in `sprint_gui.py` or `sprint_cli.py`, which were not
        read even once. `sprint_gui.py:182` is the sharpest case — "Approval
        recorded. You merge the PR in your git host…" is VISIBLE IN THE SHIPPED
        CUT at t=67 s, and no guard in this file had ever looked at it.

    Docstrings are excluded because they are prose about the code, never
    rendered, and including them buries the reviewed set in commentary.

    THE SCOPE THIS ACTUALLY HAS, written after running it rather than before:
    every string literal in the three modules, whether or not it can reach the
    screen. That is deliberately WIDER than "on camera" — it is a superset, so
    it fails closed — and the price is that a few entries in the reviewed set
    below are code vocabulary (`GET`, `Escape`, `SPA`) that no viewer will ever
    see. A string built at runtime from pieces (`"".join(...)`, `%` formatting)
    is still invisible to this scan; f-strings are visible only as their
    literal fragments.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings]


def _on_camera_text() -> str:
    """Every string the narrated demo can put in front of a viewer, in one blob.

    Two readings of the same three modules, because neither alone is enough:
    the RUNTIME constants of `sprint.py` (values computed at import, invisible
    to a source scan) and the SOURCE literals of all three (function-local and
    lowercase text, invisible to a runtime scan). Plus the narration and the
    caption strips, which are read aloud and burned into every frame.

    Case is PRESERVED here — the proper-noun check below needs it — and
    `_viewer_corpus` is the lowercased view. One corpus, two readings, so text
    added for one check cannot be missing from the other.
    """
    parts = [s for name in _fixture_surfaces()
             for s in _strings(getattr(sp, name))]
    for module in _FIXTURE_MODULES:
        parts += _source_string_literals(DEMO_DIR / module)
    parts += [ln.text for ln in nr.LINES]
    parts += [b.caption for b in nr.BEATS]
    return " ".join(parts)


def _viewer_corpus() -> str:
    return _on_camera_text().lower()


@pytest.mark.parametrize("attr", _fixture_surfaces())
def test_the_viewer_corpus_reads_every_runtime_constant(attr, monkeypatch):
    """Every text-carrying constant of `sprint.py` must reach the corpus through
    its OWN name, not by accident of another field quoting it.

    A SENTINEL, not the real value: the obvious version of this test — assert
    `sp.REPO_NAME in corpus` — passes without REPO_NAME being read at all,
    because REPO_PATH is "/Users/dev/git/shop" and contains it. That draft was
    written, mutation-tested and found to be a tautology.

    WHAT THIS CANNOT DO, because the honest limit is the point of the test
    below it. Both the parameter list and the corpus are derived from
    `_fixture_surfaces()`, so this test compares a derivation against itself:
    it proves the corpus reads everything that function DISCOVERS, and is blind
    by construction to anything the function fails to discover. When the
    derivation was `sprint.py`'s constants only, this test was green while
    three classes of on-camera text went unscanned. A guard whose oracle is its
    own subject cannot fail at the root, only in the middle.
    """
    monkeypatch.setattr(sp, attr, f"zz{attr.lower()}zz")
    assert f"zz{attr.lower()}zz" in _viewer_corpus(), attr


#: Strings that are provably on camera, written out by hand from watching the
#: cut and reading the modules — NOT produced by any discovery this file does.
#: That is the entire value: they are held OUT of the derivation, so they still
#: fail if the derivation is wrong at its root rather than merely incomplete.
#: Each is followed by where a viewer sees it.
_HELD_OUT_ON_CAMERA = (
    ("Approval recorded.", "the toast after Approve is clicked, t=67s"),
    ("no built SPA at ", "sprint_gui, a function-local literal"),
    ("Alex", "the operator name /api/config serves to the sidebar"),
    ("Cap request body size at 2 MB", "the backdrop PR card, t=0-25s"),
    ("Webhook retry double-charges on 502", "the hero card, whole clip"),
)


@pytest.mark.parametrize("needle,where", _HELD_OUT_ON_CAMERA,
                         ids=[n[:24] for n, _w in _HELD_OUT_ON_CAMERA])
def test_a_held_out_on_camera_string_is_in_the_corpus(needle, where):
    """The negative the derived guards cannot be: a known on-camera string,
    named here rather than discovered, asserted present in the corpus.

    Every other check in this section asks "does the corpus contain what the
    corpus is built from". This one asks the question that actually matters —
    "does the corpus contain what is ON THE SCREEN" — and it is the only one
    that goes red if `_fixture_surfaces` and `_source_string_literals` are BOTH
    wrong in the same direction. Two rounds of review found exactly that: a
    corpus that was internally consistent and did not read the screen.
    """
    assert needle in _on_camera_text(), (needle, where)


def test_the_viewer_corpus_also_carries_the_narration_and_captions():
    """The caption strip is burned into every frame and the narration is read
    aloud, so both are "on camera" in the sense this guard means."""
    corpus = _viewer_corpus()
    assert nr.BEATS[0].caption.lower() in corpus
    assert nr.LINES[0].text.lower() in corpus


#: Public brand names, not private inventory terms, so they are legitimate
#: plaintext in a shipped file — the payments and tracker vendors a plausible
#: "shop" fixture would otherwise drift into naming. One list, read by both the
#: on-camera scan and the narration document's scan, so the two cannot drift.
_REAL_COMPANIES = ("atlassian", "stripe", "paypal", "shopify", "acme",
                   "salesforce", "workday", "zendesk", "klarna", "adyen")


def test_nothing_synthetic_impersonates_a_real_company():
    """No real third-party brand anywhere a viewer could read."""
    corpus = _viewer_corpus()
    for word in _REAL_COMPANIES:
        assert word not in corpus, word


#: Every proper-noun-SHAPED token the fixture is allowed to put on camera,
#: reviewed by reading every entry. Three closed classes and nothing else:
#: English words that happen to start a sentence, code identifiers that appear
#: in the diff/plan/trail, and the invented product vocabulary. No count is
#: quoted here on purpose — a number in prose beside a list that changes is one
#: more claim to fall out of step with its mechanism, which is the defect this
#: file has been through three rounds of review for.
#:
#: This is the generalising half. A denylist only refuses names somebody
#: already thought of — "Klarna" in a ticket title and "Adyen" in a description
#: both passed the denylist version of this file. Against a closed set they
#: fail without anyone having predicted them.
_ALLOWED_PROPER_NOUNS = frozenset("""
    And Nobody They Ten Try
    Each Edit Extract Files Five Hand Monday Mondays Nightly Nothing One Pick
    Rate Read Record Run Sprint Tag That The Then You Your Delivery Digest
    Order Unsubscribe Webhook Chargeback Idempotency Deflake Cap
    API CSV HTTP SKUs POSTs True False Grep Response Charge CheckoutService
    NOTIF ORD PAY PLAT Users
    Alex Approval Approve Board Diff Done Review Spec System Working
    Escape GET SPA
""".split())
#: The last two rows arrived with the source-literal scan and are worth calling
#: out separately, because they are why that scan was added. Row 4 is on-camera
#: chrome the fixture serves and nothing had ever read: `Alex` is the operator
#: name from `sprint_gui.py`'s /api/config, `Approval` opens "Approval recorded.
#: You merge the PR in your git host…" which is VISIBLE at t=67 s, and
#: Diff/Spec/System/Review/Board/Done/Working/Approve are drawer and lane
#: labels. Row 5 is the honest cost of scanning source rather than screens:
#: `GET`, `Escape` and `SPA` are code vocabulary no viewer will ever see, kept
#: because a scan wide enough to be fail-closed is wider than the screen.
#: The last row is the identifier vocabulary the free-text scan also sees:
#: the four invented project prefixes, and the "/Users" segment of the
#: placeholder home path. They are pinned exactly by
#: `test_every_identifier_comes_from_the_synthetic_namespace`; they are listed
#: here because this scan reads the same blob and would otherwise flag them.

#: The shape this check can see. Deliberately narrow and stated plainly:
#: TitleCase or ALLCAPS runs of three or more letters. A brand written in
#: lower case, or a two-letter one, is NOT caught here — that is what the
#: denylist below and the repo-wide guard are for.
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")


def test_every_proper_noun_on_camera_is_in_the_reviewed_set():
    """No unreviewed proper noun reaches the screen.

    Covers the FREE-TEXT surfaces — ticket titles, descriptions, the diff, the
    plan, the review checklist, the event trail, acceptance criteria, the
    branch, the narration and the caption bar — which the identifier check
    below does not constrain at all.
    """
    seen = set(_PROPER_NOUN_RE.findall(_on_camera_text()))
    unreviewed = sorted(seen - _ALLOWED_PROPER_NOUNS)
    assert not unreviewed, (
        f"{len(unreviewed)} proper noun(s) on camera that nobody reviewed: "
        f"{unreviewed}\n\nIf they are part of the invented world, add them to "
        f"_ALLOWED_PROPER_NOUNS. If they name anything real, they must not be "
        f"in a demo that gets published.")


def test_every_identifier_comes_from_the_synthetic_namespace():
    """The IDENTIFIER half: keys, repo, paths and PR URLs.

    Scope, stated exactly, because an earlier version of this docstring
    claimed the whole file's coverage and was disproved — it said ANY
    unplanned real-world name would show up here, while "Klarna" in a ticket
    title passed. Titles and the other free text are
    `test_every_proper_noun_on_camera_is_in_the_reviewed_set`'s job; this one
    constrains only the identifiers below.
    """
    projects = {"PAY", "ORD", "NOTIF", "PLAT"}
    for key, _title, _kind, _chosen in sp.BACKLOG:
        assert key.split("-")[0] in projects, key
    assert sp.BACKDROP[0]["external_id"].split("-")[0] in projects

    assert sp.REPO_NAME == "shop"
    assert sp.REPO_PATH == "/Users/dev/git/shop"
    for url in list(sp.PR_URLS.values()) + [sp.BACKDROP[0]["pr_url"]]:
        assert url.startswith("https://github.com/you/shop/pull/"), url

    # The one human name the surfaces render is the product's own placeholder
    # operator, served by sprint_gui's /api/config.
    assert "Alex" not in " ".join(ln.text for ln in nr.LINES)


def test_every_chosen_ticket_is_created_inside_the_handoff():
    for key in sp.CHOSEN:
        created = sp.STAGES[key][0][0]
        assert sp.HANDOFF < created < sp.PARALLEL, (key, created)


def test_all_five_run_in_parallel_during_the_parallel_beat():
    """The narration says "five agents start" at 17.60 and the worker widget
    prints the in-flight count: it must reach 5 while those words hang."""
    assert sp.inflight_at(sp.PARALLEL + 3.0) == 5
    for key in sp.CHOSEN:
        status = sp.stage_at(key, sp.PARALLEL + 3.0)[0]
        assert status not in ("pending", None), (key, status)


def test_the_five_are_all_running_before_the_voice_says_five():
    """The stricter version, and the one that bites: the ramp must FINISH
    before the line, not during it.

    It used to run 17.2-19.6 while "Five agents start" is spoken at 17.60, so
    at t=19.0 the header showed "5 running" (the Working lane, which counts
    queued) next to "4/5 workers busy - 1 queued" (the chip, which counts
    agents). Both were right by their own definition and the pair was
    unreadable — a viewer counts, and one of the two numbers is then wrong.
    """
    # Found by TIME, not by a phrase. This is a timing assertion: what matters
    # is that the ramp has finished before the beat that announces five, and the
    # beat is defined by its second on the map. Keying it to wording made a copy
    # edit throw StopIteration here — a test coupled to the thing it is not
    # about. `_line_at` keeps the time anchor and adds the count assertion, so
    # moving the map fails saying WHICH second lost its line.
    line = _line_at(17.60)
    assert sp.inflight_at(line.start) == 5, sp.queue_health_at(line.start)
    assert sp.queued_at(line.start) == 0
    # And it holds for the whole line, not just its first frame.
    window = dict(nr.line_windows())[line]
    for t in [line.start + i * 0.5 for i in range(int(window / 0.5) + 1)]:
        assert sp.inflight_at(t) == 5, (t, sp.queue_health_at(t))


def test_the_review_lane_holds_exactly_the_five_the_voice_counts():
    """"The result: five pull requests" is spoken over the Review lane, whose
    badge prints its own count. Yesterday's leftover PR made that badge read 6
    against a narration saying five, so it is merged mid-clip and moves to the
    Done tally."""
    at_payoff = [r for r in sp.board_at(sp.PAYOFF) if r["lane"] == "review"]
    assert len(at_payoff) == 5, [r["title"] for r in at_payoff]
    assert {r["external_id"] for r in at_payoff} == set(sp.CHOSEN)


def test_the_leftover_pr_opens_the_board_and_leaves_before_the_payoff():
    """It has to be there at the hook — "the board, quiet: one leftover PR in
    Review" is the opening shot — and gone from that lane by the payoff."""
    hook = [r for r in sp.board_at(0.0) if r["lane"] == "review"]
    assert [r["external_id"] for r in hook] == ["PLAT-948"]
    assert sp.BACKDROP_DONE_AT < sp.PAYOFF
    after = next(r for r in sp.board_at(sp.PAYOFF)
                 if r["external_id"] == "PLAT-948")
    assert after["status"] == "done" and after["lane"] == "done"
    # Behind the open drawer, so no card visibly winks out of a live lane.
    assert sprint_gui.OPEN_DRAWER_AT < sp.BACKDROP_DONE_AT < \
        sprint_gui.CLOSE_DRAWER_AT


def test_the_header_drain_chip_tells_the_fixtures_truth():
    """`drainChip.js` formats {workers_busy}/{max_workers} · {queue_depth}
    queued, and it is on camera in every wide shot. During the handoff the
    five are queued; during the parallel beat all five slots are busy. The
    first capture served a wrong-shaped stub here and the chip read
    "0/0 workers busy" under a narration claiming five agents were running."""
    handoff = sp.queue_health_at(sp.PARALLEL - 1.0)
    assert handoff == {"workers_busy": 0, "max_workers": 5,
                       "queue_depth": 5, "est_drain_seconds": 120}
    running = sp.queue_health_at(sp.PARALLEL + 3.0)
    assert running["workers_busy"] == 5 and running["queue_depth"] == 0
    done = sp.queue_health_at(sp.PAYOFF + 1.0)
    assert done["workers_busy"] == 0 and done["queue_depth"] == 0


def test_all_five_prs_exist_before_the_payoff_opens():
    """"The result: five pull requests" is spoken at 56.40; a card still in
    Working at 56.0 makes the narration a liar."""
    for key in sp.CHOSEN:
        status = sp.stage_at(key, sp.PAYOFF)[0]
        assert status == "awaiting_approval", (key, status)
        assert sp.row_at(key, sp.PAYOFF)["pr_url"] == sp.PR_URLS[key]
    assert len(set(sp.PR_URLS.values())) == 5


def test_exactly_one_task_bounces_off_review_and_still_lands():
    """The gate that never fails anything is decoration; a gate that fails
    two of five makes the payoff a lie. Exactly one, and it recovers."""
    bounced = [key for key in sp.CHOSEN
               if any(a == 2 for *_x, a in sp.STAGES[key])]
    assert bounced == ["ORD-2187"]
    stages = [s for _t, s, *_ in sp.STAGES["ORD-2187"]]
    # reviewing -> implementing is the bounce; it must happen once and end
    # awaiting_approval.
    assert "implementing" in stages[stages.index("reviewing"):]
    assert stages[-1] == "awaiting_approval"


def test_the_bounce_is_on_camera_when_it_is_narrated():
    """"The refactor did not pass. It went back…" starts at 50.60 — by
    then ORD-2187 must be visibly on its second round, and not yet done."""
    line = _line_saying("did not pass")
    status, _live, _burn, _turns, attempt = sp.stage_at("ORD-2187", line.start)
    assert attempt == 2
    assert status in ("implementing", "testing", "reviewing"), status


def test_stage_times_are_ordered_and_burn_only_goes_up():
    for key, stages in sp.STAGES.items():
        times = [t for t, *_ in stages]
        assert times == sorted(times), key
        burns = [b for _t, _s, _l, b, *_x in stages]
        assert burns == sorted(burns), key
        turns = [n for *_x, n, _a in stages]
        assert turns == sorted(turns), key


def test_updated_at_stays_inside_the_minute():
    """The revision stamp is seconds appended to NOW's minute; a task with
    more revisions than seconds would wrap into invalid timestamps."""
    for key in sp.CHOSEN:
        final = sp._revision(key, nr.DURATION)
        assert final <= 59, (key, final)
        stamp = sp.updated_at(key, nr.DURATION)
        assert re.fullmatch(r"2026-08-03T09:14:\d{2}\+00:00", stamp), stamp


def test_the_board_is_never_empty_and_the_hero_leads_the_handoff():
    assert len(sp.board_at(0.0)) >= 1        # the leftover PR
    assert len(sp.board_at(sp.PARALLEL)) == 6
    created = [sp.STAGES[key][0][0] for key in sp.CHOSEN]
    assert created[0] == min(created), "the hero lands first"


def test_the_hero_is_approved_on_camera_and_only_the_hero():
    assert sp.PAYOFF < sp.APPROVE_AT < nr.FADE_OUT_START
    assert sp.approved_at(sp.HERO_KEY, sp.APPROVE_AT) == sp.NOW
    assert sp.approved_at(sp.HERO_KEY, sp.APPROVE_AT - 1) is None
    for key in sp.CHOSEN:
        if key != sp.HERO_KEY:
            assert sp.approved_at(key, nr.DURATION) is None, key


def test_the_approved_state_is_on_screen_long_enough_to_read():
    assert nr.FADE_OUT_START - sp.APPROVE_AT >= 2.0


# --------------------------------------------------------------------------- #
# The hero's evidence — same bar as the 26 s cut                               #
# --------------------------------------------------------------------------- #


def _diff_files() -> dict[str, list[str]]:
    """sp.DIFF applied — {path: 1-indexed new-file lines} — the same parser
    test_demo_video.py uses on fixture.DIFF, re-derived here so a citation
    can be checked against what a viewer of the gate beat can actually see."""
    files: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in sp.DIFF.splitlines():
        if raw.startswith("+++ b/"):
            current = files.setdefault(raw[len("+++ b/"):], [""])
            continue
        if raw.startswith("@@"):
            new = raw.split("+", 1)[1].split(" ", 1)[0]
            start = int(new.split(",")[0])
            assert current is not None
            while len(current) < start:
                current.append("<unchanged>")
            continue
        if current is None or raw.startswith(("diff --git", "index ", "--- ")):
            continue
        if raw.startswith("+"):
            current.append(raw[1:])
        elif raw.startswith(" "):
            current.append(raw[1:])
    return files


def test_the_diff_is_a_diff_of_the_files_the_plan_named():
    assert sorted(_diff_files()) == sorted(sp.SPEC["files_to_change"])
    assert "test_webhooks.py" in _diff_files(), \
        "the checklist cites a regression test"


@pytest.mark.parametrize("item", sp.REVIEW_CHECKLIST["items"],
                         ids=[i["file"] + ":" + str(i["line"])
                              for i in sp.REVIEW_CHECKLIST["items"]])
def test_every_line_the_review_cites_is_a_line_of_the_diff(item):
    files = _diff_files()
    assert item["file"] in files, item
    lines = files[item["file"]]
    assert item["line"] < len(lines), (item, len(lines) - 1)
    assert lines[item["line"]] != "<unchanged>", item
    assert f"{item['file']}:{item['line']}" in item["evidence"], item
    quoted = {("test_webhooks.py", 36): "assert Charge.count() == 1",
              ("webhooks.py", 15): "return Response(status=200)",
              ("test_webhooks.py", 33): "test_replay_502_charges_once"}
    want = quoted.get((item["file"], item["line"]))
    if want:
        assert want in item["evidence"], item
        assert want in lines[item["line"]], (want, lines[item["line"]])


def test_the_review_passed_with_something_left_to_read():
    assert sp.REVIEW_CHECKLIST["passed"] is True
    graded = [i for i in sp.REVIEW_CHECKLIST["items"] if not i["passed"]]
    assert graded, "an all-green checklist renders no evidence text"
    assert all(i.get("severity") in ("low", "nit") for i in graded)
    assert all(i.get("evidence") for i in sp.REVIEW_CHECKLIST["items"])


def test_every_line_of_the_diff_fits_the_shell_pane():
    for line in sp.DIFF.splitlines():
        assert len(line) <= fx.DIFF_PANE_COLS, (len(line), line)


def _survives_the_serialisers(event: dict) -> bool:
    """The allow-list both shipped serialisers carry — the same reproduction
    test_demo_video.py holds fixture.EVENT_TRAIL to."""
    source, kind = event["source"], event["kind"]
    if source in ("orchestrator", "watcher", "human") or \
            kind in ("result", "error"):
        return True
    is_agent = (source == "agent" or source == "aggregator"
                or source.startswith("planner"))
    return is_agent and (
        kind in ("tool_use", "tool_result", "text", "agent_text", "thinking")
        or kind.startswith("subagent_"))


@pytest.mark.parametrize("event", [e for _due, e in sp.EVENT_TRAIL],
                         ids=[f"{i}-{e['kind']}"
                              for i, (_d, e) in enumerate(sp.EVENT_TRAIL)])
def test_every_scripted_event_is_one_the_product_would_serialise(event):
    assert event["source"] != "reviewer"
    assert _survives_the_serialisers(event), event


def test_every_detail_pane_line_fits_or_wraps_deliberately():
    """`* kind text`, 50 usable columns — the same measurement as
    test_demo_video.py, models exempted at <= 3 wrapped rows."""
    for _due, event in sp.EVENT_TRAIL:
        rendered = f"* {event['kind']} {event['text']}"
        if event["kind"] == "models":
            assert -(-len(rendered) // 50) <= 3, rendered
            continue
        assert len(rendered) <= 50, (len(rendered), rendered)


def test_the_hero_visits_each_stage_once():
    """The trail anchors every frame to a stage BY STATUS, which is only a
    usable address while the hero's pipeline visits each status once. ORD-2187
    is the ticket that bounces; the hero must not become a second one without
    someone re-reading `_build_trail`."""
    statuses = [status for _t, status, *_ in sp.STAGES[sp.HERO_KEY]]
    assert len(statuses) == len(set(statuses)), statuses
    assert set(sp.STREAMED_STATES) <= set(statuses)
    assert sp.RUN_START == sp.STAGES[sp.HERO_KEY][1][0]


def test_the_trail_is_rebuilt_from_the_stage_table_not_written_beside_it():
    """The defect this file now guards: `_TRAIL` held ABSOLUTE seconds, written
    against an older `STAGES` and never retimed when it moved.

    Re-deriving the anchors from `STAGES[HERO_KEY]` here — rather than reading
    `sp._HERO_STAGE_START`, which would compare a value to itself — and
    rebuilding the trail from them must reproduce the shipped trail exactly. A
    hand-typed second anywhere in it fails this.
    """
    starts = {status: t for t, status, *_ in sp.STAGES[sp.HERO_KEY]}
    run_start = next(t for t, status, *_ in sp.STAGES[sp.HERO_KEY]
                     if status != "pending")
    assert sp._build_trail(starts, run_start) == sp._TRAIL


def test_the_trail_moves_when_the_stage_table_moves():
    """The mutation the test above cannot make on its own: shift every stage of
    the hero by a constant and every frame of the trail must shift with it.

    Without this, a trail that happened to agree with today's `STAGES` by
    coincidence — the exact state the file shipped in — passes.
    """
    shift = 5.0
    starts = {status: t + shift for t, status, *_ in sp.STAGES[sp.HERO_KEY]}
    run_start = sp.RUN_START + shift
    moved = sp._build_trail(starts, run_start)
    assert len(moved) == len(sp._TRAIL)
    for (was, a), (now, b) in zip(sp._TRAIL, moved):
        assert a == b, (a, b)
        assert round(now - was, 2) == shift, (was, now)


def test_the_trail_never_contradicts_the_card_behind_it():
    """THE defect. The drawer puts the event stream and the card's own status
    chip on screen together, and they are two readouts of one quantity.

    They disagreed twice in the shipped cut: `state context` was emitted at
    17.90 while the chip had read "gathering context" since 16.40, and there
    was NO `state testing` frame at all — so for the 5.4 s from 33.00 to 38.45
    the chip said "Run `pytest -q`" over an agent board still showing
    `implementing`.

    Checked on EVERY FRAME of the clip, not at the beats, with the two
    exemptions named rather than assumed:
      * while the fixture says `pending`, the agent has said nothing yet, so
        the stream must carry no state at all;
      * `awaiting_approval` is the orchestrator parking a finished attempt and
        is not a state the agent announces — the stream must by then say
        `reviewing` AND have carried the `pr_open` frame that ends the run.
    """
    seen_states, seen_awaiting = set(), 0
    for frame in range(nr.N_FRAMES):
        t = nr.t_of(frame)
        stage = sp.stage_at(sp.HERO_KEY, t)
        status = stage[0] if stage else None
        streamed = sp.trail_state_at(t)
        if status in (None, "pending"):
            assert streamed is None, (t, status, streamed)
        elif status == "awaiting_approval":
            seen_awaiting += 1
            assert streamed == "reviewing", (t, streamed)
            assert any(f["kind"] == "pr_open" for f in sp.events_at(t)), t
        else:
            seen_states.add(status)
            assert streamed == status, (t, status, streamed)
    # Non-vacuity: the loop must actually have exercised all three arms.
    assert seen_states == set(sp.STREAMED_STATES), seen_states
    assert seen_awaiting > 0


def test_every_trail_frame_lands_inside_the_stage_it_was_written_for():
    """The per-frame version of the check above: a tool call attributed to
    `planning` must be ON SCREEN during planning. An offset that overruns its
    stage puts the planner's Grep under the coder's banner."""
    starts = {status: t for t, status, *_ in sp.STAGES[sp.HERO_KEY]}
    for status, offset, frame in sp._STAGE_FRAMES:
        assert offset >= 0, (status, offset)
        due = round(starts[status] + offset, 2)
        assert sp.stage_at(sp.HERO_KEY, due)[0] == status, (
            frame["kind"], frame["text"], due, status)
    # The claim frames are the other side of it: they belong to the last moment
    # of the QUEUE, before the worker has announced anything.
    for offset, frame in sp._CLAIM_FRAMES:
        due = round(sp.RUN_START + offset, 2)
        assert offset < 0, (frame["kind"], offset)
        assert sp.stage_at(sp.HERO_KEY, due)[0] == "pending", (frame, due)


def test_the_cli_stream_opens_before_its_first_frame():
    """The second half of the same defect, on the shell.

    `SprintClient.stream_events` awaits `FrameClock.until(due)` per frame and
    `until` returns immediately for anything already past — so a stream opened
    late yields its whole backlog into ONE captured frame. Opening it before
    the first frame is due makes the backlog empty by construction.
    """
    assert sprint_cli.SELECT_AT < sp.TRAIL_START
    assert sp.TRAIL_START == min(due for due, _e in sp.EVENT_TRAIL)
    script = sprint_cli.build_script()
    selects = [(t, p) for t, k, p in script if k == "select"]
    assert selects == [(sprint_cli.SELECT_AT, sp.HERO_ID)]
    # The hero card has to exist before the shell can follow it.
    assert sp.STAGES[sp.HERO_KEY][0][0] < sprint_cli.SELECT_AT


def test_no_two_trail_frames_land_on_the_same_captured_frame():
    """The other way a stream becomes a dump: two frames closer together than
    one frame of video. `FrameClock.until` rounds to a frame index, so two
    frames that round to the same index are delivered together no matter when
    the stream opened."""
    indexes = [int(round(due * nr.FPS)) for due, _e in sp.EVENT_TRAIL]
    assert len(indexes) == len(set(indexes)), [
        (due, e["kind"]) for due, e in sp.EVENT_TRAIL]


def test_the_trail_is_quiet_before_the_shell_reads_its_own_pane():
    """`/diff` is typed at 44.50; a trail frame after that scrolls the diff
    (then the logs) off camera — the exact hazard fixture.py documents."""
    last = max(due for due, _e in sp.EVENT_TRAIL)
    assert last < 44.50, last
    trail_times = [due for due, _e in sp.EVENT_TRAIL]
    assert trail_times == sorted(trail_times)


def test_the_agent_board_has_a_frame_for_every_role_it_draws():
    emits_for = {"supervisor_decision": "supervisor", "tamper": "reviewer",
                 "review_start": "reviewer", "review": "reviewer",
                 "review_error": "reviewer", "supervisor": "supervisor"}

    def node(e):
        src = e["source"]
        if src == "orchestrator":
            return emits_for.get(e["kind"], "worker")
        if src == "aggregator" or src.startswith("planner"):
            return "planner"
        return src

    seen = {node(e) for _due, e in sp.EVENT_TRAIL}
    assert {"worker", "planner", "agent", "supervisor", "reviewer"} <= seen


def test_hero_detail_serves_evidence_only_when_it_exists():
    before_plan = sp.hero_detail(20.0)      # planning until 21.00
    assert "spec" not in before_plan["context"]
    assert sp.hero_detail(22.0)["context"]["spec"] == sp.SPEC
    running = sp.hero_detail(sp.GATE)       # reviewing at 38.40
    assert running["attempts"][0]["review_checklist"] is None
    done = sp.hero_detail(sp.PAYOFF)
    assert done["attempts"][0]["review_checklist"] == sp.REVIEW_CHECKLIST
    assert done["attempts"][0]["test_results"]["tamper_flag"] is False


# --------------------------------------------------------------------------- #
# The recorders — functions of the map                                         #
# --------------------------------------------------------------------------- #


def test_the_cli_script_is_ordered_and_inside_the_clip():
    script = sprint_cli.build_script()
    times = [t for t, _k, _p in script]
    assert times == sorted(times)
    assert 0 < times[0] and times[-1] < nr.DURATION


def test_the_cli_script_repaints_on_every_fixture_change():
    script = sprint_cli.build_script()
    refreshes = {t for t, kind, _p in script if kind == "refresh"}
    for key, stages in sp.STAGES.items():
        for start, *_ in stages:
            assert start + 0.05 in refreshes, (key, start)


def test_the_cli_selects_the_hero_before_the_gate_needs_its_pane():
    script = sprint_cli.build_script()
    selects = [(t, p) for t, k, p in script if k == "select"]
    # WHICH task and HOW MANY times; WHEN is
    # `test_the_cli_stream_opens_before_its_first_frame`'s, and is derived from
    # the trail rather than written down here as a second copy of it.
    assert [p for _t, p in selects] == [sp.HERO_ID]
    assert selects[0][0] < sp.PARALLEL, selects
    # /diff is typed after the trail has gone quiet, /approve lands on the
    # fixture's approval second.
    diff_keys = [t for t, k, p in script if k == "key" and p == "/"[0]]
    assert diff_keys[0] > max(due for due, _e in sp.EVENT_TRAIL)
    enters = [t for t, k, _p in script if k == "enter"]
    assert any(abs(t - (sp.APPROVE_AT + 0.05)) < 1e-9 for t in enters)


def test_the_cli_types_the_two_commands_the_beats_are_about():
    script = sprint_cli.build_script()
    typed = "".join(p for _t, k, p in script if k == "key")
    assert "/diff" in typed and "/approve" in typed
    submits = [p for _t, k, p in script if k == "submit"]
    assert "/logs" in submits


def test_the_gui_schedule_is_ordered_and_anchored():
    order = [sprint_gui.EXPAND_LANE_AT, sprint_gui.OPEN_DRAWER_AT,
             sprint_gui.SPEC_AT, sprint_gui.SYSTEM_AT, sprint_gui.DIFF_AT,
             sprint_gui.REVIEW_AT, sprint_gui.CLOSE_DRAWER_AT,
             sprint_gui.REOPEN_DRAWER_AT, sp.APPROVE_AT,
             sprint_gui.CLOSE2_AT]
    assert order == sorted(order)
    # The lane expands only after the last ticket exists, before "five
    # agents start" is spoken.
    last_created = max(stages[0][0] for stages in sp.STAGES.values())
    assert last_created < sprint_gui.EXPAND_LANE_AT < sp.PARALLEL
    # The Spec section opens only once the hero's plan exists.
    assert sp.stage_at(sp.HERO_KEY, sprint_gui.SPEC_AT)[0] not in (
        "pending", "context", "planning")
    # The Diff section opens only after the trail's commit frame.
    commit_at = next(due for due, e in sp.EVENT_TRAIL
                     if e["kind"] == "commit")
    assert sprint_gui.DIFF_AT > commit_at
    # The Review section opens only once the checklist exists.
    assert sp.stage_at(sp.HERO_KEY, sprint_gui.REVIEW_AT)[0] == \
        "awaiting_approval"
    # The drawer is closed while the bounce and the five PRs are narrated.
    bounce_line = _line_saying("did not pass")
    five_prs_line = _line_saying("five pull requests")
    for t in (bounce_line.start, five_prs_line.start):
        assert sprint_gui.CLOSE_DRAWER_AT <= t < sprint_gui.REOPEN_DRAWER_AT


def test_the_gui_pin_follows_the_open_section_and_only_then():
    assert sprint_gui.pin_at(sprint_gui.SPEC_AT + 0.5) == \
        sprint_gui.PIN_BY_BEAT["spec"]
    assert sprint_gui.pin_at(sprint_gui.SYSTEM_AT + 0.5) == \
        sprint_gui.PIN_BY_BEAT["system"]
    assert sprint_gui.pin_at(sprint_gui.DIFF_AT + 0.5) == \
        sprint_gui.PIN_BY_BEAT["diff"]
    assert sprint_gui.pin_at(sprint_gui.REVIEW_AT + 0.5) == \
        sprint_gui.PIN_BY_BEAT["review"]
    assert sprint_gui.pin_at(sprint_gui.REOPEN_DRAWER_AT + 1.0) == \
        sprint_gui.PIN_BY_BEAT["review"]
    for t in (5.0, sprint_gui.CLOSE_DRAWER_AT + 0.5,
              sprint_gui.CLOSE2_AT + 1.0):
        assert sprint_gui.pin_at(t) is None, t


# --------------------------------------------------------------------------- #
# Voiceover — the audio is a function of the map                               #
# --------------------------------------------------------------------------- #

SAY_LISTING_COMPACT = """\
Albert              en_US    # Hello! My name is Albert.
Anna                de_DE    # Hallo! Ich heisse Anna.
Daniel              en_GB    # Hello! My name is Daniel.
Karen               en_AU    # Hello! My name is Karen.
Samantha            en_US    # Hello! My name is Samantha.
"""

SAY_LISTING_PREMIUM = SAY_LISTING_COMPACT + """\
Ava (Premium)       en_US    # Hello! My name is Ava.
Zoe (Enhanced)      en_US    # Hello! My name is Zoe.
Serena (Premium)    en_GB    # Hello! My name is Serena.
"""


def test_voice_parsing_reads_names_and_locales():
    voices = voiceover.parse_voices(SAY_LISTING_PREMIUM)
    assert ("Samantha", "en_US") in voices
    assert ("Ava (Premium)", "en_US") in voices
    assert ("Anna", "de_DE") in voices


def test_voice_pick_prefers_premium_then_samantha():
    assert voiceover.pick_voice(
        voiceover.parse_voices(SAY_LISTING_PREMIUM)) == "Ava (Premium)"
    assert voiceover.pick_voice(
        voiceover.parse_voices(SAY_LISTING_COMPACT)) == "Samantha"
    with pytest.raises(SystemExit):
        voiceover.pick_voice([("Anna", "de_DE")])


def test_check_fit_passes_lines_that_fit_and_names_the_one_that_does_not():
    windows = [w for _ln, w in nr.line_windows()]
    voiceover.check_fit([w - 0.5 for w in windows])   # all fit: no raise
    bad = [w - 0.5 for w in windows]
    bad[3] = windows[3] + 1.0
    with pytest.raises(SystemExit) as err:
        voiceover.check_fit(bad)
    assert f"at {nr.LINES[3].start:.2f}s" in str(err.value)


def test_the_mix_places_every_line_at_its_map_offset():
    files = [Path(f"/x/line{i:02d}.aiff") for i in range(len(nr.LINES))]
    args = voiceover.mix_args(files, Path("/x/narration.m4a"))
    joined = " ".join(args)
    assert joined.count("-i ") == len(nr.LINES)
    for line in nr.LINES:
        assert f"adelay={int(round(line.start * 1000))}:all=1" in joined, \
            line.start
    assert f"-t {nr.DURATION:.3f}" in joined
    assert "amix=inputs=%d:normalize=0" % len(nr.LINES) in joined


# --------------------------------------------------------------------------- #
# Compose — both clips cut to one map, one audio over both                     #
# --------------------------------------------------------------------------- #


def test_beat_index_walks_the_narration():
    assert compose.beat_index(0.0) == 0
    assert compose.beat_index(nr.BEATS[2].start + 0.01) == 2
    assert compose.beat_index(nr.DURATION) == len(nr.BEATS) - 1


def test_the_composite_scales_into_the_same_frame_as_the_26s_cut():
    args = compose.composite_args(Path("/a/f0000.png"), Path("/b/f0000.png"),
                                  Path("/c/bar0.png"))
    graph = args[args.index("-filter_complex") + 1]
    assert f"scale={compose.APP_W}:{compose.APP_H}" in graph
    assert f"pad={compose.OUT_W}:{compose.OUT_H}" in graph
    assert "overlay=0:%d" % compose.APP_H in graph


def test_the_encode_muxes_the_narration_and_fades_on_the_maps_times():
    args = compose.encode_args(Path("/stage"), Path("/a/narration.m4a"),
                               Path("/out/demo-sprint-gui.mp4"), 24)
    joined = " ".join(args)
    assert "/a/narration.m4a" in joined
    assert "-c:a aac" in joined and "-shortest" in joined
    assert f"st={nr.FADE_OUT_START}" in joined
    assert f"afade=t=out:st={nr.FADE_OUT_START}" in joined


def test_the_side_by_side_stacks_both_clips_under_one_track():
    args = compose.sxs_args(Path("/g"), Path("/c"), Path("/a/narration.m4a"),
                            Path("/out/demo-sprint-sxs.mp4"))
    joined = " ".join(args)
    assert "hstack=inputs=2" in joined
    assert joined.count("f%04d.png") == 2
    assert "-map 2:a" in joined and "/a/narration.m4a" in joined


# --------------------------------------------------------------------------- #
# The header chip and the poll shim — the board on the frame clock             #
# --------------------------------------------------------------------------- #


def test_the_chip_text_mirrors_the_fixture_at_every_beat():
    """`chip_text_at` is what the recorder holds the live header to, so it has
    to be the fixture's own counts and not a second opinion about them."""
    for beat in nr.BEATS:
        health = sp.queue_health_at(beat.start)
        assert sprint_gui.chip_text_at(beat.start) == \
            f"{health['workers_busy']}/{health['max_workers']} workers busy"
    # The three moments the chip is actually read on camera.
    assert sprint_gui.chip_text_at(sp.PARALLEL - 1.0) == "0/5 workers busy"
    assert sprint_gui.chip_text_at(sp.PARALLEL + 3.0) == "5/5 workers busy"
    assert sprint_gui.chip_text_at(sp.PAYOFF + 1.0) == "0/5 workers busy"


def test_the_chip_never_claims_a_worker_the_header_does_not_show():
    """The bug this shipped with: the polled chip said "1/5 workers busy" at
    57.0 s while the socket-pushed header said "0 running" — two numbers from
    the same fixture disagreeing in the same header row. They are the same
    quantity and must agree on every frame of the clip."""
    for frame in range(nr.N_FRAMES):
        t = nr.t_of(frame)
        assert sprint_gui.chip_text_at(t).startswith(
            f"{sp.inflight_at(t)}/"), t


def _node_or_fail():
    """Node absence FAILS rather than skips — the same reasoning as
    test_lane_conformance.py: a skip here is indistinguishable from a pass,
    and the shim is JS whose behaviour cannot be checked from Python."""
    node = shutil.which("node")
    assert node is not None, (
        "node is not on PATH, so POLL_SHIM's behaviour cannot be checked. "
        "This repo already requires node (`node --test src/` in web/); this "
        "test fails rather than skips because a skipped guard is a hole.")
    return node


#: The shim expects a browser: a `window`, and a `fetch` to wrap. Node gives
#: neither in the shape it wants, so the harness supplies both — and supplies
#: a fetch it can hold open, which is the only way to observe the in-flight
#: counter the recorder blocks on.
POLL_SHIM_HARNESS = """\
globalThis.window = globalThis;
let release = null;
globalThis.fetch = () => new Promise((r) => { release = r; });

%(shim)s

function fail(why) { console.log('FAIL ' + why); process.exit(1); }

// A long poll (App.jsx's 10 s worker/queue fetch) and a short one (anything
// that animates). Only the long one may be parked.
const log = [];
const long = setInterval(() => log.push('long'), 10000);
const short = setInterval(() => log.push('short'), 30);
if (long >= 0) fail('the 10 s poll was left on wall time');
if (short <= 0) fail('a 30 ms interval was stolen — animation would freeze');
clearInterval(short);
if (log.length !== 0) fail('a parked poll ran without a tick');

__nhTick();
__nhTick();
if (log.join(',') !== 'long,long') fail('tick did not fire the poll: ' + log);

clearInterval(long);
__nhTick();
if (log.length !== 2) fail('a cleared poll still fires');

// The in-flight counter: idle at rest, busy while a fetch is open, idle again
// once it settles.
if (!__nhIdle()) fail('not idle at rest');
const inflight = window.fetch('/api/queue/health');
if (__nhIdle()) fail('reported idle while a fetch was open');
release({});
inflight.then(() => {
  if (!__nhIdle()) fail('never returned to idle');
  console.log('OK');
});
"""


def test_the_poll_shim_parks_long_intervals_and_leaves_short_ones(tmp_path):
    """POLL_SHIM is the only new JS in the piece and it decides whether the
    header tells the truth, so it is executed, not eyeballed."""
    node = _node_or_fail()
    script = tmp_path / "shim.mjs"
    script.write_text(POLL_SHIM_HARNESS % {"shim": sprint_gui.POLL_SHIM})
    proc = subprocess.run([node, str(script)], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0 and "OK" in proc.stdout, \
        proc.stdout + proc.stderr


# --------------------------------------------------------------------------- #
# verify_sync — reading the finished mp4 back                                  #
# --------------------------------------------------------------------------- #

#: silencedetect's real output shape, trimmed. The last pair is the trailing
#: silence, whose "end" is end-of-stream and not a sentence starting.
SILENCE_LOG = """\
[Parsed_silencedetect_0 @ 0x1] silence_start: 0
[Parsed_silencedetect_0 @ 0x1] silence_end: 0.802268 | silence_duration: 0.802268
[Parsed_silencedetect_0 @ 0x1] silence_start: 3.838503
[Parsed_silencedetect_0 @ 0x1] silence_end: 4.401497 | silence_duration: 0.562993
[Parsed_silencedetect_0 @ 0x1] silence_start: 74.806372
[Parsed_silencedetect_0 @ 0x1] silence_end: 77.995828 | silence_duration: 3.189456
"""


def test_the_silence_probe_does_not_silence_the_thing_it_reads():
    """silencedetect reports at INFO level. `-v error`, the reflex everywhere
    else in this package, returns an empty log — which this module would read
    as "no speech anywhere in the clip" and, one-way, as a pass. The absence
    of that flag is the test."""
    args = vs.silence_args(Path("/x/demo.mp4"))
    assert "error" not in args, args
    assert "-hide_banner" in args and "-nostats" in args
    joined = " ".join(args)
    assert f"silencedetect=noise={vs.NOISE_DB}dB:d={vs.MIN_SILENCE}" in joined
    assert "-map 0:a" in joined and "-f null" in joined


def test_speech_onsets_are_silence_ends_without_the_end_of_stream():
    onsets = vs.speech_onsets(SILENCE_LOG, 77.996)
    assert onsets == (0.802268, 4.401497)


def test_a_line_that_never_gets_spoken_is_reported_missing():
    starts = tuple(ln.start for ln in nr.LINES)
    matches, orphans = vs.match_onsets(starts)
    assert orphans == ()
    assert all(m.onset is not None for m in matches)
    assert max(abs(m.delta) for m in matches) == 0.0
    # Drop the gate's first line from the track: only that line reports.
    missing = tuple(t for t in starts if t != nr.LINES[8].start)
    matches, orphans = vs.match_onsets(missing)
    assert orphans == ()
    assert [m.line.start for m in matches if m.onset is None] == \
        [nr.LINES[8].start]


def test_speech_the_map_does_not_account_for_is_an_orphan():
    """The one-way version of this check — "every line has an onset" — passes
    a track with an extra sentence bleeding into the next beat. This is the
    other direction."""
    starts = tuple(ln.start for ln in nr.LINES)
    _matches, orphans = vs.match_onsets(starts + (33.0,))
    assert orphans == (33.0,)


def test_an_onset_outside_tolerance_does_not_count_as_on_time():
    late = (nr.LINES[0].start + vs.ONSET_TOL * 2,) + \
        tuple(ln.start for ln in nr.LINES[1:])
    matches, orphans = vs.match_onsets(late)
    assert matches[0].onset is None
    assert orphans == (nr.LINES[0].start + vs.ONSET_TOL * 2,)
    # Just inside the tolerance is on time, and the delta is reported signed.
    near = (nr.LINES[0].start + vs.ONSET_TOL * 0.5,) + \
        tuple(ln.start for ln in nr.LINES[1:])
    matches, orphans = vs.match_onsets(near)
    assert orphans == ()
    assert matches[0].delta == pytest.approx(vs.ONSET_TOL * 0.5)


def test_the_image_metric_is_zero_only_for_the_same_image():
    assert vs.mean_abs_diff(b"\x10\x20", b"\x10\x20") == 0.0
    assert vs.mean_abs_diff(b"\x00\x00", b"\x02\x04") == 3.0
    with pytest.raises(ValueError):
        vs.mean_abs_diff(b"\x00", b"\x00\x00")
    with pytest.raises(ValueError):
        vs.mean_abs_diff(b"", b"")


def test_the_caption_match_names_the_nearest_strip_and_its_margin():
    bars = [b"\x00\x00", b"\x10\x10", b"\x40\x40"]
    idx, dist, margin = vs.best_bar(b"\x12\x12", bars)
    assert idx == 1
    assert dist == 2.0
    assert margin == pytest.approx(16.0)     # runner-up (bar0) is 18 away


def test_the_band_crop_reads_the_caption_strip_of_the_named_pane():
    """The side-by-side is two panes hstacked, so the cli pane is the same
    crop one app-width to the right — which is what makes "do both panes name
    the same beat" a meaningful question to ask of one file."""
    from e2e.demo_video.camera import APP_H, APP_W, BAR_H
    gui = " ".join(vs.band_args(Path("/x.mp4"), 57.0, 0))
    assert f"crop={APP_W}:{BAR_H}:0:{APP_H}" in gui
    assert "-ss 57.000" in gui and "-pix_fmt rgb24" in gui
    cli = " ".join(vs.band_args(Path("/x.mp4"), 57.0, APP_W))
    assert f"crop={APP_W}:{BAR_H}:{APP_W}:{APP_H}" in cli
    assert vs.PANES["sxs"] == (("gui", 0), ("cli", APP_W))
    assert vs.PANES["gui"] == (("gui", 0),) and vs.PANES["cli"] == (("cli", 0),)


def test_the_app_crop_reads_the_screen_not_the_caption_strip():
    """The caption band is burned in by compose from the same beat table the
    band check then compares against, so on its own it proves the caption
    pipeline and not the picture — an app area 20 s out of step with a correct
    strip passed the first version of this module. This crop is the one that
    looks at the screen: everything ABOVE the strip, at y=0."""
    from e2e.demo_video.camera import APP_H, APP_W
    gui = " ".join(vs.app_args(Path("/x.mp4"), 57.0, 0))
    assert f"crop={APP_W}:{APP_H}:0:0" in gui
    assert f"scale={vs.THUMB_W}:{vs.THUMB_H}" in gui
    assert "-ss 57.000" in gui and "-pix_fmt rgb24" in gui
    # It must not be reading the band the other check already reads.
    assert f"crop={APP_W}:{vs.BAR_H}" not in gui
    cli = " ".join(vs.app_args(Path("/x.mp4"), 57.0, APP_W))
    assert f"crop={APP_W}:{APP_H}:{APP_W}:0" in cli


def test_the_reference_frame_is_thumbnailed_to_match_the_app_crop():
    """Both sides of the comparison have to land at the same geometry or
    mean_abs_diff raises rather than compares."""
    ref = " ".join(vs.source_frame_args(Path("/f0000.png")))
    assert f"scale={vs.THUMB_W}:{vs.THUMB_H}" in ref
    assert "-pix_fmt rgb24" in ref
    assert "-ss" not in ref, "a source frame is addressed by index, not time"


def test_the_grid_samples_the_whole_clip_at_a_known_spacing():
    """The count is the claim. An earlier version sampled six instants while
    the docstring described beat-scale coverage of the clip; the number the
    tool prints and the number it checks are now the same one."""
    g = vs.grid()
    assert len(g) == 38
    assert g[0] == vs.GRID_STEP
    assert all(round(b - a, 3) == vs.GRID_STEP for a, b in zip(g, g[1:]))
    # Never inside a fade, where the picture is a black mix and matches
    # everything, and never past the end of the capture.
    assert g[0] > nr.FADE_IN_END and g[-1] < nr.FADE_OUT_START
    assert vs.grid(duration=10.0, step=2.0) == (2.0, 4.0, 6.0, 8.0)


def test_the_offset_scan_names_the_shift_that_explains_the_frame():
    refs = {8.0: b"\x00\x00", 10.0: b"\x10\x10", 12.0: b"\x40\x40"}
    # A frame that IS the 12.0 s capture, sampled at 10.0 s -> +2.0 s slip.
    off, dist, band = vs.offset_scan(b"\x40\x40", refs, 10.0, step=2.0,
                                     steps=1, noise=0.25)
    assert off == 2.0 and dist == 0.0
    assert band == (2.0,)
    assert 0.0 not in band, "this is what check_app_area fails on"


def test_the_resolution_band_is_the_measured_limit_not_a_claim():
    """On a screen that is not moving every candidate ties, the argmin is
    decided by encoder jitter, and the honest output is a WIDE BAND rather
    than a confident wrong answer. Asserting the argmin instead made the
    genuine shipped cut fail 52 times."""
    static = {8.0: b"\x10\x10", 10.0: b"\x10\x10", 12.0: b"\x10\x10"}
    off, _dist, band = vs.offset_scan(b"\x10\x10", static, 10.0, step=2.0,
                                      steps=1, noise=0.25)
    assert band == (-2.0, 0.0, 2.0), band
    assert 0.0 in band, "a static screen must not be reported as a slip"
    # Moving screen: the band collapses to the true offset alone.
    moving = {8.0: b"\x00\x00", 10.0: b"\x10\x10", 12.0: b"\x40\x40"}
    _off, _d, sharp = vs.offset_scan(b"\x10\x10", moving, 10.0, step=2.0,
                                     steps=1, noise=0.25)
    assert sharp == (0.0,)


def test_the_band_is_printed_as_a_range_a_reader_can_act_on():
    assert vs._band_str((0.0,)) == "+0.0"
    assert vs._band_str((-2.0, 0.0, 6.0)) == "-2.0..+6.0"
    # An EMPTY band means nothing was measured and must SAY so. `min()` on it
    # raised ValueError and took the whole gate down mid-run — measured on a
    # real proof render. A crash reports neither pass nor failure, which in a
    # module whose whole stance is fail-closed is worse than either.
    assert "nothing measured" in vs._band_str(())


def test_the_app_check_given_nothing_to_sample_says_so(tmp_path):
    """A check that looked at nothing must never be why a build is green."""
    problems = vs.check_app_area(Path("/x/demo.mp4"), tmp_path,
                                 vs.PANES["gui"], ())
    assert len(problems) == 1 and "proved nothing" in problems[0]


def test_the_app_check_cannot_be_skipped_by_omission():
    """`verify` used to default `work` to None and skip the app area silently,
    which in a module that fails closed everywhere else made the strongest
    check the easiest one to lose. It is now positional and required."""
    import inspect
    params = inspect.signature(vs.verify).parameters
    assert params["work"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        vs.verify(Path("/x.mp4"), Path("/bars"), vs.PANES["gui"])


def test_a_missing_reference_capture_fails_closed(tmp_path):
    """The app check is the only one whose reference the encoder cannot
    manufacture, so it depends on the capture still being on disk. When it is
    not, the check must report a PROBLEM — a silently skipped proof reads
    exactly like a proof that passed."""
    problems = vs.check_app_area(Path("/x/demo.mp4"), tmp_path,
                                 vs.PANES["sxs"], [1.6, 57.0])
    assert len(problems) == 2, problems
    assert all("is missing" in p for p in problems)
    assert any("frames-gui" in p for p in problems)
    assert any("frames-cli" in p for p in problems)


# --------------------------------------------------------------------------- #
# verify_sync — the freeze gate                                                #
# --------------------------------------------------------------------------- #

#: freezedetect's real output shape. The LAST freeze deliberately has no
#: `freeze_end`: a hold that runs to end-of-stream is never closed, and that is
#: the hold a cut is most likely to have.
FREEZE_LOG = """\
[Parsed_freezedetect_0 @ 0x1] lavfi.freezedetect.freeze_start: 12.5
[Parsed_freezedetect_0 @ 0x1] lavfi.freezedetect.freeze_duration: 3.2
[Parsed_freezedetect_0 @ 0x1] lavfi.freezedetect.freeze_end: 15.7
[Parsed_freezedetect_0 @ 0x1] lavfi.freezedetect.freeze_start: 70
"""


def test_the_freeze_probe_does_not_silence_the_thing_it_reads():
    """Same defect `silence_args` documents, same shape of fix: freezedetect
    reports at INFO level, so `-v error` returns an empty log — which this
    module would read as "nothing was frozen" and, one-way, as a pass."""
    args = vs.freeze_args(Path("/x/demo.mp4"))
    assert "error" not in args, args
    assert "-hide_banner" in args and "-nostats" in args
    joined = " ".join(args)
    assert f"freezedetect=n={vs.FREEZE_NOISE}:d={vs.FREEZE_MIN}" in joined
    assert "-map 0:v" in joined and "-f null" in joined
    # The opening pass is the same filter at a shorter minimum hold.
    opening = " ".join(vs.freeze_args(Path("/x/demo.mp4"),
                                      min_dur=vs.FREEZE_OPENING_MIN))
    assert f"d={vs.FREEZE_OPENING_MIN}" in opening


def test_a_freeze_that_runs_to_the_end_of_the_stream_is_still_a_freeze():
    """The one freezedetect never closes. Dropping it is how this gate would
    have passed the very file it exists to refuse — a cut whose last shot is
    held dead to the fade."""
    found = vs.freezes(FREEZE_LOG, duration=81.0)
    assert found == ((12.5, 3.2), (70.0, 11.0)), found
    # A log with nothing in it means nothing was frozen, not a parse failure.
    assert vs.freezes("", 81.0) == ()


def test_the_opening_rule_can_actually_fire():
    """A rule that cannot fire is not a rule.

    With a single detection pass at `FREEZE_MIN`, "any freeze in the first ten
    seconds" could never catch anything "any freeze >= 2 s" had not already
    caught — it would be dead code that reads like a guarantee. The opening
    pass exists to make it real, so its minimum must be strictly shorter.
    """
    assert vs.FREEZE_OPENING_MIN < vs.FREEZE_MIN
    assert vs.FREEZE_OPENING > 0
    # A 0.8 s hold at 3 s: invisible to the main pass, caught by the opening.
    assert vs.freezes("freeze_start: 3.0\nfreeze_duration: 0.8\n"
                      "freeze_end: 3.8\n", 81.0) == ((3.0, 0.8),)


def test_localized_motion_and_a_global_shift_are_told_apart():
    """`drift_share` is the whole reason the freeze verdict means anything.

    Two frames, 4 pixels each. Localized: one pixel changes a lot. Global: all
    four change by the same small amount — which is what a push-in, a pan or a
    luma ramp does, and what silences freezedetect while nothing happens.
    """
    base = bytes([100, 100, 100] * 4)
    local = bytes([100, 100, 100] * 3 + [200, 200, 200])
    mean, moved = vs.drift_share(base, local)
    assert moved == 0.25
    assert mean == pytest.approx(100 * 3 / 12)

    drift = bytes([112, 112, 112] * 4)
    mean, moved = vs.drift_share(base, drift)
    assert moved == 1.0, "every pixel moved — that is a transform, not a UI"
    assert mean == pytest.approx(12.0)

    # THE CHEAP DRIFT, which is the case the first draft of this check got
    # wrong: one level per frame silences freezedetect (its floor is
    # FREEZE_NOISE, ~0.8/255) while moving NO pixel by `LOCAL_DELTA`. It must
    # land under the floor, not be mistaken for real motion.
    cheap = bytes([101, 101, 101] * 4)
    mean, moved = vs.drift_share(base, cheap)
    assert mean > vs.FREEZE_NOISE * 255, "it does silence freezedetect"
    assert moved < vs.MIN_MOVED_SHARE, "and it moves nothing a viewer sees"

    assert vs.drift_share(base, base) == (0.0, 0.0)
    with pytest.raises(ValueError):
        vs.drift_share(b"\x00", b"\x00\x00")
    with pytest.raises(ValueError):
        vs.drift_share(b"", b"")


def test_the_motion_thresholds_leave_room_between_real_and_manufactured():
    """The two arms must not overlap, or a frame is both dead and global."""
    assert 0 < vs.MIN_MOVED_SHARE < vs.DRIFT_MOVED_MAX < 1.0
    assert vs.LOCAL_DELTA > vs.FREEZE_NOISE * 255, (
        "a pixel must have to move MORE than freezedetect's own floor to "
        "count as moved, or the two instruments are measuring one thing")
    # The window is long enough that a legitimate hold between two UI changes
    # is not read as a dead frame — measured: judging a single consecutive
    # pair failed a genuinely moving clip 5 times in 6.
    assert vs.FREEZE_WINDOW >= 10.0 / nr.FPS


def test_min_moved_share_is_pinned_to_the_probe_s_measured_motion_floor():
    """MIN_MOVED_SHARE=0.001 was "measured, not reasoned" (`_freeze_probe`'s
    own module docstring) but nothing tied it to that measurement: a review
    found that retuning it 500x (to 0.5) still left all 241 demo tests, and
    freezedetect itself, green — only a manual run of `_freeze_probe` (a
    minute-long script nothing runs in CI) caught it, because clip A
    ("moving": a 56px block stepping to a new 32-cell-grid position every 6
    frames — LOCALIZED motion, the shape a real UI change has) started
    reading as "dead" instead of "local":

        MIN_MOVED_SHARE = 0.5  ->  `_freeze_probe`'s own summary:
        A moving     got FAIL  want PASS  WRONG   (verified by hand, see PR)

    This reproduces that measurement in-process, at the SAME geometry
    `check_freeze`'s localization pass uses on every real render — frames
    `FREEZE_WINDOW` apart, `drift_share` at `LOCAL_DELTA` — on the SAME
    frames `_freeze_probe.with_block` generates for clip A (imported
    directly: one source of the adversary, not a copy that can drift out of
    sync with it). It skips `_freeze_probe.build`'s ffmpeg encode: encoding
    is orthogonal to the pixel-level share this constant has to respect, and
    skipping it keeps this test ffmpeg-free like the rest of this file.
    (Checked by hand against a real encode: the post-encode floor for this
    same clip is ~3.9%, WIDER than the ~2.7% measured here — the encoder
    only makes the floor more forgiving, never less, so measuring pre-encode
    is the conservative choice.)

    Only the first 30 frames (one second, spanning ten of the block's 6-frame
    cell transitions) are scanned — confirmed by hand against a full
    240-frame scan of the whole 8 s clip, which lands on the exact same
    minimum, because the moved share this comparison produces turns out to be
    constant across the whole clip: a structural property of the block size
    and grid spacing, not a per-instant fluke a short scan could miss.

    THE ASSERTION: MIN_MOVED_SHARE must be a small, comfortable fraction of
    that measured floor — currently ~3.7% of it. Retuning it "significantly"
    — the ticket's own examples, 500x to 0.5 or 10x to 0.01 — both push that
    ratio far outside a generous [1%, 15%] band and fail here, long before
    anyone has to run `_freeze_probe` by hand to find out.
    """
    window = round(vs.FREEZE_WINDOW * fp.FPS)
    assert window > 0
    scan = min(30, fp.N - window)
    floor = min(
        vs.drift_share(bytes(fp.with_block(i)), bytes(fp.with_block(i + window)))[1]
        for i in range(scan))
    assert floor > 0, (
        "the 'moving' probe clip must contain real motion over the window "
        "for this measurement to mean anything")

    ratio_low, ratio_high = 0.01, 0.15
    ratio = vs.MIN_MOVED_SHARE / floor
    assert ratio_low <= ratio <= ratio_high, (
        f"MIN_MOVED_SHARE ({vs.MIN_MOVED_SHARE}) is {ratio:.1%} of the "
        f"measured local-motion floor ({floor:.4f}) from _freeze_probe's "
        f"'moving' clip — outside the [{ratio_low:.0%}, {ratio_high:.0%}] "
        f"band a silent retune must not leave")

    # The ticket's own examples, made concrete against the same measurement:
    # both retunes must land outside the band above, not just in theory.
    assert not (ratio_low <= 0.5 / floor <= ratio_high), (
        "a 500x retune to 0.5 must fail this pin — it is the exact defect "
        "this test exists to catch")
    assert not (ratio_low <= 0.01 / floor <= ratio_high), (
        "a 10x retune to 0.01 must fail this pin too")


def test_the_freeze_gate_reads_two_frames_a_window_apart_not_one():
    a = " ".join(vs.frame_at_args(Path("/x.mp4"), 10.0))
    b = " ".join(vs.frame_at_args(Path("/x.mp4"), 10.0 + vs.FREEZE_WINDOW))
    assert "-ss 10.000" in a and f"-ss {10.0 + vs.FREEZE_WINDOW:.3f}" in b
    assert f"scale={vs.THUMB_W}:{vs.THUMB_H}" in a
    assert "-frames:v 1" in a and "-pix_fmt rgb24" in a


def test_the_freeze_gate_runs_on_every_verified_clip():
    """It is wired into `verify`, not left as a function nobody calls. The
    published cut was 94% frozen with every other check in this module green;
    a freeze gate that exists and is not invoked would reproduce that exactly.
    """
    import inspect
    src = inspect.getsource(vs.verify)
    assert "check_freeze(" in src, "verify does not run the freeze gate"


def test_the_shipping_cut_refuses_the_fallback_voice(tmp_path, monkeypatch):
    """`say` may RUN the recorder on a bare checkout; it may not PUBLISH.

    voiceover detects the neural engine rather than requiring it, so the
    recorder still works with no node installed — that is deliberate. What is
    not acceptable is that the degraded read reaches an audience, and it did:
    a render made while node was too old for `npx hyperframes` shipped the
    formant read to the public site. Nothing failed, because the file was the
    right length, in sync, correctly captioned, and `voiceover.build` said so
    only in a NOTE that scrolled past in a render that takes minutes.

    So the check belongs on the shipping path and has to be a refusal.
    """
    monkeypatch.setattr(voiceover, "kokoro_available", lambda: False)
    assert voiceover.engine_id() == "say"
    with pytest.raises(SystemExit) as e:
        compose.cut(tmp_path / "work", tmp_path / "out")
    assert "REFUSING" in str(e.value)
    assert "--allow-robot-voice" in str(e.value)


def test_the_refusal_is_the_only_thing_standing_between_say_and_the_site():
    """Mutation guard for the test above. It asserts a refusal, so it passes
    just as happily if the guard is replaced by a raise on every engine — and
    then the recorder cannot cut at all and nobody notices until a render. So
    pin the other side too: with the neural engine reachable, reaching the
    guard must NOT raise, and the override must disarm it.
    """
    import inspect
    src = inspect.getsource(compose.cut)
    guard = [l for l in src.splitlines() if "REFUSING" in l]
    assert guard, "the refusal disappeared from compose.cut"
    cond = [l for l in src.splitlines() if 'engine == "say"' in l]
    assert cond, "the refusal no longer keys on the engine"
    assert "not allow_robot_voice" in cond[0], (
        "the refusal must be overridable, or a bare checkout cannot cut at all")
    assert (inspect.signature(compose.cut)
            .parameters["allow_robot_voice"].default is False), (
        "the override must be off by default, or it guards nothing")
