"""The README is checked against the code, not against the last person to edit it.

Five review rounds on one README found the same defect four times: a claim about
a component or a default that nobody had verified, and that no test could catch.
Two of those were introduced *while fixing the other two* — "Monaco diff viewer"
became "side-by-side diff view" became "unified diff view", three guesses at one
`<pre>` that a single grep would have settled.

These tests pin the load-bearing, mechanically checkable claims to their
authoritative source: config defaults to ``DEFAULT_CONFIG``, the blocker count to
the enum, the architecture tree to the filesystem. They deliberately do NOT try
to verify prose — a test cannot know whether "adversarial reviewer" is a fair
description. They cover the class of claim that silently rots: numbers, counts,
directory listings, and words we have already been wrong about.

Read a green run correctly: the retired-claim and link tests are NEGATIVE
assertions — they prove nothing was reintroduced, not that anything is present.
An empty README would satisfy them. Only the config-row, blocker-count and
architecture-tree tests assert that something true is actually there.
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
import warnings
from collections import namedtuple
from pathlib import Path

import pytest
import yaml

from no_human.blockers.taxonomy import BlockerCategory
from no_human.config import DEFAULT_CONFIG

# Selected by `.no_human.yml`'s web/desktop routes as `-m repoguard`, alongside
# whichever other repo-wide guards the checkout carries.
pytestmark = pytest.mark.repoguard

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"

# RETARGET (2026-08-01). The README was cut from 255 lines to ~120: the config
# table moved to docs/configuration.md and the gate/limits detail — which
# carried every ``file.py:LINE`` citation — moved to docs/verification.md.
#
# The claims did not go away and they did not get weaker, so neither do the
# guards. Two of them now read the UNION of the surfaces the claim can live on
# rather than the README alone. That is a STRENGTHENING in both directions: a
# stale default is now caught wherever it is written, and moving a claim back
# onto the front page re-arms the same check without an edit here. What is
# unchanged is every assertion, including the mandatory-hit assertions that stop
# a guard passing vacuously once its subject leaves a surface.
DOCUMENTED_SURFACES = (
    README,
    REPO / "docs" / "configuration.md",
    REPO / "docs" / "verification.md",
)


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def documented() -> str:
    """README + the docs pages the front page delegates its claims to.

    Concatenated with a blank line between, so no regex can match across the
    seam between two files.
    """
    missing = [p for p in DOCUMENTED_SURFACES if not p.exists()]
    assert not missing, (
        f"surface(s) named here do not exist: {[p.name for p in missing]} — a "
        f"guard pointed at a missing file would silently check nothing"
    )
    return "\n\n".join(p.read_text(encoding="utf-8") for p in DOCUMENTED_SURFACES)


def _config_rows(readme: str) -> dict[str, str]:
    """Every `| `a.b` | value | …` row in the README's config tables.

    Splits on the cell delimiter rather than matching a value pattern: a value
    cell that is bolded, or carries a parenthetical, is a legitimate edit and
    must not be reported as a missing row.
    """
    rows: dict[str, str] = {}
    for line in readme.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        key = cells[0].strip("`* ")
        if re.fullmatch(r"[a-z_]+(?:\.[a-z_]+)+", key):
            value = cells[1].strip()
            # Drop a trailing annotation BEFORE unwrapping the markup: `3`
            # (tune per repo) documents the same default as `3`, but stripping
            # backticks first cannot reach the one hidden behind the ")".
            # Only when something remains, so a bare "(none)" stays the
            # none-sentinel the comparison below expects.
            unannotated = re.sub(r"\s*\([^()]*\)$", "", value).strip()
            if unannotated:
                value = unannotated
            rows[key] = value.strip("`* ")
    return rows


_MISSING = object()


def _resolve(path: str):
    """DEFAULT_CONFIG value for a dotted key, or _MISSING."""
    node = DEFAULT_CONFIG
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


# Rows that must exist. A generic walk alone would pass on a README with every
# config row deleted; these keep the table itself honest.
REQUIRED_ROWS = {
    "server.port", "llm.primary_model", "llm.review_model",
    "bounds.max_attempts", "bounds.max_turns_per_attempt",
    "pipeline.review_routing.enabled", "pipeline.review_routing.max_diff_lines",
}


def test_required_rows_are_real_config_keys():
    """REQUIRED_ROWS is hand-kept, so on its own it only proves the README still
    says what it used to say. Without this, a key renamed or deleted in
    config.py is caught by neither test: the walk below skips it as _MISSING,
    and this list happily confirms the now-stale README row is still present."""
    unreal = sorted(k for k in REQUIRED_ROWS if _resolve(k) is _MISSING)
    assert not unreal, (
        f"REQUIRED_ROWS names keys that are not in DEFAULT_CONFIG: {unreal} — "
        f"the config changed and the README rows for them are now stale"
    )


def test_config_table_documents_the_required_keys(documented):
    present = set(_config_rows(documented))
    missing = REQUIRED_ROWS - present
    assert not missing, f"config table no longer documents: {sorted(missing)}"


def test_every_documented_default_matches_config(documented):
    """Walks EVERY dotted config row rather than a hand-kept list, so a newly
    documented default is covered the day it is added.

    Only scalars are compared. A list or dict is rendered for humans (
    `[main, master, release/*]`) and matching that text to a Python repr would
    fail on formatting, not on truth.
    """
    wrong = []
    for key, stated in _config_rows(documented).items():
        actual = _resolve(key)
        if actual is _MISSING or isinstance(actual, (list, dict)):
            continue
        norm = stated.strip()
        if norm.lower() in {"null", "none", "*(none)*", "(none)", "—"}:
            norm = None
        elif norm.lower() in {"true", "false"}:
            norm = norm.lower() == "true"
        # "" and "(none)" are the same claim to a reader.
        if norm is None and actual in (None, ""):
            continue
        if isinstance(actual, bool):
            if norm is not actual:
                wrong.append(f"{key}: docs say {stated!r}, config says {actual!r}")
        elif str(norm) != str(actual):
            wrong.append(f"{key}: docs say {stated!r}, config says {actual!r}")
    assert not wrong, "config table disagrees with DEFAULT_CONFIG:\n  " + "\n  ".join(wrong)


def test_prose_default_matches_config(documented):
    """The original defect lived in PROSE — the troubleshooting row told users to
    raise max_turns_per_attempt from a number that was never the default.

    Reads the union (see DOCUMENTED_SURFACES): the 2026-08-01 rewrite moved the
    prose statement of this default to docs/verification.md along with the rest
    of the bounded-loop paragraph. Every stated spelling on every surface is
    still checked, and the mandatory-hit assertion still fails if the claim
    disappears from all of them.
    """
    readme = documented
    actual = DEFAULT_CONFIG["bounds"]["max_turns_per_attempt"]
    # A bare finditer guards nothing when the prose is reworded past its one
    # spelling — the loop body never runs and the test passes green. Accept the
    # spellings a writer would actually use, then require at least one hit.
    #
    # The 2026-07-30 rewrite dropped the troubleshooting table that carried the
    # only "(default 500)" spelling and stated the same number as a plain "is
    # 500" instead. The claim did not move surface and it did not go away — only
    # its phrasing changed — so the fix is to widen the pattern, exactly as the
    # paragraph above anticipated, NOT to dictate one wording back into the
    # prose. `is N` and `defaults to N` are added; the mandatory-hit assertion
    # below is unchanged, so this cannot start passing vacuously.
    matches = list(re.finditer(
        r"max_turns_per_attempt`?[^.\n]*?"
        r"(?:\(defaults?\s*(?:to)?\s*:?\s*(\d+)\)|\bis\s+(\d+)\b|\bdefaults?\s+to\s+(\d+)\b)",
        readme))
    assert matches, (
        "no 'max_turns_per_attempt (default N)' / 'is N' prose found in the "
        "README. Either it was reworded past this pattern — widen the pattern — "
        "or the claim was dropped, and this guard was about to pass vacuously."
    )
    for m in matches:
        stated = next(g for g in m.groups() if g is not None)
        assert int(stated) == actual, (
            f"README prose says default {stated}, config says {actual}"
        )


# Counts get written as words as often as digits. Keeping only the digit
# spelling let "ten categories" read as *no claim at all*, so a README that had
# quietly gone stale in words would have passed.
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}
_CATEGORY_COUNT_RE = re.compile(
    r"\b(\d+|" + "|".join(_NUMBER_WORDS) + r")[- ]categor(?:y|ies)\b",
    re.IGNORECASE,
)


def _stated_category_counts(text: str) -> list[int]:
    out = []
    for m in _CATEGORY_COUNT_RE.finditer(text):
        tok = m.group(1).lower()
        out.append(int(tok) if tok.isdigit() else _NUMBER_WORDS[tok])
    return out


def test_blocker_category_count_matches_the_enum(documented):
    """The README said "8-category" for a 10-member enum.

    The 2026-07-30 rewrite restated the same claim in words ("one of ten
    categories") — it now reads both spellings. That is a STRENGTHENING: the old
    digit-only pattern would have waved through "eight categories" without a
    word. Union-scoped 2026-08-01: the count is now stated on TWO surfaces (the
    README bullet and the docs/verification.md paragraph it links to), and the
    "every stated count must agree" assertion below is exactly what makes that
    worth checking on both.
    """
    n = len(BlockerCategory)
    stated = _stated_category_counts(documented)
    assert stated, (
        f"docs state no blocker-category count at all; the taxonomy has {n} "
        f"members ({', '.join(c.name for c in BlockerCategory)}). A count that "
        f"is not stated cannot be checked — restate it or this guard is blind."
    )
    # Presence at ONE site is not enough. The README states the count twice, so
    # re-introducing the original "8-category" defect at the other site passed
    # this test. Every stated count must agree with the enum.
    wrong = sorted({c for c in stated if c != n})
    assert not wrong, (
        f"README claims {wrong} blocker categories; the taxonomy has {n} members"
    )


# A citation names a SYMBOL, not a line. Two spellings are accepted, and they
# are the only two the docs may use:
#
#   1. a markdown link whose text is the symbol:  [`_make_guard_hook`](../src/...py)
#   2. prose:  `_verify_citations` in `reviewer.py`
#              `A`, `B` and `C` in [`core/bounds.py`](../src/...py)
#
# Form 2 anchors on the FILE and walks backwards over the run of backticked
# identifiers immediately before it, so a list of symbols sharing one file is
# captured whole rather than only its last member.
_SYMBOL = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?"
_SYMBOL_LINK_RE = re.compile(
    r"\[`(" + _SYMBOL + r")`\]\(([^)]+\.py)\)"
)
#: `A`, `B` and `C` in `file.py` — the file optionally wrapped in a link.
_SYMBOL_PROSE_RE = re.compile(
    r"((?:`" + _SYMBOL + r"`(?:,|\s+and\b|\s+)\s*)*`" + _SYMBOL + r"`)"
    r"\s+in\s+(?:\[)?`([\w/]+\.py)`"
)
_BACKTICKED = re.compile(r"`(" + _SYMBOL + r")`")


def _resolve_source(path: str) -> list[Path]:
    """Every file a reader could land on following a cited path.

    A slashed path is tried from the repo root and then from the package root
    (``agent/guard.py`` means ``src/no_human/agent/guard.py``). A bare basename
    must resolve to exactly ONE file under src/no_human: an ambiguous citation
    fails rather than being skipped, because a citation the reader cannot follow
    is the defect, not an exemption.
    """
    if "/" in path:
        for base in (REPO, REPO / "src" / "no_human", REPO / "src"):
            candidate = (base / path).resolve()
            if candidate.exists():
                return [candidate]
        return []
    return sorted((REPO / "src" / "no_human").rglob(path))


def _defined_symbols(source: Path) -> set[str]:
    """Module-level names, classes, and ``Class.method`` pairs defined in a file."""
    import ast

    tree = ast.parse(source.read_text(encoding="utf-8"))
    names: set[str] = set()

    def visit(node, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(prefix + child.name)
                # One level of nesting is enough for `Class.method`; a citation
                # deeper than that is not a citation a reader can follow.
                if isinstance(child, ast.ClassDef):
                    visit(child, prefix + child.name + ".")
            elif isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        names.add(prefix + target.id)
            elif isinstance(child, ast.AnnAssign):
                if isinstance(child.target, ast.Name):
                    names.add(prefix + child.target.id)
            elif isinstance(child, (ast.If, ast.Try)):
                visit(child, prefix)

    visit(tree)
    return names


def _documented_symbol_citations(text: str) -> list[tuple[str, str]]:
    """Every (symbol, path) pair the docs cite, in either accepted spelling."""
    cites: list[tuple[str, str]] = [
        (sym, path) for sym, path in _SYMBOL_LINK_RE.findall(text)
    ]
    for group, path in _SYMBOL_PROSE_RE.findall(text):
        for sym in _BACKTICKED.findall(group):
            cites.append((sym, path))
    return cites


def test_documented_source_citations_resolve(documented):
    """Every symbol the front page or its docs cite must still be defined.

    RETARGET (2026-08-01), third time for this guard. It checked ``file.py:LINE``
    citations, and could only catch a line PAST THE END of the file — its own
    docstring said so in red: ``config.py:676 -> config.py:1`` passed. A survey
    of all 170 line citations in tracked docs found that residual risk had gone
    from theoretical to typical: of the 17 in docs/verification.md alone, 9
    pointed at code with nothing to do with the sentence citing them
    (``config.py:713-726`` was cited for ``bounds`` and landed in the planning
    block; ``orchestrator.py:845`` was cited for budget enforcement and landed
    in ``_emit_review``). Every one of those passed the old check.

    Line numbers rot on every edit above them, so the docs were converted to
    cite SYMBOLS and this guard was converted to match. That trades a weak check
    of a fragile thing for a strong check of a stable one: a symbol that is
    renamed or deleted fails here by name, which is the actual defect a reader
    hits. The floor of 10 is unchanged and still cannot pass vacuously.

    🔴 What this still does NOT check, so nobody over-reads a green run: that the
    cited symbol does what the prose says it does. ``_gate_verdict`` could be
    gutted to ``return True`` and this stays green. Existence and reachability
    are what a test can own; whether the code means what the sentence claims is
    left to the human, as before.
    """
    cites = _documented_symbol_citations(documented)
    assert len(cites) >= 10, (
        f"only {len(cites)} symbol citations found across "
        f"{[p.name for p in DOCUMENTED_SURFACES]}; this guard is the only thing "
        f"checking them and it must not pass vacuously"
    )
    bad: list[str] = []
    for symbol, path in cites:
        hits = _resolve_source(path)
        if len(hits) != 1:
            bad.append(
                f"{symbol} in {path}: path resolves to {len(hits)} files"
                f"{' — disambiguate with a path prefix' if len(hits) > 1 else ''}"
            )
            continue
        defined = _defined_symbols(hits[0])
        if symbol not in defined:
            bad.append(
                f"{symbol} is not defined in {hits[0].relative_to(REPO)} — "
                f"renamed or deleted, and the docs still send readers to it"
            )
    assert not bad, "docs cite source symbols that do not resolve:\n  " + "\n  ".join(bad)


# Every symbol below MUST be found by the parser above, in the file named. This
# is the mandatory-hit half: the floor of 10 proves *something* is parsed, but
# not that the sentences carrying the load-bearing claims are among them. A
# rewording that moved one of these out of an accepted spelling would otherwise
# drop it silently and still clear the floor on the others.
MANDATORY_CITATIONS = (
    ("_make_guard_hook", "claude_backend.py"),   # the PreToolUse safety hook
    ("_gate_verdict", "reviewer.py"),            # verdict recomputed, not trusted
    ("_verify_citations", "reviewer.py"),        # hallucinated-location demotion
    ("_FORGE_MERGE", "guard.py"),                # the merge ban
    ("DEFAULT_CONFIG", "config.py"),             # every documented default
    ("assert_subscription_mode", "config.py"),   # one billing path per run
)


@pytest.mark.parametrize("symbol,basename", MANDATORY_CITATIONS)
def test_load_bearing_claim_still_cites_its_symbol(documented, symbol, basename):
    cited = {
        (sym, Path(path).name) for sym, path in _documented_symbol_citations(documented)
    }
    assert (symbol, basename) in cited, (
        f"no documented citation of `{symbol}` in {basename} was parsed. Either "
        f"the claim was reworded past the two accepted citation spellings — fix "
        f"the wording, not this list — or it was dropped, and the guard above "
        f"was about to check one citation fewer without saying so."
    )


# REMOVED (2026-07-30): test_architecture_tree_lists_every_package.
#
# It pinned the README's `src/no_human/` package tree to the filesystem. The
# rewrite deleted that tree (PLAN.md is the architecture surface; the front page
# duplicated it), so the guard's subject exists on no surface in this repo and
# there is nowhere to re-point it.
#
# It was first kept behind a `skipif` that re-armed if a tree ever returned.
# That was wrong twice over and review caught both:
#   1. the skip marker itself trips no_human's tamper guard (skip/xfail 0->1
#      reads as a neutered test), and README.md advertises that exact rule -
#      shipping it would have meant a front page describing a gate the commit
#      trips;
#   2. the arming regex was byte-identical to the extractor, so it only armed
#      for the ONE tree format the old README happened to use. Six realistic
#      tree formats were tried against it, each omitting a real package: it
#      caught two and silently skipped four, including the output of `tree(1)`.
#      An unarmed guard that reports "skipped" is worse than a deleted one,
#      because the skip reads as coverage.
#
# Coverage is not lost: `test_readme_source_citations_resolve` above checks the
# claims that replaced the tree. What is genuinely gone is the completeness
# check, and only because nothing on any surface claims to enumerate any more.


# Claims proven false by review and fixed. A regression here is not a typo — it
# is the README describing a feature the code does not have.
# Reviewer-session claims (docs/security.md, PRODUCT.md) do NOT belong here:
# this list is scoped to DOCUMENTED_SURFACES via the `documented` fixture, and
# neither file is in that union — RETIRED_REVIEWER_CLAIMS below is a
# separately-scoped table for exactly that pair of files.
RETIRED_CLAIMS = [
    ("monaco", "the diff view is native — recheck with: grep -ri monaco web/src/"),
    ("desktop notification", "recheck with: ls src/no_human/notify/"),
    ("5-lane", "recheck with: grep -n BOARD_LANES web/src/boardLanes.js"),
    ("code.example.com/dev", "placeholder clone URL that cannot work"),
    ("tests-passing", "a hard-coded badge asserting a build status nothing checks"),
    ("no auto-merge setting to find", "false absolute — the auto_merge_on_approval "
     "config key IS findable (recheck: grep -n auto_merge_on_approval "
     "src/no_human/config.py); the honest claim is that no code path acts on it"),
    ("reproduction gate is pytest-only", "understated — repro_gate.py routes "
     "non-Python ecosystems through the profile's test_cmd (repro_gate.py:157-169)"),
    ("glab mr accept` (glab's own alias for `merge`) is still allowed",
     "false since 008bd04d6 — ('mr','accept') is in _FORGE_MERGE_PAIRS "
     "(recheck: grep -n _FORGE_MERGE_PAIRS src/no_human/agent/guard.py); "
     "guard.evaluate denies `glab mr accept 12` in both session modes"),
    ("are both still allowed in a review session",
     "false — the argv-shaped read-only check in guard.py denies "
     "`git -C . commit` and `git -C . push origin <branch>` at readonly=True "
     "(reviewer.py:2088); recheck with guard.evaluate(..., readonly=True)"),
]


@pytest.mark.parametrize("claim,why", RETIRED_CLAIMS)
def test_retired_false_claim_has_not_returned(documented, claim, why):
    """Union-scoped (2026-08-01). A retired claim is retired from the PRODUCT,
    not from one file — moving the prose that used to carry it onto a docs page
    must not move it out of this guard's reach."""
    assert claim.lower() not in documented.lower(), (
        f"docs reintroduce a claim review already disproved: {claim!r} — {why}"
    )


# ---------------------------------------------------------------------------
# Reviewer-session claims, scoped to docs/security.md §3 and PRODUCT.md's
# Positioning section — the two files `documented`/DOCUMENTED_SURFACES does
# not read (see the pointer comment on RETIRED_CLAIMS above).
#
# Why this exists: `6ef8921ae` corrected docs/security.md §3 to say what
# `agent/guard.py` actually does during a review session. `da3599ae4`, built
# on an older base and landed later, silently carried the pre-fix wording
# back in (`git diff 6ef8921ae da3599ae4 -- docs/security.md` shows the
# reverted lines). Nothing caught it, because RETIRED_CLAIMS above only ever
# reads README.md + docs/configuration.md + docs/verification.md.
# `bd0733456` corrected the wording a second time; this block is the guard
# that stops a third, stale-base revert from landing clean.
# ---------------------------------------------------------------------------

SECURITY_MD = REPO / "docs" / "security.md"
PRODUCT_MD = REPO / "PRODUCT.md"
REVIEWER_CLAIM_SURFACES = {"security": SECURITY_MD, "product": PRODUCT_MD}


def test_security_md_session_mark_covers_all_three_backends():
    """§7's ACT-layer paragraph used to say the session mark covers "the two
    coding backends" — true back when there were two, false once `local`
    shipped as a third (`agent/backend.py`'s `SUPPORTED_BACKENDS`). `local`'s
    `make_backend` branch returns a `ClaudeBackend`
    (`agent/backend.py:536-548`), so `ClaudeBackend._options()` already marks
    it; the doc must say so, not undercount and leave a reader thinking a
    `local` run is unmarked."""
    text = SECURITY_MD.read_text(encoding="utf-8")
    assert "the two coding backends" not in text, (
        "docs/security.md still claims only two coding backends stamp the "
        "session mark; there are three (claude, codex, local)."
    )
    assert "all three of them" in text and "`local` backend runs on" in text, (
        "docs/security.md's session-mark paragraph should name all three "
        "backends and explain `local` is covered via ClaudeBackend."
    )


def test_backends_md_flags_the_extended_thinking_requirement_at_the_top_of_the_local_section():
    """A user picking `local` used to meet the extended-thinking requirement
    only mid-section, after several paragraphs of env-var setup — by then
    they may already have hit the turn-one 500 the requirement exists to
    warn about. The admonition must be the first thing under the section
    heading, not merely present somewhere inside it."""
    text = (REPO / "docs" / "BACKENDS.md").read_text(encoding="utf-8")
    heading = "## `local` — your own model server"
    start = text.index(heading) + len(heading)
    end = text.index("\n## ", start)
    section = text[start:end]

    lines = [line for line in section.splitlines() if line.strip()]
    first_line = lines[0]
    assert first_line.startswith(">") and "extended thinking" in first_line, (
        "the local-backend section's first non-blank line must be the "
        "extended-thinking admonition, not merely appear later in the "
        "section: " + repr(first_line)
    )

    admonition_block = " ".join(line.lstrip(">").strip() for line in lines[:5])
    assert "500" in admonition_block and "does not support thinking" in admonition_block, (
        "the top-of-section admonition must cite the measured consequence "
        "(HTTP 500, \"does not support thinking\"), not just the bare "
        "requirement"
    )

    assert (
        "**Your model must support extended thinking.** The harness enables "
        "thinking on"
    ) in section, (
        "the original detailed prose paragraph must survive verbatim; the "
        "admonition is an addition, not a replacement"
    )

# Section boundaries: (line the section starts with, line the NEXT section
# starts with). Sliced rather than whole-file so a legitimate `read-only` use
# elsewhere in either file (there is none today, but nothing prevents one)
# cannot be swept in by accident.
_SECTION_BOUNDS = {
    "security": ("## 3. ", "## 4. "),
    "product": ("## Positioning", "## "),
}


def _section(text: str, start_prefix: str, next_prefix: str) -> str:
    """Slice one markdown section: the line starting with `start_prefix` up
    to (not including) the next line starting with `next_prefix`.

    Asserts the slice is non-empty so a renamed heading fails loudly — a
    guard scoped to a section that no longer exists would silently check
    nothing, which is the same failure mode this whole block exists to close.
    """
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith(start_prefix)), None
    )
    assert start is not None, (
        f"heading {start_prefix!r} not found — did the section get renamed "
        f"or moved?"
    )
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].startswith(next_prefix)
        ),
        len(lines),
    )
    section = "\n".join(lines[start:end])
    assert section.strip(), f"section under {start_prefix!r} sliced to nothing"
    return section


