"""`docs/INTEGRATIONS_LEGAL.md`'s `## Codex` section is a recorded legal/
compliance position, not prose anyone is free to soften on the next edit: the
three OpenAI quotes, the source URLs, the fetch date, the unfavourable
CI/CD half, discussion #8338's partial answer, the withdrawn prohibition, the
exact auth modes this codebase supports, and the "not a finding of law" close
are each load-bearing facts that silently rot the same way a stale README
claim does (see `tests/test_readme_claims.py`'s docstring for the class of
defect this guards against).

This file is RED-first: written and run against a tree with no
`docs/INTEGRATIONS_LEGAL.md` at all, so the first test's failure is the
recorded proof the doc did not already satisfy the acceptance criteria. Only
`test_integrations_legal_doc_exists_with_codex_section` needs to fail RED —
every other test here needs the doc to exist merely to run, so under RED they
error rather than assert; both are read as "not yet green" by the harness
that captured this file's first run.

Every quote and fact pinned below is copied out of `src/no_human/agent/
codex_backend.py`'s module docstring and `src/no_human/config.py`'s
`codex_auth_mode` block, not re-derived — this guard checks that the doc
still says what the code already recorded, not that either one is correct.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from no_human.config import CODEX_AUTH_MODES

# Selected by `.no_human.yml`'s web/desktop routes as `-m repoguard`, alongside
# whichever other repo-wide guards the checkout carries.
pytestmark = pytest.mark.repoguard

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "INTEGRATIONS_LEGAL.md"

_CODEX_HEADING = re.compile(r"^## Codex\b", re.MULTILINE)
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NON_ISO_DATE_RE = re.compile(
    r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"
    r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b"
)


def _doc_text() -> str:
    if not DOC.exists():
        pytest.fail(f"{DOC} does not exist")
    return DOC.read_text(encoding="utf-8")


def _codex_section() -> str:
    """The `## Codex` section slice: from its heading to the next `##` or EOF."""
    text = _doc_text()
    match = _CODEX_HEADING.search(text)
    if not match:
        pytest.fail("docs/INTEGRATIONS_LEGAL.md has no '## Codex' heading")
    rest = text[match.end():]
    next_heading = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: next_heading.start()] if next_heading else rest


def test_integrations_legal_doc_exists_with_codex_section():
    """The RED assertion: before this ticket's implementation, the doc did not
    exist at all, so both the file-existence check and the heading regex fail.
    This is the failure captured as this file's RED evidence."""
    assert DOC.exists(), f"{DOC} does not exist"
    text = DOC.read_text(encoding="utf-8")
    assert _CODEX_HEADING.search(text), "docs/INTEGRATIONS_LEGAL.md has no '## Codex' heading"


def test_codex_section_carries_three_sourced_quotes():
    section = _codex_section()
    quoted_blocks = re.findall(r'"([^"]{20,})"', section)
    assert len(set(quoted_blocks)) >= 3, (
        f"expected >= 3 distinct quoted passages, found {len(set(quoted_blocks))}"
    )
    for needle in (
        "two ways for a person to sign in",
        "support both",
        "CI/CD jobs",
    ):
        assert needle in section, f"missing quoted substring: {needle!r}"


def test_codex_section_names_source_urls():
    section = _codex_section()
    assert "developers.openai.com/codex/auth" in section
    assert "learn.chatgpt.com/docs/auth" in section


def test_fetch_dates_are_iso_formatted():
    section = _codex_section()
    dates = _DATE_RE.findall(section)
    assert dates, "no ISO-formatted (YYYY-MM-DD) date found in the Codex section"
    for d in dates:
        date.fromisoformat(d)  # raises ValueError if not a real ISO date
    assert not _NON_ISO_DATE_RE.search(section), (
        "found a non-ISO date form (e.g. 22/08/2026 or Aug 22, 2026) in the Codex section"
    )
    assert "2026-08-22" in section


def _paragraphs(section: str) -> list[str]:
    """Split into blank-line-delimited blocks, collapsing hard-wrapped
    newlines within each one, so a phrase split across wrapped lines (the
    house style — see docs/BACKENDS.md) is still one contiguous string to
    search. A physical *line* is the wrong unit for prose written at an
    ~80-column wrap; a paragraph is not."""
    return [
        re.sub(r"\s+", " ", block).strip()
        for block in re.split(r"\n\s*\n", section)
        if block.strip()
    ]


def test_unfavourable_half_and_8338_named_as_partial():
    section = _codex_section()
    assert "CI/CD" in section
    assert "#8338" in section
    matching = [p for p in _paragraphs(section) if "#8338" in p]
    assert matching, "no paragraph names #8338"
    assert any(
        "licensing half" in p or "unresolved" in p or "unanswered" in p
        for p in matching
    ), f"no #8338 paragraph carries a partiality word: {matching!r}"


def test_withdrawn_prohibition_named_as_withdrawn():
    section = _codex_section()
    matching = [p for p in _paragraphs(section) if re.search(r"\bprohibit\w*\b", p)]
    assert matching, "no paragraph mentions 'prohibit'"
    assert any(
        "withdrawn" in p or "has been removed" in p for p in matching
    ), f"no prohibit-mentioning paragraph names itself withdrawn: {matching!r}"
    assert not re.search(r"OpenAI's terms prohibit", section), (
        "section asserts a live prohibition instead of a withdrawn one"
    )


def test_auth_modes_match_the_code():
    section = _codex_section()
    assert CODEX_AUTH_MODES, "no_human.config.CODEX_AUTH_MODES is empty"
    for mode in CODEX_AUTH_MODES:
        assert mode in section, f"auth mode {mode!r} is not named in the Codex section"
    assert "src/no_human/config.py" in section, (
        "the Codex section does not name the file it checked for auth modes"
    )


def test_codex_auth_json_is_never_read_is_stated():
    section = _codex_section()
    assert "~/.codex/auth.json" in section
    assert "never read" in section or "never reads" in section


def test_nothing_is_stated_as_settled_law():
    section = _codex_section()
    assert "a lawyer should still settle it" in section
    assert "not a finding of law" in section
    for settled_phrase in ("is legal", "is permitted by OpenAI", "settled law"):
        assert settled_phrase not in section, (
            f"section asserts settled legality via {settled_phrase!r}"
        )
