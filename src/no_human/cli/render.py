"""Boundary helper for printing externally-sourced text through Rich markup.

`Console.print` (with the default `markup=True`) parses `[...]` in the text it
is given as style tags. Any field that did not originate as a literal in this
repo's source — a task's title, description, acceptance criteria, an event's
`text`/`actor`, an attempt's `branch_name`, a verifier's `label`, and the same
class of value wherever else the CLI prints one — can contain an unbalanced or
well-formed-looking bracket (a Python list literal, a regex character class, a
Markdown link, a `[WIP-BLOCKED]` tag). Rich either silently drops the bracketed
span or raises `rich.markup.MarkupError`, taking the whole command down.

Use `esc(value)` at the point such a value is interpolated into an f-string or
handed to `Table.add_row`. It never raises and is safe for `None` and
non-`str` values (ints, enums, ...), which `rich.markup.escape` alone is not —
it requires a `str` and raises on anything else.

This module intentionally does NOT provide a `console.print`-replacing helper
that escapes a whole rendered line: this repo's CLI deliberately embeds its
OWN style tags (`[bold]`, `[blue]`, ...) directly in the same f-strings, and a
line-level escape would destroy them. `esc()` is applied only to the
externally-sourced value, leaving the surrounding literal markup intact.
"""

from __future__ import annotations

from rich.markup import escape


def esc(value: object) -> str:
    """Render an externally-sourced value for a markup-enabled console.

    `None` becomes `""`; anything else is stringified first (so ints, enums,
    and other non-`str` field values are handled) and then markup-escaped.
    """
    if value is None:
        return ""
    return escape(str(value))