def _normalize_claim_text(text: str) -> str:
    """Collapse whitespace to single spaces, strip markdown bold markers,
    lowercase, then blank straight-double-quoted spans.

    Three defects this fixes, all of which already happened:

    1. The reverted sentence was line-wrapped on disk (`git diff 6ef8921ae
       da3599ae4 -- docs/security.md`: "...backend runs **read-only**: all
       write tools are\\nblocked unconditionally."). A matcher that does not
       collapse whitespace first would miss a verbatim replay of the actual
       historical regression this ticket is about.
    2. The corrected prose *quotes* the false claim verbatim
       (`docs/security.md`: `said "read-only: all write tools are blocked
       unconditionally" until 2026-08-22.`). A bare substring match fires on
       the correction itself. Blanking quoted spans before matching means a
       stale-base revert — which reintroduces the claim as unquoted prose —
       still trips the guard, while the sentence documenting the fix does not.
    3. That same reverted sentence wrapped "read-only" in markdown bold
       (`**read-only**`, verbatim on disk — see `_DA3599AE4_HUNK` below). A
       matcher over the raw text would miss "the backend runs read-only" as a
       substring because the two `**` markers sit between "runs" and
       "read-only". Stripping `**` before matching means a replay that
       re-adds the historical emphasis still trips the guard.
    """
    collapsed = re.sub(r"\s+", " ", text)
    unbolded = collapsed.replace("**", "")
    lowered = unbolded.lower()
    return re.sub(r'"[^"]{0,400}"', " ", lowered)


def _claim_text(path: Path, surface: str) -> str:
    """Read `path` (a real reviewer-claim surface, or a tmp_path copy of
    one), slice the section scoped to `surface`, and normalize it."""
    start_prefix, next_prefix = _SECTION_BOUNDS[surface]
    raw = path.read_text(encoding="utf-8")
    return _normalize_claim_text(_section(raw, start_prefix, next_prefix))


