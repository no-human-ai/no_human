"""Shared acceptance-criteria extraction for issue-tracker intake."""

from __future__ import annotations

import logging
import re


log = logging.getLogger("no_human.intake.criteria")

# ATX headings need a space after the hashes, so `##Notes` is a paragraph and
# must not close a section. One pattern decides what a heading is, for both
# "does this open the acceptance-criteria section" and "does this close it";
# two spellings disagreeing is how `##Notes` came to be a closer in one place
# and not the other.
_HEADING = re.compile(r"^\s{0,3}(?P<hashes>#{1,6})\s+(?P<title>.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(?:```|~~~)")

# The heading TEXT only has to start with the phrase: "Acceptance criteria
# (recorded API fixtures)" and "Acceptance criteria:" are both ordinary here.
_ACCEPTANCE = re.compile(r"^acceptance\s+criteria\b", re.IGNORECASE)

# The safety net is deliberately looser than the parser: a bold or bare
# "Acceptance criteria" line is not a heading we can scope, but a body that
# writes one and yields nothing is exactly the silently-ungradable task this
# warning exists to prevent.
_ACCEPTANCE_MENTION = re.compile(
    r"^\s{0,3}(?:#{1,6}\s+|\*\*|__)?\s*acceptance\s+criteria\b", re.IGNORECASE)

_CHECKLIST_ITEM = re.compile(r"^\s*[-*+]\s*\[[ xX]\]\s*(.+?)\s*$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")
_ORDERED = re.compile(r"^\s*\d{1,3}[.)]\s+(.+?)\s*$")


def _outside_fences(lines: list[str]) -> list[tuple[int, str]]:
    """`(index, line)` for every line outside a fenced code block.

    Issue bodies here routinely carry ``` blocks holding diffs and shell
    transcripts. Their `- ` lines are not criteria and their `#` lines are not
    headings, so a section that contains one would otherwise both gain junk
    items and end early.
    """
    out: list[tuple[int, str]] = []
    fenced = False
    for index, line in enumerate(lines):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            out.append((index, line))
    return out


def _items(section: list[str]) -> list[str]:
    """Criteria from one section body, checkboxes first.

    Checkboxes stay authoritative wherever both appear, which is the format
    the existing imported tasks were written in. Plain and numbered lists are
    the same kind of claim and are read together, in document order, so a
    template that mixes them does not silently drop half.
    """
    checklist = [m.group(1) for line in section if (m := _CHECKLIST_ITEM.match(line))]
    if checklist:
        return checklist
    items: list[str] = []
    for line in section:
        if match := (_BULLET.match(line) or _ORDERED.match(line)):
            items.append(match.group(1))
    return items


def extract_acceptance_criteria(text: str, issue_name: str) -> list[str]:
    """Criteria from an issue body: the acceptance-criteria section if there is
    one, otherwise the body's checkboxes.

    The SECTION wins when it exists. A body often carries an unrelated checklist
    — a Jira "Definition of done", a contributor's own to-do list — and reading
    the whole body for checkboxes lets that list speak for criteria written in
    plain bullets under the heading. Falling back to a body-wide sweep only when
    there is no section keeps a checkbox-only issue ingesting as it always has.
    """
    lines = (text or "").splitlines()
    visible = _outside_fences(lines)

    sections: list[list[str]] = []
    for position, (_, line) in enumerate(visible):
        heading = _HEADING.match(line)
        if not heading or not _ACCEPTANCE.match(heading.group("title")):
            continue
        level = len(heading.group("hashes"))
        body: list[str] = []
        for _, section_line in visible[position + 1:]:
            closing = _HEADING.match(section_line)
            if closing and len(closing.group("hashes")) <= level:
                break
            body.append(section_line)
        sections.append(body)

    # Every section, not just the first: an issue template's empty stub heading
    # must not hide a filled-in one further down.
    for body in sections:
        if criteria := _items(body):
            return criteria

    # The body-wide sweep the adapters did before, and ONLY when the body
    # names no acceptance-criteria section at all. Running it whenever a
    # section merely came up empty reopens the case this whole change is
    # about: a section holding prose, an unrelated "Definition of done"
    # checklist below it, and the checklist silently adopted as the criteria.
    # A section that names itself and yields nothing is the case #511 asks to
    # WARN about, not to guess around.
    if not sections:
        checkboxes = [m.group(1) for _, line in visible
                      if (m := _CHECKLIST_ITEM.match(line))]
        if checkboxes:
            return checkboxes

    if sections or any(_ACCEPTANCE_MENTION.match(line) for _, line in visible):
        log.warning(
            "%s names acceptance criteria but none could be extracted; "
            "pass --criteria explicitly.", issue_name)
    return []
