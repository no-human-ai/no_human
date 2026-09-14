"""No source comment or docstring in `src/` carries an approximate line anchor.

An approximate line anchor is prose like "the check is at ~12034" or
"(db.py ~2884-2892)": a number that means "go read around here". It reads
fine the day it is written and rots on every landing after, because nothing
short of a resolver keeps a bare integer in sync with a file that keeps
growing and shrinking. Nothing validated this syntax before this gate —
`tests/test_readme_claims.py`'s citation machinery covers a DIFFERENT,
disjoint syntax (`file.py:LINE`) in `docs/` only, never `~NNN` prose and
never `src/`.

DECISION: (b), a CONVENTION forbidding approximate line anchors in shipped
source, enforced here by a pytest gate that FAILS (never warns). The
alternative — a drift-tolerant gate that resolves each anchor's target and
fails only once it goes stale — was rejected because the anchors already
shipped are not *prospectively* rotten, they are *already* wrong: resolving
every target this repo carried against its own symbols found 15 of 16
pointing at the wrong function. A drift gate would fail almost immediately
against nearly the whole population and demand exactly the same
remediation (write the symbol name) plus a permanent resolver and an
arguable tolerance window. A symbol name never goes stale, so the
convention costs nothing to maintain afterward.

Scope is `src/` only, per intake — `tests/`, `docs/`, `scripts/`, `e2e/`,
`eval/`, `examples/`, `web/` and `desktop/` keep whatever policy they had.
`SCANNED_AREAS` below is the one-line-widenable record of that choice.

COUNTING METHOD (stated here, in CONTRIBUTING.md and in CHANGELOG.md)
-----------------------------------------------------------------------
A bare `grep -oE '~[0-9]{3,5}'` over `src/**/*.py` over-counts by roughly
double, because it cannot tell a line anchor from a quantity that already
names its unit: `~120MB`, `~1078s`, `~500 LOC`, `~917k`, `~275M` and
`~394 of the pending backlog` all match it just as readily as a real
anchor does (`test_a_bare_grep_over_counts` pins the gap with a fixed
corpus rather than asserting it in prose). Widening further to bare `~[0-9]`
is worse: it is dominated by `HEAD~1`-style git revisions and `~0o777`
bitwise-not literals, neither of which is prose at all.

The classifier used by this gate instead:

1. Looks only at **comments and string literals** (`tokenize.COMMENT`
   tokens and `ast.Constant` string nodes) — never raw code — which alone
   drops every `HEAD~1` and every `~` bitwise-not operator, since those
   live in code, not prose.
2. Requires the `~` to **start a token**: `(?<![\\w~])~` — this drops
   `sha~2` / `main~2`-style prose that happens to contain a tilde
   mid-word, and it is why `HEAD~1` is also excluded a second, independent
   way (the `D` before the `~` is a word character).
3. Requires **3+ digits**, optionally as an `NNN-NNN` range. The range
   branch is load-bearing: this repo shipped `(db.py ~2884-2892)`, and
   without it the gate would report a confusing half-token. The 3-digit
   floor is deliberate — the 2-digit band (`~40 chars`, `~10 ms`) is
   entirely quantities, and a 2-digit anchor into a 24,000-line file is
   not a shape that occurs.
4. Flags the number **only when it is not followed by a unit or counted
   noun** (`[-–]?\\s*[A-Za-z%×^]+`), minus a stopword deny-list
   (`in`, `and`, `or`, `the`, `a`, `an`, `is`, `at`, `to`, `on`, `by`, `as`,
   `that`, `this`, `which`, `for`, `from`, `with`). `of` is deliberately
   **not** in that deny-list: `~394 of the pending backlog` is a live
   partitive-quantity idiom in this tree, and treating `of` as a stopword
   would misflag it.

Applied to the tree at the time this gate was written, this returned 17
hits with zero false positives: 16 true anchors (15 in
`core/orchestrator.py`, 1 in `vcs/landability.py`) plus one genuine
quantity, `vcs/receipts.py:89`'s `gh caps the files array (~100)`, caught
bare only because its noun happens to *precede* the number. All 16
anchors were replaced by symbol names (or the number was dropped where no
symbol could be confirmed); `receipts.py:89` gained the one word it was
missing, `(~100 files)`, with the number itself untouched.

FAIL-CLOSED I/O
----------------
An undecodable or unparseable file is reported as an offender-shaped
error, never silently skipped — a silently-skipped file is exactly how a
gate scores a meaningless zero. Every read declares `encoding="utf-8"",
per `tests/test_text_reads_declare_encoding.py`.
"""
from __future__ import annotations