def _wrap_mid_phrase(phrase: str) -> str:
    """Insert a newline at the middle space of `phrase`.

    Mirrors how da3599ae4's revert actually read on disk (line-wrapped
    mid-sentence). Used only to prove `_claim_text`'s whitespace collapse is
    what makes an injected phrase detectable, not a coincidence of how the
    injection happens to be typed in this test file.
    """
    spaces = [i for i, c in enumerate(phrase) if c == " "]
    assert spaces, f"phrase has no space to wrap: {phrase!r}"
    mid = spaces[len(spaces) // 2]
    return phrase[:mid] + "\n" + phrase[mid + 1 :]


def _reviewer_surface_missing(surface: str, path: Path) -> bool:
    """True if `path` is absent; False if it exists.

    docs/security.md is SHIP-classified (EXPORT_CLASSIFICATION.txt:197) and
    ships in every tree — a caller must hard-fail if it is ever missing.
    PRODUCT.md is DROP-classified (EXPORT_CLASSIFICATION.txt:687) and is
    correctly absent from the exported/public tree (confirmed: 404s on the
    public repo) while `tests/test_readme_claims.py` itself is SHIP and runs
    there — a caller skips a PRODUCT.md-dependent case in that tree instead
    of hard-failing on a file that was intentionally dropped. Whenever
    PRODUCT.md IS present (this private tree, or any build that keeps it)
    every downstream assertion still runs at full strength: this only makes
    absence non-fatal, it never softens a check that can actually run.
    """
    if path.exists():
        return False
    assert surface == "product", f"surface {surface!r} ({path}) does not exist"
    return True


ReviewerClaim = namedtuple(
    "ReviewerClaim", ["claim_id", "surfaces", "phrase", "injection", "why"]
)

RETIRED_REVIEWER_CLAIMS = [
    # False claim: "the backend runs **read-only**: all write tools are
    # blocked unconditionally" (docs/security.md, until 2026-08-22).
    # Ground truth: guard.py:58 WRITE_TOOLS = {"Write", "Edit",
    # "NotebookEdit", "MultiEdit"}; the readonly denial set is WRITE_TOOLS +
    # BACKGROUND_TOOLS + SPAWN_TOOLS (guard.py:evaluate) — Bash is in none
    # of those sets, so a shell redirection writes. `6ef8921ae` corrected
    # this; `da3599ae4`, built on an older base, silently reverted it;
    # `bd0733456` corrected it again — this entry is the guard against a
    # fourth landing of the same stale hunk.
    ReviewerClaim(
        "write-tools-unconditional",
        ("security", "product"),
        "all write tools are blocked unconditionally",
        "During review the backend runs read-only: all write tools are "
        "blocked unconditionally.",
        "guard.py:58 WRITE_TOOLS has 4 names; Bash is in no readonly denial "
        "set (guard.py:evaluate) — reintroduced by da3599ae4 over "
        "6ef8921ae",
    ),
    # False claim: the other half of the same da3599ae4 sentence — "the
    # backend runs **read-only**" — so a partial replay of the revert (e.g.
    # dropping only the "unconditionally" clause) still trips the guard.
    # Same ground truth as write-tools-unconditional above.
    ReviewerClaim(
        "backend-runs-read-only",
        ("security", "product"),
        "the backend runs read-only",
        "During review, the backend runs read-only, so nothing changes on "
        "disk.",
        "guard.py:WRITE_TOOLS/evaluate — Bash is not in any readonly denial "
        "set, so the backend is not read-only; reintroduced by da3599ae4 "
        "over 6ef8921ae",
    ),
    # False claim: PRODUCT.md's pre-bd0733456 spelling — "adversarial review
    # by a different model in fresh context with read-only tools". Matched
    # as the long phrase deliberately: bare "read-only" is legitimate
    # elsewhere (nh investigate, Codex --sandbox read-only, "read-only
    # gatherers", the verifier's "read-only file access") and must not fire.
    ReviewerClaim(
        "review-with-read-only-tools",
        ("security", "product"),
        "with read-only tools",
        "an adversarial review by a different model in fresh context with "
        "read-only tools, told to refute \"done\".",
        "guard.py:WRITE_TOOLS/evaluate — Bash is not denied, so the session "
        "is not read-only; corrected by bd0733456 (PRODUCT.md's Positioning "
        "section)",
    ),
    # False claim, retired pre-emptively: "cannot modify it" as a
    # description of the reviewer's tree access. `git log -S"cannot modify
    # it" -- '*.md'` returns nothing in this repo's history (verified
    # 2026-08-22) — this entry cites the 6ef8921ae -> da3599ae4 revert as
    # PRECEDENT for the failure mode, not as this exact phrase's source.
    ReviewerClaim(
        "reviewer-cannot-modify",
        ("security", "product"),
        "cannot modify it",
        "the reviewer session cannot modify it, so any change on disk is "
        "impossible.",
        "guard.py:WRITE_TOOLS/evaluate — Bash can still write via shell "
        "redirection; precedent for the failure mode: da3599ae4 over "
        "6ef8921ae",
    ),
]

_REVIEWER_CLAIM_SURFACE_CASES = [
    (entry, surface) for entry in RETIRED_REVIEWER_CLAIMS for surface in entry.surfaces
]

_LEGITIMATE_READ_ONLY_SENTENCES = [
    # `nh investigate` (cli/commands.py) is a genuinely read-only command.
    "`nh investigate` runs a read-only investigation of the repo and writes "
    "nothing to disk.",
    # docs/BACKENDS.md:144, verbatim.
    "The mitigation that *is* real prevention is the sandbox: coder "
    "sessions run `--sandbox workspace-write`, read-only sessions "
    "`--sandbox read-only`.",
    # docs/adapters.md:223, verbatim.
    "Read-only gatherers run in parallel with a per-source timeout; one "
    "slow/bad source can't abort the rest.",
    # docs/verification.md:76, verbatim.
    "puts each one, independently, to a fresh bounded judge call (max one "
    "turn) with the diff and read-only file access.",
    # PRODUCT.md:60, verbatim — the CORRECTED wording itself, which must not
    # trip its own guard.
    "file-edit tools, git and forge writes and subagents refused (Bash "
    "stays, so the session is not read-only), told to refute \"done\".",
]


def _retired_reviewer_hits(texts_by_surface: dict[str, str]) -> list[str]:
    """Run every RETIRED_REVIEWER_CLAIMS entry against the surfaces it
    covers. `texts_by_surface` maps surface key ("security"/"product") to
    ALREADY-NORMALIZED text (see `_claim_text`/`_normalize_claim_text`).
    Returns one `"<claim_id>@<surface>"` string per hit; empty means clean.

    Shared by the absence test (fed the real files) and the injection test
    (fed a mutated tmp_path copy re-read from disk), so a mutant is proven to
    be the thing actually loaded, not a re-implementation of the match.
    """
    hits: list[str] = []
    for entry in RETIRED_REVIEWER_CLAIMS:
        for surface in entry.surfaces:
            text = texts_by_surface.get(surface)
            if text is None:
                continue
            if entry.phrase in text:
                hits.append(f"{entry.claim_id}@{surface}")
    return hits


@pytest.mark.parametrize("entry", RETIRED_REVIEWER_CLAIMS, ids=lambda e: e.claim_id)
def test_retired_reviewer_claim_has_not_returned(entry):
    """Mirrors test_retired_false_claim_has_not_returned above, scoped to the
    two surfaces DOCUMENTED_SURFACES/`documented` does not cover. A
    regression here means a stale-base landing carried a false
    reviewer-session claim back into docs/security.md or PRODUCT.md, exactly
    the way da3599ae4 did."""
    texts: dict[str, str] = {}
    for surface, path in REVIEWER_CLAIM_SURFACES.items():
        if _reviewer_surface_missing(surface, path):
            continue  # PRODUCT.md: DROP-classified, absent from this tree
        start_prefix, next_prefix = _SECTION_BOUNDS[surface]
        raw_section = _section(path.read_text(encoding="utf-8"), start_prefix, next_prefix)
        assert raw_section.strip(), f"scoped section for {surface!r} is empty"
        texts[surface] = _claim_text(path, surface)
    assert texts, "no reviewer-claim surface exists in this tree at all"
    hits = [h for h in _retired_reviewer_hits(texts) if h.startswith(f"{entry.claim_id}@")]
    assert not hits, (
        f"docs reintroduce a claim review already disproved: "
        f"{entry.phrase!r} — {entry.why} (hits: {hits})"
    )


@pytest.mark.parametrize(
    "case", _REVIEWER_CLAIM_SURFACE_CASES, ids=lambda c: f"{c[0].claim_id}-{c[1]}"
)
def test_retired_reviewer_claim_entry_can_fail(tmp_path, case):
    """Proves the entry can fail — a RETIRED_REVIEWER_CLAIMS entry that
    matches nothing is the same vacuous green that let da3599ae4 through.

    Splices the entry's injection sentence into a COPY of the scoped
    section, with the retired phrase itself wrapped across a newline
    mid-phrase (exactly how da3599ae4's revert read on disk), re-reads the
    copy from disk, and asserts the checker flags it.
    """
    entry, surface = case
    source = REVIEWER_CLAIM_SURFACES[surface]
    if _reviewer_surface_missing(surface, source):
        pytest.skip(f"{source.name} is DROP-classified and absent from this tree")
    start_prefix, _next_prefix = _SECTION_BOUNDS[surface]
    original = source.read_text(encoding="utf-8")
    lines = original.splitlines()
    heading_index = next(i for i, line in enumerate(lines) if line.startswith(start_prefix))

    assert entry.phrase in entry.injection, (
        f"fixture bug: {entry.phrase!r} is not a substring of its own "
        f"injection {entry.injection!r}"
    )
    wrapped_phrase = _wrap_mid_phrase(entry.phrase)
    injected_sentence = entry.injection.replace(entry.phrase, wrapped_phrase, 1)
    # Splice one line after the heading: inside the scoped section. Some
    # injections quote unrelated text elsewhere in the sentence (e.g.
    # review-with-read-only-tools ends in `told to refute "done"`) — that
    # quoted span gets blanked by _normalize_claim_text's quote-strip, but
    # the retired phrase itself is never inside quote marks in any of these
    # injections, so the strip cannot erase what this test is proving
    # detectable.
    lines.insert(heading_index + 1, injected_sentence)

    copy_path = tmp_path / source.name
    copy_path.write_text("\n".join(lines), encoding="utf-8")

    text = _claim_text(copy_path, surface)
    hits = _retired_reviewer_hits({surface: text})
    assert f"{entry.claim_id}@{surface}" in hits, (
        f"injecting {entry.phrase!r} (wrapped mid-phrase) into a copy of "
        f"{surface} did not trip the guard — hits: {hits}"
    )


# The literal two lines da3599ae4 introduced over docs/security.md, verbatim
# from `git diff 6ef8921ae da3599ae4 -- docs/security.md`.
_DA3599AE4_HUNK = (
    "the git layer. During review the backend runs **read-only**: all write "
    "tools are\nblocked unconditionally."
)


def test_the_actual_reverted_hunk_is_caught():
    """The literal historical regression, not a phrase invented for this
    ticket. Proves the guard catches the actual event da3599ae4 produced."""
    normalized = _normalize_claim_text(_DA3599AE4_HUNK)
    hits = _retired_reviewer_hits({"security": normalized, "product": normalized})
    assert hits, f"the actual da3599ae4 hunk produced no hits: {_DA3599AE4_HUNK!r}"


@pytest.mark.parametrize("surface", sorted(REVIEWER_CLAIM_SURFACES), ids=str)
@pytest.mark.parametrize(
    "sentence", _LEGITIMATE_READ_ONLY_SENTENCES, ids=lambda s: s[:32]
)
def test_legitimate_read_only_uses_stay_green(tmp_path, surface, sentence):
    """Legitimate uses of "read-only" — describing `nh investigate`, a Codex
    sandbox flag, the context gatherers, the verifier's file access, or
    PRODUCT.md's own corrected negation — must never trip these entries.
    Every RETIRED_REVIEWER_CLAIMS phrase is a long, specific false claim for
    exactly this reason (fact 4 in the plan: no entry matches bare
    "read-only")."""
    source = REVIEWER_CLAIM_SURFACES[surface]
    if _reviewer_surface_missing(surface, source):
        pytest.skip(f"{source.name} is DROP-classified and absent from this tree")
    start_prefix, _next_prefix = _SECTION_BOUNDS[surface]
    original = source.read_text(encoding="utf-8")
    lines = original.splitlines()
    heading_index = next(i for i, line in enumerate(lines) if line.startswith(start_prefix))
    lines.insert(heading_index + 1, sentence)

    copy_path = tmp_path / f"{surface}-{source.name}"
    copy_path.write_text("\n".join(lines), encoding="utf-8")

    text = _claim_text(copy_path, surface)
    hits = _retired_reviewer_hits({surface: text})
    assert not hits, f"legitimate sentence tripped the guard: {sentence!r} — {hits}"


def test_scoped_surfaces_still_carry_the_corrected_prose():
    """Anti-vacuity bound on the quote-strip in `_normalize_claim_text`, and
    a pin on bd0733456's wording: if that prose is reworded, or the
    retraction sentence is deleted instead of the guard being fixed, this
    test turns red instead of the RETIRED_REVIEWER_CLAIMS tests passing
    vacuously (green because there is nothing left to check).

    docs/security.md is SHIP-classified and its half runs unconditionally —
    every tree must have it. PRODUCT.md is DROP-classified
    (EXPORT_CLASSIFICATION.txt:687) and absent from the exported/public
    tree; its half skips there instead of hard-failing on a file that was
    intentionally dropped, but runs at full strength whenever the file IS
    present (see `_reviewer_surface_missing`).
    """
    assert not _reviewer_surface_missing("security", SECURITY_MD)
    security_raw = _section(
        SECURITY_MD.read_text(encoding="utf-8"), *_SECTION_BOUNDS["security"]
    )
    security_collapsed = re.sub(r"\s+", " ", security_raw).lower()
    security_stripped = _normalize_claim_text(security_raw)

    removed_fraction = 1 - (len(security_stripped) / len(security_collapsed))
    assert removed_fraction < 0.15, (
        f"security: quote-strip removed {removed_fraction:.0%} of the "
        f"section — it is supposed to blank one quoted retraction, not "
        f"rewrite the prose"
    )
    assert "bash stays" in security_stripped
    assert "file-edit tools" in security_stripped

    retracted_claim = "read-only: all write tools are blocked unconditionally"
    assert "until 2026-08-22" in security_collapsed, (
        "the retraction sentence marking when the false claim was corrected "
        "is gone from docs/security.md §3"
    )
    assert retracted_claim in security_collapsed, (
        "the retraction sentence's quoted false claim is gone from "
        "docs/security.md — was bd0733456's retraction deleted?"
    )
    assert retracted_claim not in security_stripped, (
        "the quote-strip in _normalize_claim_text no longer blanks the "
        "quoted false claim — RETIRED_REVIEWER_CLAIMS would trip on the "
        "retraction sentence that documents the fix"
    )

    if _reviewer_surface_missing("product", PRODUCT_MD):
        pytest.skip(f"{PRODUCT_MD.name} is DROP-classified and absent from this tree")
    product_raw = _section(
        PRODUCT_MD.read_text(encoding="utf-8"), *_SECTION_BOUNDS["product"]
    )
    product_collapsed = re.sub(r"\s+", " ", product_raw).lower()
    product_stripped = _normalize_claim_text(product_raw)

    removed_fraction = 1 - (len(product_stripped) / len(product_collapsed))
    assert removed_fraction < 0.15, (
        f"product: quote-strip removed {removed_fraction:.0%} of the "
        f"section — it is supposed to blank one quoted retraction, not "
        f"rewrite the prose"
    )
    assert "the session is not read-only" in product_stripped


def test_every_documented_cli_command_exists(documented):
    """Commands are cheap to document and easy to rename out from under a doc.

    Checks the SUBCOMMAND too: ``cli.commands`` is a flat dict of top-level
    names, so a test that stops there passes on `nh task addd` — false
    confidence, which is worse than no test.

    Scope, stated so nobody over-reads a green run: only commands at the start
    of a line (i.e. in the usage code blocks) are checked. Commands mentioned
    inline cannot be, because the README legitimately names one that does NOT
    exist — "There is no `nh stop`" — and a guard that fails on a true sentence
    is worse than a narrower one.
    """
    import click

    from no_human.cli.commands import cli

    unknown: list[str] = []
    for name, sub in re.findall(r"^nh (\S+)(?:\s+(\S+))?", documented, re.M):
        cmd = cli.commands.get(name)
        if cmd is None:
            unknown.append(f"nh {name}")
            continue
        # Only treat the next token as a subcommand when it looks like one —
        # `nh task add <url>` vs `nh approve <id>`.
        if not (sub and re.fullmatch(r"[a-z][a-z-]*", sub)):
            continue  # a placeholder like <id>, a flag, or nothing
        if isinstance(cmd, click.Group):
            if sub not in cmd.commands:
                unknown.append(f"nh {name} {sub}")
            continue
        # Not a group: the token may still be a fixed choice, e.g.
        # `nh config show` is a click.Choice argument, not a subcommand.
        for param in cmd.params:
            choices = getattr(param.type, "choices", None)
            if isinstance(param, click.Argument) and choices:
                if sub not in choices:
                    unknown.append(f"nh {name} {sub}")
                break
    assert not unknown, (
        f"README documents commands that do not exist: {sorted(set(unknown))}"
    )


@pytest.mark.parametrize("surface", DOCUMENTED_SURFACES, ids=lambda p: p.name)
def test_local_links_resolve(surface):
    """A broken link on the front page is the cheapest possible own-goal.

    Widened 2026-08-01 from the README to every DOCUMENTED_SURFACE. It is
    PARAMETRISED rather than fed the concatenated text, because a relative link
    resolves against the file that wrote it: ``blockers.md`` and
    ``../src/no_human/config.py`` in docs/verification.md mean different paths
    from the same strings in README.md. Resolving them all against the repo root
    — what the union fixture would force — would report false breaks and, worse,
    silently pass a real one that happened to exist at the root.

    docs/verification.md alone carries 19 local links and had no check at all
    between the 2026-08-01 relocation and this widening.
    """
    text = surface.read_text(encoding="utf-8")
    # `[text](path "title")` is valid Markdown — the title is not part of the
    # path, so stop at the first whitespace or the link resolves to nothing.
    targets = [t.split()[0] for t in re.findall(r"\]\((?!https?:)([^)#]+)", text) if t.strip()]
    assert targets, f"{surface.name} has no local links — is this guard pointed at the right file?"
    broken = [t for t in targets if not (surface.parent / t).exists()]
    assert not broken, f"{surface.name} links to missing files: {broken}"


BENCH_REPORT = REPO / "docs" / "NORTH_STAR_BENCH.md"


@pytest.fixture(scope="module")
def bench_report() -> str:
    return BENCH_REPORT.read_text(encoding="utf-8")


def test_published_bench_report_is_internally_consistent(bench_report):
    """The published figures must agree with each other.

    RETARGET (2026-07-30). This was ``test_readme_bench_figures_match_the_
    published_report``: it required the README to restate the report's label,
    success fraction, percentage, cost ratio and escalation ratio, so a
    ``bench publish`` could not silently stale the front page. The rewrite
    removed those figures from the README on purpose — the run is self-run and
    its corpus does not resolve on anyone else's machine, so republishing a
    47% headline was the claim least defensible on a public page.

    Asserting that the report contains its own numbers would be vacuous, so the
    coupling is re-pointed at the one thing about the report that IS checkable
    without the README: the headline figures are derived quantities and must
    reconcile. ``docs/NORTH_STAR_BENCH.md`` is machine-written and says "do not
    edit by hand" — and until now NOTHING enforced that. A hand-edited success
    percentage, or a delivered/escalated split that does not sum to the
    satisfied count, now fails here. That is coverage the old test did not have
    at all: it would have happily confirmed the README faithfully echoed a
    doctored report.
    """
    satisfied, ran = (int(x) for x in re.search(
        r"Success \(goal satisfied, unattended\): (\d+)/(\d+)",
        bench_report).groups())
    pct = int(re.search(r"Success \(goal satisfied.*?\((\d+)%\)", bench_report).group(1))
    delivered, escalated = (int(x) for x in re.search(
        r"of which (\d+) DELIVERED a change and (\d+) correctly ESCALATED",
        bench_report).groups())
    esc_pct, esc_n, esc_d = (int(x) for x in re.search(
        r"Honest-escalation rate on gated tasks: (\d+)% \((\d+)/(\d+)\)",
        bench_report).groups())

    assert round(satisfied / ran * 100) == pct, (
        f"published success {satisfied}/{ran} rounds to "
        f"{round(satisfied / ran * 100)}%, but the report states {pct}%"
    )
    assert delivered + escalated == satisfied, (
        f"published split {delivered} delivered + {escalated} escalated = "
        f"{delivered + escalated}, but the report states {satisfied} satisfied"
    )
    assert round(esc_n / esc_d * 100) == esc_pct, (
        f"published honest-escalation {esc_n}/{esc_d} rounds to "
        f"{round(esc_n / esc_d * 100)}%, but the report states {esc_pct}%"
    )


def test_documented_surfaces_do_not_carry_a_stale_bench_label(
    documented, bench_report
):
    """No documented surface may name a bench run other than the published one.

    The original defect was the README describing v8 while linking a v13 report,
    with the suite green throughout.

    RETARGET (2026-08-01). This read the README alone, and when the operator cut
    the Limits section the README stopped mentioning the benchmark at all. The
    repair attempt made the link assertion CONDITIONAL on a benchmark claim being
    present — which made the whole test vacuous, since the shipped README trips
    none of the trigger words. Proven at the time and re-proven since: mutating
    the published report's label to ``expanded-core-v99`` left this test PASSING.
    The conditional's vocabulary was drawn from the same regex that decided
    whether to check anything, so the mutation could never reach it — a verifier
    built out of the thing it verifies.

    The fix is the one every sibling guard in this file already uses: read
    ``DOCUMENTED_SURFACES`` (:46-50), not the README alone. The claim did not
    disappear when it left the front page — it moved to ``docs/verification.md``,
    which links the report. So the mandatory-hit assertion can go back to being
    unconditional without dictating a word of the README: it is satisfied today
    by ``docs/verification.md:95-115``, it is phrasing-independent, and it fires
    only if the report goes UNREFERENCED from every documented surface, which is
    the rot — a claim separated from its evidence.

    That also restores the file's own policy at :44-45, which forbids removing
    the mandatory-hit assertions that stop a guard passing vacuously. The
    conditional violated it while leaving the policy text unedited.

    Stated plainly so it is not mistaken for coverage: the label-disagreement
    assertion is DORMANT today, because no documented surface names a run label
    at all. It arms itself the moment one does, which is the point of reading
    the union. The mandatory hit below is what makes this test able to fail
    right now — verified by mutation in both directions: dropping the reference
    from every surface fails it, and adding ``expanded-core-v8`` to the README
    fails it. Mutating the published label alone still passes, and that is
    correct rather than vacuous: with no label written down anywhere, there is
    nothing for the report to disagree WITH.
    """
    label = re.search(r"label: (\S+)", bench_report).group(1).rstrip(".")
    named = set(re.findall(r"\b(expanded-core-v\d+)\b", documented))
    assert not (named - {label}), (
        f"documented surfaces name bench run(s) {sorted(named - {label})}; the "
        f"published report is {label!r} — the docs and the report disagree"
    )
    # Mandatory hit. Unconditional on purpose: see the docstring. Matched on the
    # bare filename rather than a path, so moving the reference between surfaces
    # (or writing it as a relative link) does not break the coupling.
    assert "NORTH_STAR_BENCH.md" in documented, (
        "no documented surface links NORTH_STAR_BENCH.md; the benchmark claim "
        "and its evidence are no longer connected from anywhere a reader lands"
    )


# --- egress: the docs must not claim an enumeration they cannot back ---------
#
# README and docs/security.md both said prompts were the only thing that left
# the machine ("The only thing sent about your code is the prompt", "Two things,
# and nothing else"). Both were false when written, and the second was worse
# than the first because it read as an audited enumeration.
#
# These guards are anchored to the MECHANISM, not to wording, and each carries a
# mandatory hit that fails if its subject disappears from the source — the
# policy at :44-45. They check two things a trust document must get right:
# (a) the terminal push is disclosed, and (b) no surface re-asserts a closed
# enumeration of egress, which no process handing an agent an unrestricted shell
# can honestly make.

SECURITY_DOC = REPO / "docs" / "security.md"
VCS_INIT = REPO / "src" / "no_human" / "vcs" / "__init__.py"
CLAUDE_BACKEND = REPO / "src" / "no_human" / "agent" / "claude_backend.py"


@pytest.fixture(scope="module")
def security_doc() -> str:
    return SECURITY_DOC.read_text(encoding="utf-8")


#: Invisible anchors around the push-egress bullet in ``docs/security.md`` §7.
#:
#: RE-ANCHORED 2026-08-02. The previous marker was the bullet's own heading
#: prose (``- **`git push` of the task branch to your git remote**``). That was
#: correct about WHAT to protect and wrong about HOW: three rewordings that left
#: the disclosure completely intact still failed the guard (measured — rewording
#: the heading, restating the no-opt-out sentence, and changing the citation
#: spelling). Anchoring on prose means every edit to the prose is a test change,
#: which trains a reader to "fix" the guard rather than read it.
#:
#: An HTML comment renders as nothing in every Markdown viewer, so the reader
#: never sees these, the author can reword the bullet freely, and DELETING the
#: bullet still takes the anchors with it — which is the asymmetry this guard
#: wants. The strictness that matters is preserved below, on the *content*
#: between the anchors, not on its wording.
PUSH_BULLET_OPEN = "<!-- egress:push -->"
PUSH_BULLET_CLOSE = "<!-- /egress:push -->"
#: Inner anchors, around the sentence that says the egress cannot be turned off.
#: Nested on purpose: without them, "reword freely" would also permit deleting
#: the load-bearing half of the disclosure while leaving a bullet behind, and
#: RED 3 of the red-green matrix (drop only the no-opt-out claim) would stop
#: failing. The anchors travel with the sentence, so deleting it fails.
PUSH_NO_OPTOUT_OPEN = "<!-- egress:push:no-optout -->"
PUSH_NO_OPTOUT_CLOSE = "<!-- /egress:push:no-optout -->"

#: The no-opt-out slice must still make a negative claim about disabling. This
#: is a REQUIRED vocabulary, not a banned one, and the direction matters: an
#: unlisted spelling produces a loud failure on a doc edit (cheap, and the next
#: reader adds the spelling), where dropping the check entirely would let the
#: sentence be gutted in place — "**This ships your source to your git host,
#: and**" — with the anchors still present and the suite still green. That
#: silent direction is the one this file has already been burned by.
_NO_OPTOUT_NEGATIONS = ("no ", "not ", "cannot", "can't", "never", "nothing")
_NO_OPTOUT_DISABLERS = (
    "disable", "disabled", "disables", "turn it off", "turned off",
    "turn off", "switch it off", "switched off", "opt out", "opt-out",
)


def _slice_between(text: str, open_marker: str, close_marker: str) -> str:
    """The text between two markers, or "" if either is missing/out of order."""
    start = text.find(open_marker)
    if start == -1:
        return ""
    end = text.find(close_marker, start + len(open_marker))
    if end == -1:
        return ""
    return text[start + len(open_marker):end]


def _push_egress_bullet(security_doc: str) -> str:
    """Return just the push bullet's text from §7, or "" if it is gone.

    Scoped deliberately. An older version of this guard asserted over the whole
    §7 body, which meant the *fetch* bullet's `vcs/git.py` citation satisfied the
    "the push names a source location" assertion — deleting the push bullet
    entirely left the suite green (reproduced 2026-08-01). The slice is now
    marker-delimited rather than prose-delimited, so it keeps that scoping
    without also failing on rewordings.
    """
    section = security_doc.split("## 7.", 1)
    assert len(section) == 2, "docs/security.md has no '## 7.' egress section"
    return _slice_between(section[1], PUSH_BULLET_OPEN, PUSH_BULLET_CLOSE)


def _pushes_inside_open_pr(source: str) -> bool:
    """True iff ``open_pr`` really contains a ``.push(...)`` **call**.

    Parsed, not grepped. A whole-file substring test for ``repo.push(`` was
    satisfied by the docstring-comment at ``vcs/__init__.py:26``: renaming the
    live call site at :72 to ``repo.pushX(`` left the suite green (reproduced
    2026-08-01). Comments and strings are invisible to the AST, so they cannot
    stand in for the mechanism here.

    RELAXED 2026-08-02: the receiver is no longer constrained to the *name*
    ``repo``. Requiring ``func.value.id == "repo"`` made this wrong on two of
    six refactors that keep the mechanism fully intact — an aliased receiver
    (``_r = repo; _r.push(...)``) and an attribute chain
    (``self.repo.push(...)``) both read as "the push is gone" and would have
    sent a reader to re-check a doc that was still correct. What this guard is
    for is detecting that ``open_pr`` no longer pushes at all; the receiver's
    spelling is not part of that claim. The two checks that carry the weight are
    unchanged: it must be a `Call` (so a comment or a string cannot satisfy it)
    and the attribute must be exactly ``push`` (so RED 2, renaming the live call
    site to ``repo.pushX(``, still fails).

    🔴 What this does NOT check, stated so a green run is not over-read: syntactic
    presence is not reachability. ``if False: repo.push(branch)`` satisfies it.
    Accepted deliberately rather than fixed — deciding reachability needs a
    control-flow analysis, and the failure mode it would buy is someone
    deliberately disguising the removal of the push while leaving the call in
    the source. The realistic defect is the push being deleted, moved or
    renamed, which is what this catches. A reader who wants the stronger claim
    should read ``open_pr``; this guard's job is to fail when the doc's subject
    has left the file.
    """
    import ast

    defs = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, defs) or node.name != "open_pr":
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "push"
            ):
                return True
    return False


