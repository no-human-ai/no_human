"""Parse PR/MR references out of free text — full URLs OR human shorthand.

A code_review task is often described the way a human writes it in a ticket:
"code.example.com/dev/acme-test PR #7001" or
"gitlab.acme.net ci_gate/subgroup/metrics-core MR !7006" — not a full
``https://…/pull/7001`` URL, and usually more than one. The old single-URL
regex found zero of these and failed the task at the gate.

``parse_pr_refs`` recognises both forms and returns full, canonical URLs that
``pr_watcher.parse_pr_url`` can then parse — de-duplicated, order-preserving.
"""

from __future__ import annotations

import re

# Full URLs already in canonical form (GitHub/GHE …/pull/N, GitLab …/merge_requests/N).
_FULL = re.compile(r"https?://[^\s()<>]+?/(?:pull|merge_requests)/\d+")

# Human shorthand: "<host> <owner/group/.../repo> PR #<n>" or "… MR !<n>".
# host has at least one dot + a TLD; separator is a slash or whitespace; the repo
# path is the slug; then "PR #" (GitHub) or "MR !" (GitLab) and the number.
_SHORT = re.compile(
    r"(?P<host>[a-z0-9][a-z0-9.-]*\.[a-z]{2,})"   # host with a TLD
    r"[\s/]+"                                       # sep: slash or whitespace
    r"(?P<path>[a-z0-9][a-z0-9._/-]*?)"            # owner/group/…/repo slug
    r"\s+"
    r"(?:(?P<pr>PR)\s*#|(?P<mr>MR)\s*!)"          # PR #  or  MR !
    r"(?P<num>\d+)",
    re.IGNORECASE,
)


def parse_pr_refs(text: str) -> list[str]:
    """Return every PR/MR reference in *text* as a full canonical URL.

    Handles full URLs and shorthand like ``host/path PR #123`` / ``host path
    MR !45``. De-duplicated, in first-seen order. Empty list if none found.
    """
    if not text:
        return []
    # Collect both forms WITH each match's position, then order by position, so
    # the result is in true first-seen order rather than every full URL ahead
    # of every shorthand ref. Callers treat the first element as the primary
    # reference -- ``orchestrator._run_code_review`` anchors the review's
    # single-URL fields (the comment target, the UI link) on
    # ``parse_pr_refs(...)[0]`` -- so a task that names its primary PR as
    # shorthand and a secondary one as a full URL later must not have that
    # secondary URL hoisted to the front.
    found: list[tuple[int, str]] = []
    for m in _FULL.finditer(text):
        found.append((m.start(), m.group(0)))
    for m in _SHORT.finditer(text):
        host = m.group("host")
        path = m.group("path").rstrip("/")
        num = m.group("num")
        if m.group("mr"):
            found.append((m.start(), f"https://{host}/{path}/-/merge_requests/{num}"))
        else:
            found.append((m.start(), f"https://{host}/{path}/pull/{num}"))
    found.sort(key=lambda pair: pair[0])
    seen: set[str] = set()
    out: list[str] = []
    for _, url in found:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out