import ast
import io
import pathlib
import re
import tokenize

import pytest

# Selected by `.no_human.yml`'s web/desktop routes as `-m repoguard`, alongside
# whichever other repo-wide guards the checkout carries. Deliberately no
# `slow`/`nightly` marker: see `test_the_gate_runs_on_the_pr_lane` below.
pytestmark = pytest.mark.repoguard

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: `src/` only, per intake. A tuple so the scope decision is visible and
#: one-line-widenable if it is ever revisited.
SCANNED_AREAS = ("src",)

#: The `~` must start a token (drops `sha~2`, `main~2`, and — together with
#: rule 1 restricting to comments/strings — `HEAD~1` and bitwise-not `~x`).
#: 3+ digits, optionally an `NNN-NNN` range so `~2884-2892` counts once.
_ANCHOR_TOKEN = re.compile(r"(?<![\w~])~(\d{3,5}(?:-\d{3,5})?)")

#: What "followed by a unit or counted noun" means: an optional leading
#: hyphen/en-dash (`~328-turn`), optional space (`~500 LOC`), then a run of
#: letters/`%`/`×`/`^`.
_UNIT_TAIL = re.compile(r"^[-–]?\s*([A-Za-z%×^]+)")

#: Words that do NOT count as a real unit/noun, so a number followed by one
#: of these is still an anchor, not a quantity. `of` is deliberately absent:
#: `~394 of the pending backlog` is a live partitive-quantity idiom.
_STOPWORDS = {
    "in", "and", "or", "the", "a", "an", "is", "at", "to", "on", "by", "as",
    "that", "this", "which", "for", "from", "with",
}

#: A naive count with none of the above discrimination — what a quick
#: `grep -oE '~[0-9]{3,5}' -r src/` gives you. Used only to demonstrate the
#: gap the classifier closes; never used to decide a pass/fail.
_BARE_ANCHOR_SHAPE = re.compile(r"~[0-9]{3,5}")


def _find_anchors(text: str) -> list[tuple[int, str]]:
    """`(offset, matched_token)` for every `~NNN` shape in `text` that reads
    as an approximate LINE ANCHOR rather than a quantity.

    See the module docstring for the counting method this implements.
    """
    hits: list[tuple[int, str]] = []
    for match in _ANCHOR_TOKEN.finditer(text):
        tail = text[match.end():]
        unit = _UNIT_TAIL.match(tail)
        if unit and unit.group(1).lower() not in _STOPWORDS:
            continue  # a real unit/counted noun follows: a quantity
        hits.append((match.start(), match.group(0)))
    return hits


def _prose_spans(path: pathlib.Path) -> list[tuple[int, str, bool]]:
    """`(lineno, text, offset_uncertain)` for every comment and every
    string-literal LINE in `path`.

    Comments are already single physical lines (`tokenize` guarantees
    this). A multi-line string constant is split on `"\\n"` and offset from
    `node.lineno`, so a docstring reports the anchor's OWN line rather than
    the docstring's opening line — an earlier draft of this scanner reported
    the string's start line for every hit inside it, which made the failure
    message point at the wrong place for anything past the first line. If
    the computed line's text is not actually found at that physical line,
    `offset_uncertain` is True and the caller falls back to `node.lineno`.
    """
    source = path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    spans: list[tuple[int, str, bool]] = []

    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            spans.append((tok.start[0], tok.string, False))

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for offset, line_text in enumerate(node.value.split("\n")):
                candidate = node.lineno + offset
                actual = (
                    source_lines[candidate - 1]
                    if 0 <= candidate - 1 < len(source_lines)
                    else ""
                )
                if line_text and line_text not in actual:
                    spans.append((node.lineno, line_text, True))
                else:
                    spans.append((candidate, line_text, False))

    return spans