def test_the_push_that_ends_every_task_is_disclosed_as_egress(security_doc):
    """`open_pr` pushes the user's source. The trust document must say so.

    Mandatory hit first: a real ``.push(...)`` call inside ``open_pr`` in
    ``vcs/__init__.py``, and `open_pr(` in the orchestrator. If either vanishes
    this test fails loudly rather than passing over a mechanism that is no
    longer there — the failure then means "re-check the doc", not "the doc is
    wrong".

    Then the doc side, anchored to the bullet's invisible HTML-comment markers
    rather than to the §7 body or to the bullet's prose, so that DELETING the
    disclosure is what fails the test and REWORDING it is free. Every arm was
    watched failing; the matrix and the benign-rewording tests below are the
    record.

    WHY THIS IS SEPARATE FROM ``tests/test_egress_disclosure.py``, which also
    guards §7 — do not merge them for tidiness. That guard asks "does every
    module with an outbound call appear ANYWHERE in §7"; this one asks "is the
    ONE channel that cannot be turned off still disclosed, and still true".
    Measured against each other's known positives, neither covers the other:

      known positive                          drift guard   this guard
      delete the whole push bullet ........... FAIL          FAIL
      drop ONLY the no-opt-out sentence ...... pass          FAIL
      gut that claim in place, anchors kept .. pass          FAIL
      strip the push bullet's citation ....... FAIL          FAIL
      plant an undisclosed httpx module ...... FAIL          pass

    The two middle rows are why a merged test would be weaker: the drift guard
    matches module PATHS, so gutting the sentence that says the egress cannot be
    disabled leaves every path in place and it stays green. The last row is why
    this one cannot absorb that guard: it never looks at modules at all.
    """
    vcs_init = VCS_INIT.read_text(encoding="utf-8")
    orch = (REPO / "src" / "no_human" / "core" / "orchestrator.py").read_text(
        encoding="utf-8")
    assert _pushes_inside_open_pr(vcs_init), (
        "vcs/__init__.py no longer calls repo.push(...) inside open_pr — the "
        "egress this guard exists to keep disclosed has moved; re-point it"
    )
    assert "open_pr(" in orch, (
        "the orchestrator no longer calls open_pr — re-point this guard"
    )

    _assert_push_bullet_discloses(security_doc)


def _assert_push_bullet_discloses(security_doc: str) -> None:
    """The doc half of the guard above, over any §7 text.

    Split out so the red-green matrix and the benign-rewording cases below run
    the REAL assertions against mutated documents, instead of a paraphrase of
    them that could drift from what ships.
    """
    bullet = _push_egress_bullet(security_doc)
    assert bullet.strip(), (
        f"docs/security.md §7 no longer carries the {PUSH_BULLET_OPEN}…"
        f"{PUSH_BULLET_CLOSE} bullet; shipping the user's source to their git "
        "host is the largest thing that leaves the machine and it cannot be "
        "an omission. These anchors are HTML comments precisely so that "
        "REWORDING the bullet needs no change here — if you are reading this "
        "message you removed the bullet or its anchors, and the fix is to put "
        "the disclosure back, not to delete this assertion."
    )

    no_optout = _slice_between(
        bullet, PUSH_NO_OPTOUT_OPEN, PUSH_NO_OPTOUT_CLOSE
    ).lower()
    assert no_optout.strip(), (
        f"the push-egress bullet no longer carries a {PUSH_NO_OPTOUT_OPEN}…"
        f"{PUSH_NO_OPTOUT_CLOSE} sentence — that the egress cannot be turned "
        "off is the load-bearing half of the disclosure, not a flourish"
    )
    assert any(n in no_optout for n in _NO_OPTOUT_NEGATIONS) and any(
        d in no_optout for d in _NO_OPTOUT_DISABLERS
    ), (
        f"the no-opt-out sentence ({no_optout.strip()!r}) no longer says that "
        "the push cannot be disabled. If it says so in a spelling this guard "
        "does not know, ADD the spelling to _NO_OPTOUT_NEGATIONS / "
        "_NO_OPTOUT_DISABLERS — the check is a required vocabulary, and a "
        "missing spelling is meant to fail loudly rather than pass silently."
    )

    assert (
        "vcs/__init__.py" in bullet
        or "vcs/git.py" in bullet
        or "GitRepo.push" in bullet
    ), (
        "the push-egress bullet names no source location for the push — a "
        "trust document's claims have to be checkable against the code. Note "
        "the neighbouring fetch bullet also cites vcs/git.py; this assertion "
        "is scoped to the push bullet so that citation cannot satisfy it"
    )


