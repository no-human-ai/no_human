"""Display-only short title for tasks whose title is a whole sentence.

Tasks filed through the grill already get a refined title
(``intake/grill.py``); tasks from ``nh task add``, trackers and the MCP bridge
carry the raw first sentence. This never mutates the stored title — tracker
dedupe keys on it (``web/src/App.jsx`` "dedupe keys" comment).

A title whose (quote-stripped) length is already at or under ``limit`` is
returned as-is, colon and all — the separator cut only fires when the title
would otherwise overflow the limit.
"""
from __future__ import annotations

_SEPARATORS = (" — ", " – ", ": ", " (", " -- ")
_QUOTES = "`\"'"


def title_short(title: str, limit: int = 60) -> str:
    t = (title or "").strip()
    # Drop a wrapping quote/backtick pair around a leading token (e.g. a
    # `path/like/this` a tracker or CLI quoted) — a plain .strip("`\"'")
    # only trims the string's very edges, missing a closing mark mid-string.
    if t and t[0] in _QUOTES:
        end = t.find(t[0], 1)
        # Unwrap a quoted leading TOKEN (`path/like/this`) or a fully
        # wrapped title, but NOT a quoted phrase that has text after it:
        # if the closing mark sits mid-string with whitespace inside the
        # span, stripping the pair would delete a quote from mid-text, so
        # a title like '"Fix the bug," said the reviewer' lost its inner
        # quote and read as if nothing was quoted at all.
        if end > 0 and (end == len(t) - 1
                        or not any(c.isspace() for c in t[1:end])):
            t = t[1:end] + t[end + 1 :]
    t = t.strip()
    if len(t) <= limit:
        return t
    cut = min((i for i in (t.find(s) for s in _SEPARATORS) if 0 < i <= limit), default=-1)
    if cut > 0:
        return t[:cut].rstrip()
    return t[: limit - 1].rstrip() + "…"