def _file_offenders(path: pathlib.Path, *, label: str | None = None) -> list[str]:
    """Offender messages for `path`, `"label:line: ..."` per hit.

    Fails closed: a file that cannot be read or parsed is reported as an
    offender-shaped error rather than silently skipped, which is the only
    way a flat-zero result can be trusted to mean "clean" rather than
    "the scan never looked".
    """
    if label is None:
        try:
            label = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            label = str(path)

    try:
        spans = _prose_spans(path)
    except Exception as exc:  # noqa: BLE001 — intentionally fail-closed, not a skip
        return [
            f"{label}:0: could not scan for approximate line anchors "
            f"({exc.__class__.__name__}: {exc})"
        ]

    offenders = []
    for lineno, text, uncertain in spans:
        for _offset, matched in _find_anchors(text):
            note = (
                " [line offset uncertain; reporting the string's start line]"
                if uncertain
                else ""
            )
            offenders.append(
                f"{label}:{lineno}: approximate line anchor {matched!r}{note} "
                "— name the symbol instead (e.g. `_already_satisfied_subject`), "
                "which the reader has to grep for anyway, or if this is a "
                "quantity, say what you are counting (`~100 files`, not "
                "`(~100)`)"
            )
    return offenders


def _scan_area(area: str) -> list[str]:
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / area).rglob("*.py")):
        offenders.extend(_file_offenders(path))
    return offenders


# ---------------------------------------------------------------------------
# The gate.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("area", SCANNED_AREAS)
def test_no_source_comment_carries_an_approximate_line_anchor(area):
    offenders = _scan_area(area)
    assert offenders == [], (
        "approximate line anchors rot on every landing and nothing else "
        "validates them; replace with a symbol name the reader can grep "
        "for, or delete the number and keep the prose:\n" + "\n".join(offenders)
    )


def test_the_scan_would_not_pass_vacuously():
    """A misconfigured `REPO_ROOT` or a renamed `src/` would make the
    flat-zero assertion above pass for the wrong reason. Guarded directly,
    the way the sibling `read_text` gate documents doing for its own scope."""
    root = REPO_ROOT / "src"
    paths = list(root.rglob("*.py"))
    assert paths, "src/ scan found no Python files at all; REPO_ROOT is wrong"
    assert any("~" in p.read_text(encoding="utf-8") for p in paths), (
        "no file under src/ contains a tilde at all; the flat-zero pass "
        "above would be vacuous"
    )


# ---------------------------------------------------------------------------
# Mutation demonstration (acceptance criterion: shown, not asserted in prose).
# ---------------------------------------------------------------------------


def test_the_gate_fails_on_a_planted_anchor(tmp_path):
    """Copy a real `src/` file, plant an anchor in it, and prove the SAME
    scanner the repo-wide test above uses reports exactly that line. This is
    the mutation the repo-wide test's flat-zero pass would otherwise only be
    asserted, never demonstrated, to catch."""
    real = REPO_ROOT / "src" / "no_human" / "core" / "bounds.py"
    original = real.read_text(encoding="utf-8")
    assert _file_offenders(real) == [], (
        "bounds.py is expected to be clean before mutation; if not, this "
        "test can't tell the mutation from pre-existing rot"
    )

    mutated = original + "\n# planted anchor for the mutation demo, see ~12034\n"
    planted_lineno = len(mutated.splitlines())
    copy_path = tmp_path / "bounds.py"
    copy_path.write_text(mutated, encoding="utf-8")

    offenders = _file_offenders(copy_path, label="src/no_human/core/bounds.py")
    matching = [o for o in offenders if "~12034" in o]
    assert matching, (
        "planting '~12034' in a copy of a real src/ file was not caught by "
        "the same scanner the repo-wide gate uses — the gate cannot be "
        "trusted to catch a new one"
    )
    assert any(f":{planted_lineno}:" in o for o in matching), matching

    # The untouched original is still clean: it is the mutation the gate
    # objects to, not the file.
    assert _file_offenders(real) == []