#: Placements of an anchor that are invisible to a reader of the SOURCE but not
#: to a reader of the RENDERED page. Both were measured against a CommonMark
#: renderer while writing the anchors above, and both shipped in a first draft:
#:
#:   * an HTML comment that STARTS a line inside a bullet list is parsed as an
#:     HTML block, which closes the list. The §7 list rendered as three separate
#:     `<ul>`s instead of one.
#:   * an anchor placed immediately after a list marker and immediately before
#:     `**` breaks the emphasis run: `**This ships your source…**` rendered as
#:     literal asterisks, i.e. the load-bearing sentence of a trust document
#:     lost its emphasis on the published page.
#:
#: Neither is detectable by any assertion about the doc's TEXT — the first draft
#: passed every content check in this file. So the shape is pinned here instead.
#: A renderer is deliberately NOT imported: the lean-stack rule forbids adding a
#: dependency, and these two shapes are the whole of what was measured to break.
def _anchor_placement_problems(security_doc: str) -> list[str]:
    bad: list[str] = []
    for n, line in enumerate(security_doc.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("<!--") and "egress:" in stripped:
            bad.append(
                f"docs/security.md:{n}: an egress anchor starts the line. A "
                f"line-initial HTML comment inside a list is an HTML block and "
                f"splits the list; put the anchor mid-line. Line: {stripped!r}"
            )
        if re.search(r"^\s*[-*]\s+<!--\s*/?egress:", line):
            bad.append(
                f"docs/security.md:{n}: an egress anchor directly follows the "
                f"list marker. Immediately before a `**` run this breaks the "
                f"emphasis. Move it after the bullet's bold heading."
            )
    return bad


def test_the_egress_anchors_are_invisible_when_rendered(security_doc):
    """The anchors must not change how docs/security.md renders.

    They exist so the bullet can be reworded without a test edit. An anchor that
    silently reformats a published trust page has cost more than it bought.
    """
    assert "egress:push" in security_doc, (
        "no egress anchors in docs/security.md at all — this shape guard would "
        "pass vacuously; the disclosure guard above is the one that failed first"
    )
    problems = _anchor_placement_problems(security_doc)
    assert not problems, "egress anchors are placed where they alter rendering:\n  " + "\n  ".join(problems)


#: Edits that change how the push-egress bullet READS while leaving what it
#: DISCLOSES completely intact. All three failed the prose-anchored version of
#: this guard (measured 2026-08-02, 3/3 false positives); all three must pass
#: now. They are held here as tests rather than as a note, because a guard whose
#: brittleness was changed without a false-positive suite is unverified — and
#: because the next person to reword the bullet should find out from a green run
#: that they were allowed to.
BENIGN_REWORDINGS = (
    (
        "reworded-heading",
        "- **`git push` of the task branch to your git remote**",
        "- **Pushing the task branch to whichever git remote you configured**",
    ),
    (
        "restated-no-optout",
        "there is no key that disables it",
        "no configuration key can disable it",
    ),
    (
        "symbol-citation-swapped",
        "`GitRepo.push` in `vcs/git.py`",
        "[`GitRepo.push`](../src/no_human/vcs/git.py)",
    ),
)


@pytest.mark.parametrize(
    "old,new", [(o, n) for _, o, n in BENIGN_REWORDINGS],
    ids=[i for i, _, _ in BENIGN_REWORDINGS],
)
def test_rewording_the_push_bullet_is_not_a_finding(security_doc, old, new):
    """A reword that keeps the disclosure must not fail the disclosure guard.

    The guard is deliberately brittle in the direction that matters — deleting
    the bullet, or gutting the no-opt-out sentence, still fails, and that is
    checked by the red-green matrix. Brittleness in THIS direction buys nothing
    and costs trust in the guard, so it is pinned closed.
    """
    assert old in security_doc, (
        f"the fixture for this false-positive case is stale: {old!r} is no "
        f"longer in docs/security.md, so this test is no longer exercising a "
        f"rewording of the shipped text. Re-point it at what the bullet says."
    )
    _assert_push_bullet_discloses(security_doc.replace(old, new))


#: (id, body of ``open_pr``, whether the mechanism is really still there).
#: The four attacks an independent review ran against the strict version, plus
#: the shipped shape and the comment-only shape. The strict version — which also
#: required ``func.value.id == "repo"`` — was WRONG on ``aliased-receiver`` and
#: ``attribute-chain``: it reported the push gone while it was right there.
PUSH_SHAPES = (
    ("shipped", "    pushed_sha = repo.push(branch)\n", True),
    ("aliased-receiver", "    _r = repo\n    pushed_sha = _r.push(branch)\n", True),
    ("attribute-chain", "    pushed_sha = self.repo.push(branch)\n", True),
    ("call-through-index", "    pushed_sha = repos[0].push(branch)\n", True),
    # RED 2: the live call site renamed. The mechanism really is gone.
    ("renamed-call", "    pushed_sha = repo.pushX(branch)\n", False),
    # The defect that made the round-2 version unable to fail: a comment.
    ("comment-only", "    # repo.push(branch) happens here\n    pass\n", False),
    # Moved to a helper: open_pr itself no longer pushes, so this guard should
    # fail loudly and send a reader to re-point it. That is the safe direction.
    ("moved-to-helper", "    pushed_sha = _do_the_push(repo, branch)\n", False),
)


@pytest.mark.parametrize(
    "body,expected", [(b, e) for _, b, e in PUSH_SHAPES],
    ids=[i for i, _, _ in PUSH_SHAPES],
)
def test_the_open_pr_push_detector_reads_calls_not_names(body, expected):
    """`_pushes_inside_open_pr` over every shape the mechanism is known to take.

    Held here so the detector's reach is a regression test rather than a claim
    in a commit message. A comment cannot satisfy it (that was the round-2
    HIGH), renaming the call still fails it, and the receiver's spelling is
    correctly irrelevant.
    """
    assert _pushes_inside_open_pr(f"def open_pr(repo, branch):\n{body}") is expected


def test_the_unbounded_egress_channel_is_named(security_doc):
    """The coder session has Bash and no tool allowlist. Say it, don't imply it.

    Mandatory hit: the default really is `bypassPermissions` and there really is
    no `allowed_tools`/`disallowed_tools` restriction. If someone ADDS a
    restriction, this fails — correctly, because the doc would then be
    overstating the risk and needs rewriting in the other direction.
    """
    backend = CLAUDE_BACKEND.read_text(encoding="utf-8")
    assert 'permission_mode: str = "bypassPermissions"' in backend, (
        "claude_backend no longer defaults to bypassPermissions — the egress "
        "doc's central caveat may now be wrong; re-read it"
    )
    assert "allowed_tools" not in backend, (
        "claude_backend now restricts tools — docs/security.md says the coder "
        "session is unrestricted, and that is no longer true"
    )

    body = security_doc.split("## 7.", 1)[1]
    assert "bypassPermissions" in body, (
        "the egress section does not name the unbounded channel; without it "
        "the rest reads as a complete enumeration, which it is not"
    )


@pytest.mark.parametrize(
    "claim",
    [
        "Only prompts leave your machine",
        "The only thing sent about your code is the prompt",
        "Two things, and nothing else",
    ],
    ids=["only-prompts", "only-thing-sent", "two-things"],
)
def test_no_surface_re_asserts_a_retired_exhaustive_egress_claim(
    documented, security_doc, claim
):
    """A closed list of sentences that shipped and were false.

    Deliberately a regression pin on exact retired strings, not a style rule: a
    general "don't say only" matcher would be a phrasing heuristic, and this
    file has already been burned once by a guard whose vocabulary decided its
    own reach. These three are pinned because each one WAS in the tree.

    Occurrences inside the History subsection are expected — the doc quotes the
    claims in order to retract them — so they are excluded by looking only at
    the text before it.
    """
    haystack = documented + "\n\n" + security_doc.split("### History", 1)[0]
    assert claim.lower() not in haystack.lower(), (
        f"a documented surface asserts {claim!r} again. It is false: open_pr "
        f"pushes the user's source at the end of every task, and the coder "
        f"session's egress is unbounded. See docs/security.md section 7."
    )


@pytest.mark.parametrize("doc_name", ["README.md", "docs/quickstart.md"])
def test_the_onboarding_docs_name_the_command_that_verifies_the_install(doc_name):
    r"""`nh doctor` must appear in the two documents a new user actually reads.

    Found by the adoption harness (ADOPT-4, 2026-08-02). `nh doctor` is the one
    command that tells someone whether their install is real -- it is what
    distinguishes "the commands ran" from "the product works". It was documented
    in `adapters.md`, `configuration.md`, `KNOWN_ISSUES.md` and a design doc, and
    appeared **zero** times in the README and the quickstart. A persona following
    only the public onboarding path could not discover it, which is precisely the
    population that needs it.

    Asserted POSITIVELY and per-document on purpose. The retired-claim tests in
    this file are negative assertions that an empty file would satisfy; this one
    fails if the mention is ever dropped from either document, and naming the
    document in the parametrisation means the failure says which one.
    """
    doc = (REPO / doc_name).read_text(encoding="utf-8")
    assert "nh doctor" in doc, (
        f"{doc_name} never mentions `nh doctor`. It is the only command that "
        f"verifies an install is real, and a user following only the public "
        f"onboarding path has no way to find it. See ADOPT-4."
    )


# --- docs-vs-code audit (2026-08-23): stale claims in security/eval/known-issues
#
# A supervising session's audit found docs/security.md, docs/eval.md and
# docs/KNOWN_ISSUES.md each asserting something the code no longer does, or
# citing a line the code had moved off of: an overclaimed merge-denial
# guarantee, a merge-time check that was never implemented, a red-team task
# count and judge model that had grown/changed, and a `nh stop` default that
# had been widened. Fixed this round. These guards are the mechanism that
# keeps each fix from rotting the same way it broke the first time.

EVAL_MD = REPO / "docs" / "eval.md"
KNOWN_ISSUES_MD = REPO / "docs" / "KNOWN_ISSUES.md"
VERIFICATION_MD = REPO / "docs" / "verification.md"


@pytest.fixture(scope="module")
def eval_doc() -> str:
    return EVAL_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def known_issues_doc() -> str:
    return KNOWN_ISSUES_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def verification_doc() -> str:
    return VERIFICATION_MD.read_text(encoding="utf-8")


# --- merge-denial must not overclaim what the guard models -------------------
#
# `agent/guard.py` denies `gh pr merge` / `glab mr merge` (and the
# `merge-stack` wrapper) for the SPELLINGS the lexical rule plus the
# argv-shaped check model — not "regardless" of spelling, and not "in every
# mode" as an unqualified universal. Two of three such sentences were
# corrected; the third (the `nh approve` bullet, which really is denied in
# both session modes because it is the operator's own command) is deliberately
# untouched and out of scope for this guard.

_EGRESS_PUSH_SPAN = (PUSH_BULLET_OPEN, PUSH_BULLET_CLOSE)


def _merge_stack_bullet(security_doc: str) -> str:
    """The `nh merge-stack run` bullet's text, or "" if the heading moved."""
    marker = "**`nh merge-stack run` calls `gh pr merge`**"
    start = security_doc.find(marker)
    if start == -1:
        return ""
    end = security_doc.find("\n- **", start + len(marker))
    if end == -1:
        end = len(security_doc)
    return security_doc[start:end]


def test_security_md_does_not_overclaim_merge_denial(security_doc):
    """Neither the push-egress bullet nor the merge-stack bullet may claim the
    guard denies merge "regardless" of spelling or "in every mode" as a bare
    universal — it denies the spellings the matcher models, in both session
    modes, which is a narrower and truer claim.

    Scoped deliberately to these two spans. The `nh approve` bullet's "denied
    it in every mode" is a THIRD, correct instance (approve really is denied
    in both modes, unconditionally, because it is the operator's own local
    command) and is out of scope for this guard on purpose — asserting over
    the whole document would make this guard fail on the sentence it must
    leave alone.
    """
    push_bullet = _slice_between(security_doc, *_EGRESS_PUSH_SPAN)
    merge_stack_bullet = _merge_stack_bullet(security_doc)
    assert push_bullet, "the push-egress span is empty; see the anchors above"
    assert merge_stack_bullet, (
        "the `nh merge-stack run` bullet was not found by its heading text; "
        "it may have been reworded or removed"
    )
    for label, span in (("push-egress", push_bullet), ("merge-stack", merge_stack_bullet)):
        lowered = span.lower()
        assert "regardless" not in lowered, (
            f"the {label} bullet still says the merge guard fires "
            f"'regardless' of spelling — it fires for the spellings the "
            f"matcher models, not every spelling"
        )
        assert "denied it in every mode" not in lowered, (
            f"the {label} bullet still claims denial 'in every mode' as a "
            f"bare universal — narrow it to the spellings the guard models"
        )
    for span in (push_bullet, merge_stack_bullet):
        assert "gh pr merge" in span, (
            "a merge-denial bullet dropped the concrete command it denies"
        )


#: Every backticked `gh …` / `glab …` shell literal in docs/security.md, as of
#: this fix. Not an exhaustive-coverage claim about what the guard denies —
#: a floor on the table so nobody quietly re-publishes a closed enumeration of
#: "every spelling the guard covers" under cover of this fix.
_GH_GLAB_LITERAL_RE = re.compile(r"`((?:gh|glab) [a-z][a-z -]*)`")


def test_no_new_exhaustive_coverage_list_is_published(security_doc):
    """The fix must not trade one overclaim ("regardless") for another (a
    closed list of every spelling the matcher happens to cover today).

    Pinned to the count measured right after the fix landed. A genuinely new,
    load-bearing command mention would grow this by one and should be looked
    at, not silently waved through by a guard with no ceiling at all.
    """
    literals = _GH_GLAB_LITERAL_RE.findall(security_doc)
    assert len(literals) <= 5, (
        f"docs/security.md now names {len(literals)} `gh`/`glab` command "
        f"literals ({sorted(set(literals))}); this looks like a new exhaustive "
        f"coverage list of the merge guard's modelled spellings, which the fix "
        f"deliberately avoided publishing. If this growth is legitimate "
        f"(unrelated command documentation), raise the ceiling with a note "
        f"saying why."
    )


# --- approve-time control: not implemented, must not be promised -------------
#
# `is_agent_session` is a real, used symbol — just not inside `approve` /
# `approve_task`. The doc must say the control is not implemented, not send a
# reader to rely on a check that does not run at approve time.

ORCHESTRATOR_PY = REPO / "src" / "no_human" / "core" / "orchestrator.py"
APP_PY = REPO / "src" / "no_human" / "api" / "app.py"
COMMANDS_PY = REPO / "src" / "no_human" / "cli" / "commands.py"


def _function_body_source(source: str, func_name: str) -> str:
    """Source text of the FIRST `def`/`async def` named *func_name* found
    anywhere in the module — top-level or a class method — bounded by the
    node's own `end_lineno`.

    AST-parsed for both the start and the end (so a comment or string
    mentioning the name cannot be mistaken for the definition, and a nested
    def below it cannot leak past the real end of the body), which is enough
    to prove a symbol is present/absent inside exactly that function.
    """
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    node = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func_name
        ),
        None,
    )
    assert node is not None, f"no def {func_name}(...) found anywhere in the module"
    end = node.end_lineno or len(lines)
    return "".join(lines[node.lineno - 1:end])


def test_is_agent_session_is_real_but_absent_from_approve(security_doc):
    """Positive control first: `is_agent_session` must be a real, live symbol
    somewhere in the tree, or this test would be proving a no-op.

    `agent/guard.py` — where the merge-ban's own spelling check lives — has
    NO occurrence of `is_agent_session` at all; the symbol is defined in
    `core/orchestrator.py` and consumed by websocket/event code in `api/app.py`
    and `cli/commands.py`, never by `approve` or `approve_task`. That is the
    fact the doc must now state plainly instead of pointing a reader at an
    approve-time check that does not run.
    """
    orchestrator_src = ORCHESTRATOR_PY.read_text(encoding="utf-8")
    assert "def is_agent_session" in orchestrator_src, (
        "is_agent_session is no longer defined in core/orchestrator.py — "
        "re-check where the real symbol lives before re-reading this test"
    )
    app_src = APP_PY.read_text(encoding="utf-8")
    commands_src = COMMANDS_PY.read_text(encoding="utf-8")
    assert app_src.count("is_agent_session") >= 1, (
        "is_agent_session is no longer referenced in api/app.py; the positive "
        "control this test relies on to prove the symbol is real has gone stale"
    )
    assert commands_src.count("is_agent_session") >= 1, (
        "is_agent_session is no longer referenced in cli/commands.py; the "
        "positive control this test relies on to prove the symbol is real "
        "has gone stale"
    )

    approve_body = _function_body_source(commands_src, "approve")
    assert "is_agent_session" not in approve_body, (
        "cli/commands.py's approve() now calls is_agent_session — the "
        "approve-time control docs/security.md says is unimplemented may "
        "have been built; if so, update the doc to describe it instead of "
        "denying it"
    )
    approve_task_body = _function_body_source(app_src, "approve_task")
    assert "is_agent_session" not in approve_task_body, (
        "api/app.py's approve_task() now calls is_agent_session — the "
        "approve-time control docs/security.md says is unimplemented may "
        "have been built; if so, update the doc to describe it instead of "
        "denying it"
    )

    assert "is the thing to rely on" not in security_doc, (
        "docs/security.md still tells the reader an approve-time "
        "agent-session check is the thing to rely on — it is not "
        "implemented; the doc must say so instead"
    )


# --- file:line and file:symbol citations must resolve to the code they
# describe ---------------------------------------------------------------
#
# `docs/verification.md` moved its citations to SYMBOLS for exactly the reason
# stated at `test_documented_source_citations_resolve` above: line numbers rot
# on every edit above them. security.md, eval.md and KNOWN_ISSUES.md used to
# be `path:line[-line]`-only, and two same-day incidents made the cost of that
# concrete: task e5eb7b63 burned 4 attempts on its own edits shifting the line
# it had just cited, and a doc-truth PR's `guard.py:2404-2408` citations had
# re-rotted to ~2504-2510 within hours of an unrelated landing. Rewriting the
# convention wholesale would be a style change this fix does not make — so a
# SECOND citation form is accepted alongside the line form: `file.py:Symbol`
# (a class, function, or module-level constant name, optionally followed by
# `:line_start-line_end` as a purely advisory, never-checked hint). Every
# citation actually written in the three docs — either form — is required to
# appear in the table below.

_LINE_CITATION_RE = re.compile(
    r"`((?:[\w./-]+\.(?:py|mjs|cjs))?:\d+(?:-\d+)?)`"
)
#: `file.py:Symbol[.method]` optionally followed by an advisory `:N[-M]` that
#: is parsed but never checked — the whole point is that it may go stale.
#: Reuses `_SYMBOL` (module scope, above) so both citation surfaces recognize
#: the same identifier shape.
_SYMBOL_CITATION_RE = re.compile(
    r"`((?:[\w./-]+\.py)?:" + _SYMBOL + r"(?::\d+(?:-\d+)?)?)`"
)
_LEGACY_LINE_SPEC_RE = re.compile(r"^\d+(?:-\d+)?$")
_REGEX_FALLBACK_DEF_RE = re.compile(r"^(?:async\s+)?(?:def|class)\s+(\w+)\b")
_REGEX_FALLBACK_ASSIGN_RE = re.compile(r"^(\w+)\s*(?::[^=]+)?=")


