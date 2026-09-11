"""Every `<doc>.md#<anchor>` reference in the repo must resolve to a heading.

This exists because one of them was printed AT THE USER. When a profile asks
for CI and no backend can be built, `orchestrator.py` emits
`ci_backend_unavailable` ending in a pointer to the CI section of
`docs/adapters.md` — and it named that section `#ci`, which is not an anchor
that file has. The heading is ``## CI (`no_human/ci/`)``, which GitHub slugs to
`ci-no_humanci`, so the link a user follows out of an error message lands at
the top of the page with no indication that it missed. Six more references were
broken the same way, all written by hand from the heading's *text* rather than
from its slug.

(Note the shape of this docstring: it describes a bad reference without
spelling one, because this file is itself tracked and this guard reads it. The
first version of this test passed only because it was still untracked when it
ran — it could not see its own prose. That is the "a probe must match the
tool's universe" failure in miniature, and it is why the run that matters is
the one after `git add`.)

A broken anchor is invisible to every other check in this repo: the file
exists, the link is a 200, and the page renders. Only the fragment is wrong,
and nothing but a resolver notices. So the guard is a resolver.

The slug function is GitHub's documented rule (lowercase; drop everything that
is not alphanumeric, space, hyphen or underscore; spaces become hyphens). It is
self-tested below against headings whose anchors are independently known to
work, because a checker built out of the same guess as the thing it checks can
agree with itself and be wrong — the failure mode this repo has hit before.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories whose markdown is not ours to hold to this rule.
_SKIP_PARTS = {"node_modules", ".git", "dist", "build"}

_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$")
# `name.md#anchor` — the reference form used in prose, comments and messages.
_REFERENCE = re.compile(r"([A-Za-z0-9_\-]+\.md)#([A-Za-z0-9_\-]+)")

_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff",
                    ".woff2", ".zip", ".whl", ".svg"}


def github_slug(heading: str) -> str:
    """GitHub's heading-to-anchor rule."""
    s = heading.strip().lower()
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def test_the_slugger_matches_anchors_that_are_known_to_work():
    """Control. Without this the resolver below could be uniformly wrong and
    still report a clean run, because it would be comparing its own guess to
    its own guess."""
    # Plain heading — the simplest case, and `adapters.md#jira` is a link that
    # has worked in the quickstart for as long as the section has existed.
    assert github_slug("Jira") == "jira"
    # Punctuation is dropped, not replaced: this is the exact shape that was
    # got wrong seven times.
    assert github_slug("CI (`no_human/ci/`)") == "ci-no_humanci"
    # Digits and a full stop, as in the quickstart's numbered steps.
    assert github_slug("4. Add your first task") == "4-add-your-first-task"
    # Hyphens survive; spaces become hyphens.
    assert github_slug("How a CI result gates the loop") == \
        "how-a-ci-result-gates-the-loop"
    assert github_slug("Notify-out (`no_human/notify/`)") == \
        "notify-out-no_humannotify"


def _anchors_by_filename() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for md in REPO_ROOT.rglob("*.md"):
        if _SKIP_PARTS & set(md.parts):
            continue
        found = set()
        for line in md.read_text(errors="replace", encoding="utf-8").splitlines():
            m = _HEADING.match(line)
            if m:
                found.add(github_slug(m.group(1)))
        out.setdefault(md.name, set()).update(found)
    return out


def _tracked_files() -> list[str]:
    return subprocess.check_output(
        ["git", "ls-files"], cwd=REPO_ROOT, text=True).split()


def test_every_markdown_anchor_reference_resolves():
    """The guard itself, over every tracked file — prose, code and comments.

    Scoped to references whose TARGET FILE exists in this repo: a link to some
    other project's `README.md#install` is not ours to resolve, and treating it
    as a failure would make the check unmaintainable and then muted.
    """
    anchors = _anchors_by_filename()
    broken: list[str] = []
    checked = 0

    for rel in _tracked_files():
        path = REPO_ROOT / rel
        if not path.is_file() or path.suffix.lower() in _BINARY_SUFFIXES:
            continue
        try:
            text = path.read_text(errors="replace", encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, anchor in _REFERENCE.findall(line):
                if name not in anchors:
                    continue  # target is not a document in this repo
                checked += 1
                if anchor not in anchors[name]:
                    broken.append(
                        f"{rel}:{lineno} -> {name}#{anchor} "
                        f"(headings there slug to: "
                        f"{', '.join(sorted(anchors[name])) or '<none>'})")

    # Fail closed. Zero references checked means the reference regex stopped
    # matching, and a resolver that resolves nothing reports a clean run
    # forever — indistinguishable from every link being correct.
    assert checked > 0, (
        "no in-repo `<file>.md#anchor` references were found at all — the "
        "matcher has drifted and this guard is measuring nothing")

    assert not broken, (
        f"{len(broken)} of {checked} in-repo doc anchors do not resolve. A "
        f"user following one lands at the top of the page with no error:\n  "
        + "\n  ".join(broken))
