"""`no_human.intake.criteria` — what an issue body's acceptance criteria are.

The adapter-level wiring is covered in `test_intake.py`, `test_jira_intake.py`
and `test_intake_linear.py`. These pin the extraction itself, and each case is
a body shape that really occurs in this repository's own issues.
"""

from __future__ import annotations

import logging

import pytest

from no_human.intake.criteria import extract_acceptance_criteria


def extract(body: str, name: str = "GitHub issue o/r#1") -> list[str]:
    return extract_acceptance_criteria(body, name)


@pytest.mark.parametrize("heading", [
    "## Acceptance criteria",
    "## Acceptance criteria (recorded API fixtures)",   # issue #430, verbatim
    "## Acceptance criteria:",
    "### Acceptance Criteria",
    "#### acceptance criteria — must all hold",
])
def test_the_heading_only_has_to_start_with_the_phrase(heading):
    """Anchoring the heading to end-of-line is why issue #430 ingested with no
    criteria AND no warning: everything after the phrase is ordinary."""
    assert extract(f"{heading}\n- first\n- second\n") == ["first", "second"]


def test_a_numbered_list_is_criteria_too():
    """#430 and several others here write their criteria as `1.`/`2.`."""
    assert extract("## Acceptance criteria\n1. first\n2) second\n") == ["first", "second"]


def test_the_section_outranks_an_unrelated_checklist_elsewhere():
    """A Jira "Definition of done", or a contributor's own to-do list, must not
    speak for criteria written in plain bullets under the heading. Reading the
    whole body for checkboxes meant the wrong list won silently — and wrong
    criteria are graded as if they were real, where empty ones at least warn.
    """
    body = (
        "## Acceptance criteria\n- retries three times\n- logs the failure\n"
        "## Definition of done\n- [x] PR raised\n- [ ] release notes updated\n"
    )
    assert extract(body) == ["retries three times", "logs the failure"]


def test_checkboxes_still_win_inside_the_section():
    body = "## Acceptance criteria\n- [ ] boxed one\n- plain one\n"
    assert extract(body) == ["boxed one"]


def test_a_body_with_only_checkboxes_is_unchanged():
    """The path every already-imported task was created through."""
    assert extract("Intro\n- [ ] one\n- [x] two\n") == ["one", "two"]


@pytest.mark.parametrize("section_body", [
    "",                          # an unfilled template stub
    "See the linked design doc.",   # a section that holds only prose
])
def test_a_section_that_yields_nothing_warns_rather_than_borrowing(section_body, caplog):
    """The body-wide checkbox sweep runs only when the body names no
    acceptance-criteria section AT ALL. Running it whenever a section merely
    came up empty reopens the case this change exists to close: a section
    holding prose, an unrelated checklist below it, and the checklist quietly
    adopted as the criteria. Wrong criteria are graded as if they were real;
    #511's third criterion asks for a warning here, not a guess."""
    body = (f"## Acceptance criteria\n{section_body}\n"
            "## Definition of done\n- [x] PR raised\n- [ ] notes updated\n")
    with caplog.at_level(logging.WARNING, logger="no_human.intake.criteria"):
        assert extract(body, "GitHub issue o/r#9") == []
    assert "GitHub issue o/r#9" in caplog.text
    assert "--criteria" in caplog.text


def test_a_later_heading_is_consulted_when_the_first_is_empty():
    body = (
        "## Acceptance criteria\n\nTo be filled in.\n"
        "## Notes\nprose\n"
        "## Acceptance criteria\n- the real one\n"
    )
    assert extract(body) == ["the real one"]


def test_a_fenced_block_inside_the_section_is_not_criteria():
    """Issue bodies here routinely paste diffs and shell transcripts. A `- `
    line inside one is not a criterion and a `#` line is not a heading, so an
    unaware reader both gains junk and ends the section early."""
    body = (
        "## Acceptance criteria\n"
        "- a real criterion\n"
        "```diff\n- removed line\n# not a heading\n```\n"
        "- another real one\n"
    )
    assert extract(body) == ["a real criterion", "another real one"]


def test_a_following_heading_of_the_same_level_ends_the_section():
    body = "## Acceptance criteria\n- mine\n## Notes\n- not mine\n"
    assert extract(body) == ["mine"]


def test_a_deeper_heading_does_not_end_the_section():
    body = "## Acceptance criteria\n- mine\n### Detail\n- also mine\n"
    assert extract(body) == ["mine", "also mine"]


def test_a_body_that_names_criteria_but_yields_none_warns(caplog):
    """The safety net is deliberately looser than the parser. `**Acceptance
    criteria**` is not a heading markdown can scope, but a body that writes one
    and yields nothing is exactly the silently ungradable task this warning
    exists to prevent."""
    with caplog.at_level(logging.WARNING, logger="no_human.intake.criteria"):
        assert extract("**Acceptance criteria**\n\nSee the thread.\n",
                       "GitHub issue o/r#7") == []
    assert "GitHub issue o/r#7" in caplog.text
    assert "--criteria" in caplog.text


def test_a_body_with_no_criteria_at_all_stays_silent(caplog):
    """Most issues are not written with criteria. Warning on all of them would
    make the warning worth ignoring."""
    with caplog.at_level(logging.WARNING, logger="no_human.intake.criteria"):
        assert extract("Just a bug report.\n") == []
    assert not caplog.records


def test_prose_about_acceptance_criteria_is_not_a_mention(caplog):
    """`The acceptance criteria are what the reviewer grades against` — a real
    sentence from issue #511. Mid-sentence use must not trip the net."""
    with caplog.at_level(logging.WARNING, logger="no_human.intake.criteria"):
        assert extract(
            "The acceptance criteria are what the reviewer grades against.\n") == []
    assert not caplog.records


def test_carriage_returns_do_not_hide_the_section():
    """A body fetched from a forge can carry CRLF; `splitlines` handles it, but
    a `$`-anchored item pattern would keep the `\\r` in the criterion text."""
    assert extract("## Acceptance criteria\r\n- first\r\n- second\r\n") == [
        "first", "second"]