def _symbol_vicinity_by_regex(lines: list[str], symbol: str) -> list[str] | None:
    """Degraded resolution used only when the source fails to parse as AST.

    Column-0 `def`/`class`/assignment statements only — good enough to
    survive a transient syntax error elsewhere in the file without turning
    every citation in it RED. A nested `Class.method` symbol or a genuinely
    missing name still returns None (the citation still fails) rather than
    guessing at indentation.
    """
    name = symbol.split(".", 1)[0]
    start = None
    for i, line in enumerate(lines):
        match = _REGEX_FALLBACK_DEF_RE.match(line)
        if match and match.group(1) == name:
            start = i
            break
        match = _REGEX_FALLBACK_ASSIGN_RE.match(line)
        if match and match.group(1) == name:
            return [line]
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _REGEX_FALLBACK_DEF_RE.match(lines[j]) or _REGEX_FALLBACK_ASSIGN_RE.match(lines[j]):
            end = j
            break
    return lines[start:end]


def _symbol_vicinity(source_text: str, symbol: str) -> list[str] | None:
    """The lines making up *symbol*'s vicinity in *source_text*.

    For a function or class: its `def`/`class` line through its last line,
    including the docstring and body, EXCLUDING any decorator lines (a
    `@click.option`-decorated command is cited by its body, not its
    decorators). For a module-level constant: the assignment statement.
    `Outer.method` reaches one level into a class body, matching the depth
    `_defined_symbols` (above) already supports for the documented-surface
    citations.

    AST-first. A source file that fails to parse (SyntaxError/ValueError/
    UnicodeDecodeError — e.g. a mid-edit sibling function elsewhere in a hot
    file) falls back to `_symbol_vicinity_by_regex` and emits a UserWarning
    rather than failing every citation into that file; a symbol that is
    genuinely renamed or deleted still resolves to None either way.

    Returns None when *symbol* is not found.
    """
    lines = source_text.splitlines()
    try:
        tree = ast.parse(source_text)
    except (SyntaxError, ValueError, UnicodeDecodeError) as exc:
        warnings.warn(
            f"AST parse failed while resolving citation symbol {symbol!r} "
            f"({exc}); falling back to regex resolution",
            UserWarning,
            stacklevel=2,
        )
        return _symbol_vicinity_by_regex(lines, symbol)

    outer, dot, inner = symbol.partition(".")

    def _search(node, name):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if child.name == name:
                    return child
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        return child
            elif isinstance(child, (ast.If, ast.Try)):
                found = _search(child, name)
                if found is not None:
                    return found
        return None

    node = _search(tree, outer)
    if dot:
        node = _search(node, inner) if isinstance(node, ast.ClassDef) else None

    if node is None:
        return None
    return lines[node.lineno - 1 : node.end_lineno]

#: (doc filename, raw citation text exactly as it appears between backticks,
#: path to resolve it against — inherited from the preceding citation in the
#: same doc for a bare `:NNN` / `:Symbol` continuation, exactly as a reader
#: would read it in prose — expected literal substring in the cited line(s)
#: or, for a `file.py:Symbol` row, anywhere in that symbol's vicinity).
CITATION_TABLE = (
    # docs/security.md
    ("security.md", "guard.py:WRITE_TOOLS", "guard.py", 'WRITE_TOOLS = {"Write"'),
    ("security.md", "agent/claude_backend.py:ClaudeBackend.__init__:519",
     "agent/claude_backend.py", 'permission_mode: str = "bypassPermissions"'),
    ("security.md", ":ClaudeBackend.__init__:544", "agent/claude_backend.py",
     "PreToolUse guard"),
    ("security.md", "vcs/pr_watcher.py:default_pr_state", "vcs/pr_watcher.py",
     '"gh", "pr", "view"'),
    ("security.md", "vcs/git.py:GitRepo._have_remote_commit:836", "vcs/git.py",
     '"git", "fetch"'),
    ("security.md", ":GitRepo.fetch:916", "vcs/git.py", '["fetch", remote]'),
    ("security.md", "cli/commands.py:merge_stack_run:2807", "cli/commands.py",
     '"gh", "pr", "merge"'),
    ("security.md", "cli/commands.py:approve:4955", "cli/commands.py",
     '_refuse_agent_gate_act("approve")'),
    ("security.md", ":merge_stack_run:2777", "cli/commands.py",
     '_refuse_agent_gate_act("merge_stack_run")'),
    ("security.md", "updates.py:44", "updates.py", "PYPI_JSON_URL"),
    ("security.md", "updates.py:57", "updates.py", "DISABLE_ENV_VAR"),
    ("security.md", "desktop/main.mjs:240", "desktop/main.mjs",
     "async function checkForUpdates("),
    ("security.md", "desktop/updater.mjs:113", "desktop/updater.mjs",
     "autoUpdater.checkForUpdates()"),
    ("security.md", "desktop/main.mjs:1098", "desktop/main.mjs", "checkForUpdates()"),
    ("security.md", "desktop/electron-builder.config.cjs:366",
     "desktop/electron-builder.config.cjs", '"github"'),
    ("security.md", "desktop/updater.mjs:66", "desktop/updater.mjs",
     "autoDownload = false"),
    ("security.md", "ci/gitlab.py:403", "ci/gitlab.py", "pipeline"),
    ("security.md", "ci/jenkins.py:301-330", "ci/jenkins.py", "_HTTP_MARKER"),
    ("security.md", "ci/jenkins.py:154-169", "ci/jenkins.py", "buildWithParameters"),
    ("security.md", "ci_gate/enrich.py:70-83", "ci_gate/enrich.py", "_HTTP_MARKER"),
    ("security.md", "ci/circleci.py:169-180", "ci/circleci.py", "_latest_pipeline_for"),
    ("security.md", "ci/circleci.py:182-186", "ci/circleci.py", "_create_pipeline"),
    ("security.md", "context/teams.py:35", "context/teams.py", "GRAPH_SEARCH_URL"),
    ("security.md", ":55-66", "context/teams.py", '"queryString": query'),
    ("security.md", "context/teams.py:50-54", "context/teams.py",
     "M365 Graph token not configured"),
    ("security.md", "notify/slack.py:53", "notify/slack.py",
     "httpx.post(self.webhook_url"),
    ("security.md", "notify/teams.py:205", "notify/teams.py",
     "httpx.post(self.webhook_url"),
    ("security.md", "integrations/__init__.py:test_integration:1591",
     "integrations/__init__.py", "async def test_integration"),
    ("security.md", ":VIEW_ONLY_CHECKS:1588", "integrations/__init__.py",
     "VIEW_ONLY_CHECKS = frozenset"),
    ("security.md", ":_check_github:1462", "integrations/__init__.py",
     "async def _check_github"),
    ("security.md", ":_probe_github_ambient:515", "integrations/__init__.py",
     "_probe_github_ambient"),
    ("security.md", ":_probe_github_ambient:549", "integrations/__init__.py",
     "Only WHETHER a non-empty token exists"),
    ("security.md", "brain/client.py:89-133", "brain/client.py",
     "cfg.control_plane_url"),
    ("security.md", "telemetry.py:_destination", "telemetry.py",
     "posthog_host"),
    ("security.md", "intake/mcp_bridge.py:40", "intake/mcp_bridge.py",
     "127.0.0.1:8420"),
    ("security.md", "cli/commands.py:print_no_task_matching:78", "cli/commands.py",
     "no task matching"),
    ("security.md", "history/extractor.py:65-72", "history/extractor.py",
     "csrf_token"),
    # docs/eval.md
    ("eval.md", "src/no_human/cli/commands.py:bench_run:7123",
     "src/no_human/cli/commands.py", "different --trials are not resumed"),
    ("eval.md", ":bench_run:7446", "src/no_human/cli/commands.py", "asyncio.gather"),
    ("eval.md", ":bench_run:7329", "src/no_human/cli/commands.py",
     "(sc.task_id, sc.trial)"),
    ("eval.md", "src/no_human/eval/northstar_card.py:NorthStarCard.pass_k_rate:443",
     "src/no_human/eval/northstar_card.py", "def pass_k_rate("),
    ("eval.md", "northstar_card.py:success_headline:829-830", "northstar_card.py",
     "pass^{card.trials}"),
    ("eval.md", "northstar_card.py:render_northstar_md:1474-1478", "northstar_card.py",
     "Per-spec reliability"),
    ("eval.md", "tests/test_bench_trials.py:272", "tests/test_bench_trials.py",
     '"pass^1" not in line'),
    ("eval.md", "northstar_card.py:NorthStarCard.spec_mean_success_rate:360",
     "northstar_card.py", "def spec_mean_success_rate("),
    # docs/KNOWN_ISSUES.md
    ("KNOWN_ISSUES.md", "db.py:Store.connect", "db.py", "aiosqlite.connect"),
)

assert len(CITATION_TABLE) >= 20, (
    "CITATION_TABLE has too few rows to be a meaningful floor on citation "
    "coverage across security.md/eval.md/KNOWN_ISSUES.md"
)

#: Rows that cite a path classified `drop` in EXPORT_CLASSIFICATION.txt (here:
#: `ci_gate/` at line 597) — the file is real in this private tree but absent
#: from the public export, so `tests/test_readme_claims.py`, which SHIPS,
#: would otherwise fail on arrival there. These rows are skipped (not passed)
#: when the file does not resolve, and still fully checked — mandatory-hit
#: included — wherever it does; see
#: `test_absent_tolerant_citation_still_checks_content_when_present` for the
#: non-vacuity control that proves the skip cannot mask a wrong citation.
_ABSENT_OK = frozenset({("security.md", "ci_gate/enrich.py:70-83")})

_CITATION_DOC_PATHS = {
    "security.md": SECURITY_DOC,
    "eval.md": EVAL_MD,
    "KNOWN_ISSUES.md": KNOWN_ISSUES_MD,
}


def _citation_source_lines(resolve_path: str, spec: str) -> list[str]:
    """The literal text of the line(s) *spec* names inside *resolve_path*.

    Empty list if the path does not resolve to exactly one file, or the file
    is shorter than the citation claims — both read as "this citation no
    longer points anywhere real" rather than raising.
    """
    hits = _resolve_source(resolve_path)
    if len(hits) != 1:
        return []
    lines = hits[0].read_text(encoding="utf-8").splitlines()
    if "-" in spec:
        start_s, end_s = spec.split("-", 1)
    else:
        start_s = end_s = spec
    start, end = int(start_s), int(end_s)
    if start < 1 or end > len(lines):
        return []
    return lines[start - 1:end]


#: How far a line citation may drift before it must be re-anchored. ±5 lines
#: absorbs the ordinary edit above a citation (an added import, a docstring
#: line, a small helper) that shifts everything below it in a hot file like
#: orchestrator.py or db.py — measured: those shifts turned this suite red for
#: unrelated in-flight tasks, and whole commits (886a575e2) exist only to
#: re-anchor. It stays far smaller than a real relocation: a moved or
#: reworded block leaves the token outside the window and still goes RED, and
#: the token itself must still match EXACTLY as a substring — the window
#: widens WHERE we look, never WHAT counts as a match.
_CITATION_DRIFT_WINDOW = 5


def _locate_line_citation(
    resolve_path: str, spec: str, token: str
) -> tuple[str, int | None, str]:
    """Where *token* actually lives relative to a legacy `path:line[-line]`
    citation, tolerating small drift.

    Returns ``(status, found_line, detail)``:

    - ``"unresolved"`` — *resolve_path* does not resolve to exactly one file.
      *found_line* is None; *detail* is empty. Callers must preserve today's
      assertion text for this case — it is what `_ABSENT_OK` skips on.
    - ``"exact"`` — *token* is on the cited line(s), unchanged. *found_line*
      is the cited start line.
    - ``"drifted"`` — *token* is not on the cited line(s) but IS within
      `_CITATION_DRIFT_WINDOW` lines of them. *found_line* is the 1-based
      line the match now starts on (the occurrence nearest the citation, so
      a token that also appears far away does not win); *detail* is empty.
    - ``"missing"`` — *token* is nowhere in the window. *found_line* is the
      nearest occurrence anywhere else in the file (or None if it appears
      nowhere at all); *detail* names that candidate, or says the content is
      gone, for the diagnostic message.
    """
    hits = _resolve_source(resolve_path)
    if len(hits) != 1:
        return "unresolved", None, ""

    lines = hits[0].read_text(encoding="utf-8").splitlines()
    if "-" in spec:
        start_s, end_s = spec.split("-", 1)
    else:
        start_s = end_s = spec
    start, end = int(start_s), int(end_s)
    span = max(end - start + 1, 1)

    cited = lines[max(start - 1, 0):end] if start >= 1 else []
    if start >= 1 and end <= len(lines) and token in "\n".join(cited):
        return "exact", start, ""

    lo = max(0, start - 1 - _CITATION_DRIFT_WINDOW)
    hi = min(len(lines), end + _CITATION_DRIFT_WINDOW)
    window_starts = sorted(
        range(lo, max(hi - span + 1, lo)),
        key=lambda i: abs(i - (start - 1)),
    )
    for i in window_starts:
        candidate = "\n".join(lines[i:i + span])
        if token in candidate:
            return "drifted", i + 1, ""

    # Missing: search the whole file for a diagnostic, but never treat a
    # distant match as found — that would be exactly the widened match rule
    # this helper must not implement.
    for i in range(0, max(len(lines) - span + 1, 0)):
        candidate = "\n".join(lines[i:i + span])
        if token in candidate:
            return "missing", i + 1, f"nearest candidate is line {i + 1}"
    return "missing", None, f"not found anywhere in {hits[0]}"


def _check_citation(
    doc: str,
    raw: str,
    resolve_path: str,
    token: str,
    *,
    source_text: str | None = None,
) -> None:
    """Shared assertion body for a single citation row — factored out so the
    non-vacuity control below can drive it directly with a wrong token.

    Dispatches on the shape of the spec after the (possibly inherited) path:
    a bare line number or range (`58`, `507-533`) resolves against the real
    file on disk, unchanged from before symbol citations existed. Anything
    else is a symbol (optionally followed by an advisory `:line-line` that is
    parsed and then ignored — the whole point of a symbol citation is to
    survive that range going stale).

    *source_text* lets a test inject file content directly instead of
    reading the resolved path from disk (used by the refactor-resilience and
    AST-fallback tests below); it only applies to the symbol path — a legacy
    line citation always reads the real file, since there is nothing to
    fake a stale line range against.
    """
    tail = raw.split(":", 1)[1]
    if _LEGACY_LINE_SPEC_RE.match(tail):
        status, found_line, detail = _locate_line_citation(resolve_path, tail, token)
        if status == "unresolved":
            assert False, (
                f"{doc} cites `{raw}` (resolved against {resolve_path!r}) but that "
                f"does not resolve to a real line range — the code moved or the "
                f"citation was never re-derived"
            )
        if status == "exact":
            return
        if status == "drifted":
            warnings.warn(
                f"{doc} cites `{raw}` for {token!r}, which has drifted from "
                f"line {tail} to line {found_line} in {resolve_path} — still "
                f"passing on a ±{_CITATION_DRIFT_WINDOW}-line tolerance; run "
                f"`uv run python scripts/reanchor_citations.py --apply` to "
                f"re-anchor it",
                UserWarning,
                stacklevel=2,
            )
            return
        # status == "missing"
        lines = _citation_source_lines(resolve_path, tail)
        haystack = "\n".join(lines) if lines else "(citation is out of range)"
        assert False, (
            f"{doc} cites `{raw}` for {token!r}, but the line(s) now read:\n"
            f"  {haystack!r}\n"
            f"and {token!r} was not found within "
            f"±{_CITATION_DRIFT_WINDOW} lines of the citation either — "
            f"{detail}; re-derive the citation from the current tree, or run "
            f"`uv run python scripts/reanchor_citations.py --apply` if the "
            f"nearest candidate above is the right target"
        )

    # Symbol citation: strip the optional advisory `:line[-line]` suffix —
    # it is never consulted for pass/fail, only parsed so the format allows it.
    symbol = tail.split(":", 1)[0]
    if source_text is not None:
        text = source_text
        display_path = resolve_path
    else:
        hits = _resolve_source(resolve_path)
        assert len(hits) == 1, (
            f"{doc} cites `{raw}` (resolved against {resolve_path!r}) but that "
            f"resolves to {len(hits)} files, not one"
        )
        display_path = hits[0]
        text = hits[0].read_text(encoding="utf-8")

    vicinity = _symbol_vicinity(text, symbol)
    assert vicinity is not None, (
        f"{doc} cites `{raw}` but {symbol!r} is not defined in {display_path} "
        f"— renamed or deleted, and the doc still sends readers to it"
    )
    haystack = "\n".join(vicinity)
    assert token in haystack, (
        f"{doc} cites `{raw}` for {token!r}, but {symbol}'s body now reads:\n"
        f"  {haystack!r}\n"
        f"re-derive the citation from the current tree"
    )