def test_the_guard_can_actually_see_an_offender():
    """A guard that scores zero because its scanner is broken looks exactly
    like a guard that scores zero because the tree is clean. Pin the scanner
    against a known positive AND a known-clean symbol-form replacement, both
    directions, so a refactor that quietly stops matching fails here instead
    of going green forever."""
    found = [tok for _offset, tok in _find_anchors("see the check at ~12034 for details")]
    assert found == ["~12034"], "the scanner no longer recognises a planted anchor"

    clean = _find_anchors("see `_already_satisfied_subject` for details")
    assert clean == [], "the scanner flags a symbol-form reference with no digits"


def test_the_scanner_reads_comments_and_docstrings_and_not_code(tmp_path):
    """`x = ~1050` in code is a bitwise-not expression, not prose, and must
    never be visited; `~1050` in a comment on the very next line must be."""
    planted = tmp_path / "code_vs_comment.py"
    planted.write_text(
        "x = ~1050  # not this one, it is code\n"
        "y = 2  # see the check at ~1050\n",
        encoding="utf-8",
    )
    offenders = _file_offenders(planted)
    assert len(offenders) == 1, offenders
    assert ":2:" in offenders[0], offenders


def test_an_anchor_deep_in_a_docstring_reports_its_own_line(tmp_path):
    """A 40-line docstring with the anchor on line 30 must report 30, not
    the docstring's opening line — the bug an earlier draft of this scanner
    had (it reported line 12606 for an anchor actually on line 12628)."""
    docstring_lines = [f"filler line {i}" for i in range(1, 40)]
    docstring_lines.insert(29, "the check is at ~99999")  # 30th line, 1-based
    source = '"""' + "\n".join(docstring_lines) + '\n"""\n'

    planted = tmp_path / "deep_docstring.py"
    planted.write_text(source, encoding="utf-8")

    offenders = _file_offenders(planted)
    assert len(offenders) == 1, offenders
    assert ":30:" in offenders[0], offenders


# ---------------------------------------------------------------------------
# New anchors are rejected automatically, not by review attention.
# ---------------------------------------------------------------------------


def test_the_gate_runs_on_the_pr_lane():
    """No `slow`/`nightly` marker, so this file lands in the `fast` PR lane
    automatically — `.no_human.yml` selects `repoguard` guards by MARKER,
    never by filename, and a `slow`/`nightly` marker here would silently
    disarm the gate on every PR (mirrors `test_test_lanes.py`'s concern that
    fast+nightly must partition the whole suite with nothing left out)."""
    marks = pytestmark if isinstance(pytestmark, (list, tuple)) else [pytestmark]
    names = {mark.name for mark in marks}
    assert names == {"repoguard"}, (
        f"this gate carries marker(s) {sorted(names)}; the PR lane runs "
        '`-m "not slow and not nightly"`, so a slow/nightly marker here '
        "would silently disarm the gate on every PR"
    )


# ---------------------------------------------------------------------------
# Approximate QUANTITIES are untouched and still read naturally.
# ---------------------------------------------------------------------------

#: Copied verbatim from live lines in `src/` (see the files named in each
#: `why`). Every one of these must NOT be flagged.
_QUANTITY_CASES = [
    ("~5500x", "unit suffix x — cli/commands.py"),
    ("~1078s", "unit suffix s (seconds) — config.py, review/reviewer.py"),
    ("~360s", "unit suffix s (seconds) — config.py"),
    ("~113k", "unit suffix k — core/bounds.py"),
    ("~758k", "unit suffix k — core/bounds.py"),
    ("~917k", "unit suffix k — core/db.py"),
    ("~275M", "unit suffix M — core/pricing.py"),
    ("~120MB", "unit suffix MB — doctor.py, walks_provision.py"),
    ("~500 LOC", "counted noun LOC — core/prompt_blocks.py"),
    ("~328-turn successful runs", "counted noun, hyphenated — core/bounds.py"),
    ("~394 of the pending backlog", "partitive idiom; 'of' is not a stopword — config.py"),
    ("~700 of the 1000 characters", "partitive idiom — learning/queue.py"),
    ("~38-60%", "below the 3-digit floor — core/feasibility.py"),
    ("~10^5", "below the 3-digit floor — agent/claude_backend.py"),
]

#: Not prose at all — git revisions and a bitwise-not literal.
_NON_PROSE_NEGATIVES = [
    ("HEAD~1", "git revision; ~ does not start a token"),
    ("main~2", "git revision; ~ does not start a token"),
    ("~0o777", "bitwise-not-shaped octal literal; no 3+ digit run right after ~"),
]