@pytest.mark.parametrize(
    "doc,raw,resolve_path,token",
    CITATION_TABLE,
    ids=[f"{doc}:{raw}" for doc, raw, _, _ in CITATION_TABLE],
)
def test_doc_citations_resolve_to_the_code_they_describe(doc, raw, resolve_path, token):
    """Every `path:line[-line]` citation in security.md/eval.md/KNOWN_ISSUES.md
    must point at code that still says what the doc cites it for.

    This is the guard the docs-vs-code audit found missing: several of these
    citations had rotted silently because nothing checked them. A citation
    that no longer resolves, or resolves to a line with none of the expected
    content, means the code moved and the doc did not follow.

    Exception: a row in `_ABSENT_OK` cites a drop-classified path (see the
    comment on `_ABSENT_OK`) — if it doesn't resolve at all, that's the
    export tree behaving as designed, so this skips instead of failing.
    If it DOES resolve, the check below still runs in full, mandatory hit
    included — the file must exist in this dev tree, and non-vacuity is
    covered by `test_absent_tolerant_citation_still_checks_content_when_present`.
    """
    if (doc, raw) in _ABSENT_OK and not _resolve_source(resolve_path):
        pytest.skip(
            f"{resolve_path} is drop-classified (EXPORT_CLASSIFICATION.txt) "
            f"and absent from this tree — expected in the public export; "
            f"the citation is fully checked wherever the file is present"
        )
    _check_citation(doc, raw, resolve_path, token)


def test_absent_tolerant_citation_still_checks_content_when_present():
    """Non-vacuity control for `_ABSENT_OK`: the skip above must trigger only
    on a genuinely absent file, never on a present-but-wrong citation.

    `ci_gate/enrich.py` is drop-classified but real in this (private) dev
    tree, so feeding its row a token that is not on the cited lines must
    still raise — if it silently passed or skipped instead, the
    absent-tolerant row would be able to mask a stale citation forever.

    This module SHIPS, so this test runs in the public export too, where
    `ci_gate/enrich.py` is absent by design (same as the row it controls
    for) — it skips there rather than failing, for the same reason.
    """
    doc, raw, resolve_path, _token = next(
        row for row in CITATION_TABLE if (row[0], row[1]) in _ABSENT_OK
    )
    if not _resolve_source(resolve_path):
        pytest.skip(
            f"{resolve_path} is drop-classified and absent from this tree — "
            f"this control only exercises the present-file branch, and can "
            f"only run wherever the file is present"
        )
    with pytest.raises(AssertionError):
        _check_citation(doc, raw, resolve_path, "no-such-token-in-this-file")


def test_the_citation_table_covers_every_line_citation_in_the_three_docs():
    """Every backticked `path:line[-line]` OR `path:Symbol[:line[-line]]`
    citation actually written in security.md/eval.md/KNOWN_ISSUES.md must
    have a row in CITATION_TABLE — otherwise this guard only ever checks the
    citations someone remembered to add, which is exactly the blind spot
    that let the originals rot. Legacy line-only citations remain legal —
    migrating to a symbol anchor is encouraged for rot-prone hot files, not
    required for every row.
    """
    table_by_doc: dict[str, set[str]] = {}
    for doc, raw, _, _ in CITATION_TABLE:
        table_by_doc.setdefault(doc, set()).add(raw)

    missing: list[str] = []
    extra: list[str] = []
    for doc, path in _CITATION_DOC_PATHS.items():
        text = path.read_text(encoding="utf-8")
        found = set(_LINE_CITATION_RE.findall(text)) | set(
            _SYMBOL_CITATION_RE.findall(text)
        )
        table = table_by_doc.get(doc, set())
        missing.extend(f"{doc}: {raw}" for raw in sorted(found - table))
        extra.extend(f"{doc}: {raw}" for raw in sorted(table - found))

    assert not missing, (
        "citations written in the docs are not covered by CITATION_TABLE:\n  "
        + "\n  ".join(missing)
    )
    assert not extra, (
        "CITATION_TABLE has rows for citations no longer present in the "
        "docs (stale table entries — the doc changed and the table did not):"
        "\n  " + "\n  ".join(extra)
    )


def test_symbol_citation_resilience():
    """The entire point of a `file.py:Symbol` citation: it must keep
    resolving to the same code after the cited file grows above it.

    Prepending 100 arbitrary lines to each migrated citation's real source
    shifts every historical line number in the file by exactly 100 without
    touching any symbol. A citation that (despite appearances) secretly
    depended on the line number it was first written against — or on a now
    very-stale advisory `:line-line` suffix — would go RED here; one that
    resolves the symbol by name, as designed, does not notice the padding.
    """
    pad_text = "\n".join(f"# padding line {i}" for i in range(100)) + "\n"
    checked = 0
    for doc, raw, resolve_path, token in CITATION_TABLE:
        tail = raw.split(":", 1)[1]
        if _LEGACY_LINE_SPEC_RE.match(tail):
            continue  # legacy line citation — not part of this migration
        hits = _resolve_source(resolve_path)
        assert len(hits) == 1, (
            f"{resolve_path} (from {doc} citation `{raw}`) does not resolve "
            f"to exactly one file in this tree"
        )
        padded_source = pad_text + hits[0].read_text(encoding="utf-8")
        _check_citation(doc, raw, resolve_path, token, source_text=padded_source)
        checked += 1
    assert checked >= 15, (
        f"only {checked} symbol citations were exercised by the resilience "
        f"check — the ~20-row migration this test guards should cover most "
        f"of CITATION_TABLE's symbol-anchored rows"
    )


def test_symbol_citation_fails_on_wrong_symbol():
    """A symbol citation must fail exactly as loudly as a line citation does
    when the code under it has moved — a renamed/deleted symbol, or a token
    no longer present in an otherwise-correctly-resolved symbol's vicinity.
    """
    source_text = (
        "class Widget:\n"
        "    def spin(self):\n"
        "        return 'whee'\n"
    )
    with pytest.raises(AssertionError, match="is not defined in"):
        _check_citation(
            "security.md",
            "fake.py:NoSuchSymbol",
            "fake.py",
            "whee",
            source_text=source_text,
        )
    with pytest.raises(AssertionError, match="re-derive the citation"):
        _check_citation(
            "security.md",
            "fake.py:Widget.spin",
            "fake.py",
            "not-anywhere-in-spin",
            source_text=source_text,
        )


def test_symbol_citation_falls_back_to_regex_when_ast_fails():
    """A syntax error anywhere in a hot file must not turn every citation
    into that file RED — a well-formed symbol elsewhere in the same file
    should still resolve via the regex fallback, with a UserWarning that
    makes the degraded path visible instead of silently swallowing it.
    """
    source_text = (
        "def healthy_symbol():\n"
        "    return 'still here'\n"
        "\n"
        "def broken(:\n"
        "    pass\n"
    )
    with pytest.warns(UserWarning, match="falling back to regex"):
        vicinity = _symbol_vicinity(source_text, "healthy_symbol")
    assert vicinity is not None, "regex fallback failed to resolve a top-level def"
    assert "still here" in "\n".join(vicinity)

    with pytest.warns(UserWarning, match="falling back to regex"):
        _check_citation(
            "security.md",
            "fake.py:healthy_symbol",
            "fake.py",
            "still here",
            source_text=source_text,
        )


def test_line_citation_tolerates_small_drift(tmp_path, monkeypatch):
    """The defect this guard exists for: an unrelated edit prepends a few
    lines above a citation's target in a hot file, and the cited line number
    rots — but the CONTENT is still right there, a few lines down.

    `_check_citation` must still pass (with a UserWarning naming the drift,
    not silently), and `_locate_line_citation` must report exactly where the
    content moved to.
    """
    original = "\n".join(f"line {i}" for i in range(1, 11)) + "\n"
    target = tmp_path / "widget.py"
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_resolve_source", lambda path: [target])

    status, found_line, _detail = _locate_line_citation("widget.py", "5", "line 5")
    assert status == "exact" and found_line == 5

    # Prepend 3 lines — everything below shifts down by 3, exactly the shape
    # of the drift this window absorbs.
    target.write_text("pad 1\npad 2\npad 3\n" + original, encoding="utf-8")

    status, found_line, _detail = _locate_line_citation("widget.py", "5", "line 5")
    assert status == "drifted", f"expected drifted, got {status!r}"
    assert found_line == 8, f"expected the content at its new line 8, got {found_line}"

    with pytest.warns(UserWarning, match="drifted"):
        _check_citation("security.md", "widget.py:5", "widget.py", "line 5")


def test_line_citation_fails_when_content_is_gone(tmp_path, monkeypatch):
    """A citation must still go RED when its content is genuinely gone —
    deleted, reworded, or moved far enough that drift tolerance is not the
    honest answer. Both cases must name a nearest-candidate diagnostic
    (never silently guess, never pass).
    """
    original = "\n".join(f"line {i}" for i in range(1, 11)) + "\n"
    target = tmp_path / "widget.py"
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_resolve_source", lambda path: [target])

    # Case 1: the token is deleted/reworded entirely — no candidate anywhere.
    target.write_text(
        "\n".join(f"line {i}" for i in range(1, 5))
        + "\nsomething else entirely\n"
        + "\n".join(f"line {i}" for i in range(6, 11))
        + "\n",
        encoding="utf-8",
    )
    status, found_line, detail = _locate_line_citation("widget.py", "5", "line 5")
    assert status == "missing"
    assert found_line is None
    assert "not found anywhere" in detail
    with pytest.raises(AssertionError, match="not found anywhere"):
        _check_citation("security.md", "widget.py:5", "widget.py", "line 5")

    # Case 2: the token moved 20 lines away — well outside the ±5 window —
    # still fails, but the diagnostic names where it actually is.
    padded = "\n".join(f"pad {i}" for i in range(1, 21)) + "\n" + original
    target.write_text(padded, encoding="utf-8")
    status, found_line, detail = _locate_line_citation("widget.py", "5", "line 5")
    assert status == "missing"
    assert found_line == 25, f"expected the real (out-of-window) line, got {found_line}"
    assert "nearest candidate is line 25" in detail
    with pytest.raises(AssertionError, match="nearest candidate is line 25"):
        _check_citation("security.md", "widget.py:5", "widget.py", "line 5")


def test_drift_window_is_not_a_blanket_pass():
    """The window is a small, deliberate tolerance, not a fuzzy-match
    escape hatch — this pins both the bound itself and the fact that content
    genuinely outside it is `"missing"`, not `"drifted"`.
    """
    assert _CITATION_DRIFT_WINDOW <= 10, (
        "the drift window grew past a small tolerance for ordinary edits — "
        "that starts to hide real relocations instead of catching them"
    )


def test_every_line_citation_currently_resolves_exactly():
    """The shipped docs are exactly anchored today, not merely within drift
    tolerance — this is what gives `scripts/reanchor_citations.py --check`
    something to enforce, and proves the new tolerance did not quietly
    downgrade every legacy citation to "drifted".
    """
    checked = 0
    for doc, raw, resolve_path, token in CITATION_TABLE:
        tail = raw.split(":", 1)[1]
        if not _LEGACY_LINE_SPEC_RE.match(tail):
            continue  # symbol citation — not part of this guard
        if (doc, raw) in _ABSENT_OK and not _resolve_source(resolve_path):
            continue  # export-absent row; covered by its own non-vacuity test
        status, found_line, detail = _locate_line_citation(resolve_path, tail, token)
        assert status == "exact", (
            f"{doc} citation `{raw}` is {status!r} (found_line={found_line}, "
            f"{detail}), not exactly anchored — the shipped docs should not "
            f"be relying on drift tolerance"
        )
        checked += 1
    assert checked >= 15, (
        f"only {checked} legacy line citations were exercised — the ~22-row "
        f"legacy-form slice of CITATION_TABLE should cover most of them"
    )


# --- KNOWN_ISSUES.md's traceback citations (not backtick-wrapped) ------------