#: The five real anchor shapes this repo shipped before the fix (each from
#: `core/orchestrator.py`, spelled here because this file is not itself
#: scanned — `SCANNED_AREAS` is `("src",)`).
_ANCHOR_SHAPES = [
    "~4407 —",
    "(~10674)",
    "(~17967),",
    "(line ~1050)",
    "~2884-2892);",
]


@pytest.mark.parametrize(("text", "why"), _QUANTITY_CASES)
def test_approximate_quantities_are_not_flagged(text, why):
    assert _find_anchors(text) == [], why


@pytest.mark.parametrize(("text", "why"), _NON_PROSE_NEGATIVES)
def test_non_prose_tildes_are_not_flagged(text, why):
    assert _find_anchors(text) == [], why


def test_the_only_quantity_this_change_touched_named_no_unit():
    """`receipts.py:89` is the one quantity this change touched, and it
    gained a word, not a rewrite: `(~100)` -> `(~100 files)`, number
    unchanged. Every other quantity in `src/` stays byte-identical."""
    receipts = (REPO_ROOT / "src" / "no_human" / "vcs" / "receipts.py").read_text(
        encoding="utf-8"
    )
    assert "~100 files" in receipts, "receipts.py:89 should read '(~100 files)'"
    assert "(~100)" not in receipts, "the old bare quantity should be gone"

    unaffected = {
        REPO_ROOT / "src" / "no_human" / "core" / "pricing.py": "~275M",
        REPO_ROOT / "src" / "no_human" / "core" / "db.py": "~917k",
        REPO_ROOT / "src" / "no_human" / "core" / "prompt_blocks.py": "~500 LOC",
        REPO_ROOT / "src" / "no_human" / "doctor.py": "~120MB",
        REPO_ROOT / "src" / "no_human" / "review" / "reviewer.py": "~1078s",
    }
    for path, needle in unaffected.items():
        text = path.read_text(encoding="utf-8")
        assert needle in text, f"{needle!r} should still read verbatim in {path}"


def test_every_anchor_shape_this_repo_shipped_is_caught():
    for shape in _ANCHOR_SHAPES:
        hits = _find_anchors(shape)
        assert len(hits) == 1, f"expected exactly one anchor in {shape!r}, got {hits}"


def test_a_bare_grep_over_counts():
    """Pins why `_find_anchors`'s unit-exclusion step exists, with a fixed
    corpus rather than the live tree, so the assertion stays meaningful
    forever instead of degrading to a trivial 0-vs-0 once every real anchor
    is gone (which, after this change, all of them are)."""
    corpus_lines = (
        _ANCHOR_SHAPES
        + [text for text, _why in _QUANTITY_CASES]
        + [text for text, _why in _NON_PROSE_NEGATIVES]
    )
    corpus = "\n".join(corpus_lines)

    bare_hits = len(_BARE_ANCHOR_SHAPE.findall(corpus))
    classifier_hits = sum(len(_find_anchors(line)) for line in corpus_lines)

    assert classifier_hits == len(_ANCHOR_SHAPES), (
        f"expected the classifier to flag exactly the {len(_ANCHOR_SHAPES)} "
        f"real anchor shapes and nothing else in this corpus, got "
        f"{classifier_hits}"
    )
    assert bare_hits >= 2 * classifier_hits, (
        f"bare grep found {bare_hits} '~NNN'-shaped tokens vs "
        f"{classifier_hits} the classifier flags as anchors over the same "
        "corpus; the gap is exactly the quantities a naive regex cannot "
        "distinguish from a line anchor"
    )

    # Same story on the live tree: src/ still carries a substantial
    # population of ~NNN quantities that only the unit-exclusion step
    # correctly leaves alone.
    root = REPO_ROOT / "src"
    live_bare_hits = sum(
        len(_BARE_ANCHOR_SHAPE.findall(path.read_text(encoding="utf-8")))
        for path in root.rglob("*.py")
    )
    assert live_bare_hits >= 20, (
        "expected src/ to still carry a substantial population of ~NNN "
        "quantities that a bare grep cannot tell apart from a line anchor"
    )