def test_known_issues_traceback_cites_the_functions_it_names(known_issues_doc):
    """The plain-text traceback in KNOWN_ISSUES.md names `db.py:2296` inside
    `update_attempt` and `orchestrator.py:4683` inside `_run_attempt` — not
    backtick-wrapped, so the generic citation table above cannot see them.
    Checked directly against the AST so a refactor that moves either call is
    caught rather than silently believed.

    Re-anchored 2026-09-01: P5's `list_tasks(limit=, offset=)` pagination and
    its review-round-1 `rowid DESC` tie-break together added 18 lines above
    this call inside db.py (most recently to 2103 on the P5 merge); the citation is re-verified
    against the code, not carried forward blind.

    Re-anchored again 2026-09-01: the local-backend infra classifier
    (`_park_local_infra`, the coder call site, and the `_park_quota` guard)
    added lines above this call inside orchestrator.py, moving it from 4559
    to 4566; re-verified against the code, not carried forward blind.

    Re-anchored again 2026-09-03 (second): `record_cancel_reason` and its
    callers added 47 lines above `update_attempt`'s commit in db.py, moving
    the citation from 2188 to 2235; re-verified against the code, not
    carried forward blind.

    Re-anchored again 2026-09-03: the reviewer role-backend disclosure work
    added 24 lines above `_run_attempt`'s update_attempt call in
    orchestrator.py, moving the citation from 4599 to 4623; re-verified
    against the code, not carried forward blind.

    Re-anchored again 2026-09-02 (second): `reconcile_landed_terminal`
    (the terminal failed/cancelled landed-evidence pass) added 85 lines above
    `update_attempt`'s commit in db.py, moving the citation from 2103 to
    2188; re-verified against the code, not carried forward blind.

    Re-anchored again 2026-09-02: the profile-divergence advisory's
    `_profile_divergence_warned` latch, added in `Orchestrator.__init__`,
    pushed every later line in the file down by 5, moving this citation from
    4566 to 4571; re-verified against the code, not carried forward blind.

    Re-anchored again 2026-09-02 (rebase): the declared-repro-files-committed
    preflight (`repro_send_back_message`'s helper text, `_declared_files_
    preflight`, `_DECLARED_FILES_ROUND_TURNS`, `declared_files_send_back_
    message`, and the `_repro_gate_step`/`_repro_corrective_round` wiring)
    added 28 lines above this call inside orchestrator.py during the rebase
    onto origin/main, moving it from 4571 to 4599; re-verified against the
    code, not carried forward blind.

    Re-anchored again 2026-09-03 (second): the reviewer role-backend
    disclosure-rendering slice (`_emit_models` dropping the appended
    `detail` suffix in favour of the `role_backends` kwarg alone, plus its
    updated docstring) added 2 net lines above `_run_attempt`'s
    `update_attempt(attempt_id, branch_name=branch)` call inside
    orchestrator.py, moving the citation from 4623 to 4625; re-verified
    against the code, not carried forward blind.

    Re-anchored again 2026-09-03 (third): the approval-supersede write
    site — `_write_status`'s CASE-clause stamp of
    `context.approval_superseded_at` (all three CAS branches), the
    in-process mirror, and the docstring explaining the contract — added
    61 lines above `update_attempt`'s commit in db.py, moving the citation
    from 2235 to 2296; re-verified against the code, not carried forward
    blind. `_run_attempt`'s call site in orchestrator.py is untouched by
    this change and stays at 4625.

    Re-anchored again 2026-09-04: the dispatch-time intake-eval hoisted
    path (the `elif ctx.get("eval_result")` branch that acts on a
    grill/wizard-stored verdict, plus its cost/residual-gap comments)
    added 18 lines above `_run_attempt`'s `update_attempt` call inside
    `_drive`, earlier in orchestrator.py, moving the citation from 4625
    to 4643; re-verified against the code, not carried forward blind.

    Re-anchored again 2026-09-03 (fourth): the structural-budget preflight
    (`structural_budget_send_back_message`, `_structural_budget_preflight`,
    and its call site between the repro gate and the draft-PR open — one
    bounded corrective round when a diff grows a frozen
    `tests/test_structural_budget.py` entry, so the re-anchor lands before
    review instead of costing a whole extra attempt) added 34 lines above

Re-anchored again 2026-09-03 (fourth): the WIP-checkpoint resume-digest
    sentence (`build_resume_digest`'s `base` kwarg, kept) was tried together
    with a one-turn already-satisfied correction (`_wip_claim_correction`,
    `_WIP_SUBJECT_REASON`, `_WIP_CLAIM_CORRECTION`) that briefly moved this
    citation to 4648; the correction turn was WITHDRAWN on independent
    review (task bf645f3a: coder sessions never resume across attempts, so a
    same-session correction turn cannot fix a cross-attempt mistake, and its
    abort-exception path had no handler at its call site) and removed along
    with its constants and test registration — see
    `tests/test_already_satisfied_wip_correction.py`. `_run_attempt`'s call
    site in orchestrator.py is back at 4625, its original line; the `base`
    threading and the `attempt_n` handoff write that stayed neither added
    nor removed lines above this call. `update_attempt`'s call site in
    db.py is untouched by this change and stays at 2296.

    Re-home merge 2026-09-04 (279c03c5): the WIP resume-digest change and the intake-eval/preflight chain now live on one tree; the call measures at 4683 here — re-verified against the code, not carried forward blind.
    `_run_attempt`'s `update_attempt(attempt_id, branch_name=branch)` call
    in orchestrator.py, moving the citation from 4625 to 4659; re-verified
    against the code, not carried forward blind. db.py:2296 is untouched by
    this change.

    Re-anchored again 2026-09-04 (fifth): the follow-up widening of the
    structural-budget preflight (`scanned_root`/`touches_scanned_root`
    alongside `frozen_paths`/`touched_frozen` so a brand-new offender or a
    stale frozen entry also buys a corrective round, plus the generalized
    `structural_budget_send_back_message` naming whichever paths triggered
    it) added 22 lines above `_run_attempt`'s
    `update_attempt(attempt_id, branch_name=branch)` call in
    orchestrator.py, moving the citation from 4659 to 4681; re-verified
    against the code, not carried forward blind. db.py:2296 is untouched by
    this change.

    Re-anchored again 2026-09-04 (re-home merge): both the intake-eval
    hoisted path (+18) and the structural-budget preflight chain (+34, +22)
    now live on one tree; the call measures at 4683 here — re-verified
    against the code, not carried forward blind.

    Re-anchored again 2026-09-04 (second): the lifetime-cap follow-ups
    (`latest_review_attempt`/`latest_failed_attempt`, the `_attempt_recency`
    helper beside `_MECHANICAL_FEEDBACK_SOURCES`, `_mechanical_round`'s third
    conjunct, and the `_budget_exhausted_blocker` last-failure sentence)
    added 29 net lines above `_run_attempt`'s `update_attempt(attempt_id,
    branch_name=branch)` call in orchestrator.py, moving the citation from
    4683 to 4712; re-verified against the code, not carried forward blind.
    db.py:2296 sits above the new `latest_review_attempt`/
    `latest_failed_attempt` helpers (added near :2469) and is untouched by
    this change.

    Re-anchored again 2026-09-04 (attribution guard, rebased onto the
    lifetime-cap follow-ups above): the base-sha pin hardening added three
    helpers ahead of `_run_attempt` in the file (`_base_exclusion_refs`, the
    `base_pin`-aware paragraph on `_foreign_authored_commits`, and the
    `ls_remote_exact` pin capture plus its fail-closed advisory branch
    inside `_run_attempt` itself) — 89 net lines above the 4683 baseline on
    its own — which combined with the lifetime-cap follow-ups' 29 lines
    moves `self.store.update_attempt(attempt_id, branch_name=branch)` to
    4801 in orchestrator.py. The same change added `base_pin_sha` to
    `Store._ensure_task_columns`'s additive-column dict in db.py, ahead of
    `update_attempt`, moving its `await self.db.commit()` from 2296 to 2306
    (the lifetime-cap helpers sit above this call and do not shift it
    further). Both re-verified against the code, not carried forward blind.

    Re-anchored again 2026-09-04 (sixth): the task_failed telemetry
    reason_category wiring (_fail's new keyword-only param, the
    _telemetry_hook resolution, and the tagged self._fail(...) call sites)
    added 6 net lines above _run_attempt's update_attempt(attempt_id,
    branch_name=branch) call in orchestrator.py, moving the citation from
    4801 to 4807; re-verified against the code, not carried forward blind.
    db.py:2306 is untouched by this change.
    """
    assert "db.py:2306" in known_issues_doc, (
        "the traceback no longer cites db.py:2306 — this test is pointed at "
        "stale text; re-derive from the current traceback"
    )
    assert "orchestrator.py:4807" in known_issues_doc, (
        "the traceback no longer cites orchestrator.py:4807 — this test is "
        "pointed at stale text; re-derive from the current traceback"
    )

    db_src = (REPO / "src" / "no_human" / "core" / "db.py").read_text(encoding="utf-8")
    db_body = _function_body_source(db_src, "update_attempt")
    db_lines = db_src.splitlines()
    assert 1 <= 2306 <= len(db_lines), "db.py is now shorter than line 2306"
    assert db_lines[2305].strip() == "await self.db.commit()", (
        f"db.py:2306 is now {db_lines[2305]!r}, not the commit the traceback "
        f"names"
    )
    assert "await self.db.commit()" in db_body, (
        "line 2306 is no longer inside update_attempt's body"
    )

    orch_src = ORCHESTRATOR_PY.read_text(encoding="utf-8")
    orch_body = _function_body_source(orch_src, "_run_attempt")
    orch_lines = orch_src.splitlines()
    assert 1 <= 4807 <= len(orch_lines), "orchestrator.py is now shorter than line 4807"
    assert "self.store.update_attempt(" in orch_lines[4806], (
        f"orchestrator.py:4807 is now {orch_lines[4806]!r}, not the "
        f"update_attempt call the traceback names"
    )
    assert "self.store.update_attempt(" in orch_body, (
        "line 4807 is no longer inside _run_attempt's body"
    )


# --- eval.md: judge model, red-team count, and the live CI file --------------

GOLDEN_TASKS_DIR = REPO / "eval" / "golden_tasks"


def _red_team_task_ids() -> set[str]:
    """Every golden task id where `impossible` or `tempts_tamper` is true."""
    ids: set[str] = set()
    for path in sorted(GOLDEN_TASKS_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if data.get("impossible") or data.get("tempts_tamper"):
            ids.add(data["id"])
    return ids


def test_eval_md_red_team_count_matches_the_golden_tasks(eval_doc):
    """Derived from the golden task corpus, not hand-counted: a red-team task
    added or retired changes this count automatically, and the doc's number
    and named ids must track it.
    """
    ids = _red_team_task_ids()
    assert len(ids) >= 5, (
        f"only {len(ids)} red-team golden tasks found under "
        f"{GOLDEN_TASKS_DIR.relative_to(REPO)} — this floor exists so the "
        f"guard cannot pass by finding nothing"
    )
    number_words = {
        1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
        6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
    }
    word = number_words.get(len(ids))
    assert word, f"no number-word mapping for {len(ids)} red-team tasks"
    assert word in eval_doc.lower(), (
        f"eval.md does not say {word!r} ({len(ids)}) red-team tasks; it must "
        f"match the golden task corpus exactly"
    )
    missing_ids = [i for i in sorted(ids) if i not in eval_doc]
    assert not missing_ids, (
        f"eval.md's red-team paragraph omits {missing_ids} — every red-team "
        f"task id must be named so a reader can find it in the corpus"
    )


def test_eval_md_names_the_judge_model_the_code_defaults_to(eval_doc):
    """The Intent-match judge's default model must match what `IntentJudge`
    actually constructs with, via `inspect.signature` — not a hand-copied
    string that can drift the day the default changes.
    """
    from no_human.eval.judge import IntentJudge

    default_model = inspect.signature(IntentJudge.__init__).parameters["model"].default
    assert default_model in eval_doc, (
        f"eval.md does not name IntentJudge's actual default model "
        f"({default_model!r}) as the judge model"
    )
    assert "claude-sonnet-4-6" not in eval_doc, (
        "eval.md still names the retired judge model claude-sonnet-4-6"
    )


def test_eval_md_names_the_live_ci_workflow(eval_doc):
    """CI runs the offline eval harness from `.github/workflows/ci.yml`, not
    `.gitlab-ci.yml` — the doc must point a reader at the workflow that
    actually runs, and that file must exist.
    """
    assert (REPO / ".github" / "workflows" / "ci.yml").exists(), (
        "docs/eval.md is being asked to name .github/workflows/ci.yml, but "
        "that file does not exist in this tree"
    )
    assert ".github/workflows/ci.yml" in eval_doc, (
        "eval.md does not name .github/workflows/ci.yml as where the offline "
        "harness runs in CI"
    )
    assert ".gitlab-ci.yml" not in eval_doc, (
        "eval.md still points at .gitlab-ci.yml, which is not the live CI file"
    )


# --- KNOWN_ISSUES.md: the real `nh stop --timeout` default -------------------


def test_known_issues_stop_timeout_default_matches_the_cli(known_issues_doc):
    """`nh stop --timeout` defaults to `stop_grace_s(config)` (60s) plus
    `STOP_COMMAND_MARGIN_S` (15s) = 75s. Computed from the real constants
    rather than hand-copied, so a change to either constant moves this
    assertion instead of silently leaving the doc behind again.
    """
    from no_human.core.scheduler import STOP_COMMAND_MARGIN_S, stop_grace_s

    computed = stop_grace_s(DEFAULT_CONFIG) + STOP_COMMAND_MARGIN_S
    assert computed == 75, (
        f"stop_grace_s(DEFAULT_CONFIG) + STOP_COMMAND_MARGIN_S is now "
        f"{computed}, not 75 — re-derive KNOWN_ISSUES.md's stated default "
        f"from these constants before trusting the assertion below"
    )
    assert "75s" in known_issues_doc, (
        "KNOWN_ISSUES.md does not state the real nh stop --timeout default "
        "(75s = stop_grace_s + STOP_COMMAND_MARGIN_S)"
    )
    assert "defaulted to 3s" in known_issues_doc, (
        "KNOWN_ISSUES.md dropped the historical note that the old default "
        "(3s) was the actual bug"
    )
    assert "30s" not in known_issues_doc, (
        "KNOWN_ISSUES.md still states a 30s nh stop --timeout default, which "
        "is not what the CLI computes"
    )


# --- verification.md: four gates, and the published recall figure -----------


def test_verification_md_counts_four_gates(verification_doc):
    """The pipeline runs FOUR gates (reviewer, verifiers, tamper guard, repro
    gate) — not three. "Deterministic lint evidence" is explicitly an INPUT to
    the gates, not a gate itself, and must stay excluded from the count.
    """
    assert "Three gates" not in verification_doc, (
        "verification.md still opens with 'Three gates' — the pipeline runs "
        "four (reviewer, verifiers, tamper guard, repro gate)"
    )
    assert "Four gates" in verification_doc, (
        "verification.md does not state the pipeline runs four gates"
    )
    gate_headings = [
        "An adversarial reviewer that is not the author",
        "Verifiers — a recorded verdict per rule",
        "A tamper guard against a self-gutted test suite",
        "A reproduction gate that proves the fix fixed the bug",
    ]
    missing = [h for h in gate_headings if f"## {h}" not in verification_doc]
    assert not missing, (
        f"verification.md is missing gate heading(s) {missing} — the "
        f"'four gates' count must be backed by four actual sections"
    )
    assert "## Deterministic lint evidence — not a gate, an input" in verification_doc, (
        "verification.md's lint-evidence heading no longer explicitly "
        "disclaims being a gate — without that disclaimer a reader could "
        "recount it as a fifth gate against the stated total of four"
    )


REVIEWER_RECALL_METHOD_MD = REPO / "docs" / "REVIEWER_RECALL_METHOD.md"


def test_verification_md_publishes_the_recall_figure(verification_doc):
    """verification.md must cite the actual published reviewer-recall run
    (2026-08-11, claude-opus-4-8, 15/19 recall, 7/10 specificity) rather than
    claiming no number is published — and that run must really be the one
    REVIEWER_RECALL_METHOD.md documents, not a number invented for this doc.
    """
    assert REVIEWER_RECALL_METHOD_MD.exists(), (
        "docs/REVIEWER_RECALL_METHOD.md does not exist; verification.md is "
        "being asked to cite a method doc that is not in the tree"
    )
    method_doc = REVIEWER_RECALL_METHOD_MD.read_text(encoding="utf-8")

    required_tokens = ("15/19", "7/10", "2026-08-11", "claude-opus-4-8")
    for token in required_tokens:
        assert token in verification_doc, (
            f"verification.md does not cite {token!r} from the published "
            f"reviewer-recall run"
        )
        assert token in method_doc, (
            f"{token!r} is asserted in verification.md but is not actually "
            f"in REVIEWER_RECALL_METHOD.md — the citation would not be "
            f"re-derivable from its own source"
        )
    assert "REVIEWER_RECALL_METHOD.md" in verification_doc, (
        "verification.md does not link/name REVIEWER_RECALL_METHOD.md as "
        "where the recall figure's method lives"
    )
    assert "No number is published" not in verification_doc, (
        "verification.md still claims no recall number is published, but "
        "one now is"
    )


_BOUNDS_SECTION = ("## When it cannot finish", "\n## ")
_BOUNDS_DOC_LOCATION = 'docs/verification.md:175-176 ("When it cannot finish")'


def _bounds_paragraph(verification_doc: str) -> str:
    """Return the bounded-loop paragraph, or "" if its heading was removed."""
    return _slice_between(verification_doc, *_BOUNDS_SECTION)


def test_verification_md_bounds_numbers_match_config(verification_doc):
    """Pin the paragraph's three bounded-loop defaults to the actual config.

    ``max_turns_per_attempt`` is also covered by
    ``test_prose_default_matches_config``, but that guard scans every documented
    surface. This one is deliberately narrower: it pins this one paragraph and
    is the only guard for ``max_attempts`` and ``lifetime_attempts``.
    """
    paragraph = _bounds_paragraph(verification_doc)
    assert paragraph, (
        f"{_BOUNDS_DOC_LOCATION} - bounds section is missing; this guard "
        "must not pass vacuously"
    )
    normalized = re.sub(r"\s+", " ", paragraph)

    for key in ("max_attempts", "max_turns_per_attempt", "lifetime_attempts"):
        actual = DEFAULT_CONFIG["bounds"][key]
        matches = re.findall(
            rf"`?bounds\.{key}`?[^.]*?(?:\(defaults?\s*(?:to)?\s*:?\s*(\d+)\)|"
            rf"\bis\s+(\d+)\b|\bdefaults?\s+to\s+(\d+)\b)",
            normalized,
        )
        stated = [int(next(value for value in match if value)) for match in matches]
        assert stated, (
            f"{_BOUNDS_DOC_LOCATION} - bounds.{key}: no stated default found; "
            "the claim was dropped (this guard was about to pass vacuously) or "
            "reworded past this pattern — widen the pattern"
        )
        for found in stated:
            assert found == actual, (
                f"{_BOUNDS_DOC_LOCATION} - bounds.{key}: expected {actual} "
                f'(DEFAULT_CONFIG["bounds"]["{key}"], src/no_human/config.py), '
                f'found {found} in the "When it cannot finish" paragraph'
            )
