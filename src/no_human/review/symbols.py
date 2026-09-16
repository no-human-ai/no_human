"""Declaration extraction for wiring evidence — polyglot, depth-aware, and
carried by nothing but the standard library (issue #114 phase 4).

`wiring_evidence` asks one question: does a symbol this diff ADDS have any
reference outside its own file? Answering it needs two halves. The reference
half was already language-agnostic — `git grep -w` reads bytes, not syntax.
The declaration half was not: it was `ast.parse` over `.py` files, module top
level only, so a diff in any other language declared nothing and a method
declared nothing either.

This module is that half, widened on both axes:

* **Python** comes from `ast`, at module level AND inside class bodies, with
  qualified names (`Store.close`).
* **The JS/TS family** comes from a line scanner over source whose comments
  and string literals have been blanked, matching `export`ed module-level
  `function`, `class`, `const`/`let`/`var`, `interface`, `type` and `enum`
  declarations. A CommonJS `module.exports = {...}` is not read, so a
  `require`-style file contributes nothing.

Three rules keep the widening from turning an advisory block into noise, and
each is a deliberate ceiling rather than a gap to fill later:

* **A `def` inside a function is not collected.** A closure is local by
  construction, so "no reference outside this file" is true of nearly every
  one of them and says nothing about whether it is wired. Only class bodies
  are descended.
* **Dunder methods are not collected.** `__init__`, `__enter__` and their kin
  are invoked by the language, never by name, so a reference search cannot
  find the call that does exist.
* **An unexported JS binding is not collected.** `const MAX = 10` beside its
  only use is the ordinary shape of a module, not an unwired symbol, and
  collecting those would have buried the real findings under them.

Everything here is pure, total and deterministic: any input that cannot be
parsed yields `{}`, never an exception and never a guess. A missed
declaration costs the reviewer one line of evidence; an invented one would
cost it an accusation, so every ambiguity resolves toward the miss.
"""

from __future__ import annotations

import ast
import re

#: Suffixes each reader claims. A path outside both tuples contributes no
#: declarations at all — the honest answer for a language nothing here parses.
PYTHON_EXTENSIONS = (".py",)
JS_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")

_IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]*"

#: Anchored at column zero — it is only ever applied with `re.match` — and at
#: `export`, both on purpose. An indented declaration sits inside a function,
#: a class or a `declare module` block, and this scanner has no block
#: structure to tell which; an unexported one is file-private by construction,
#: the same reason a Python closure is skipped. What is left is the module's
#: declared surface, the only part of it another file could have wired.
_JS_DECLARATION = re.compile(
    rf"""
    export \s+ (?:default\s+)? (?:declare\s+)? (?:abstract\s+)? (?:async\s+)?
    (?:
        function \s* \*? \s* (?P<function>{_IDENTIFIER})
      | class \s+ (?P<class>{_IDENTIFIER})
      | (?:const|let|var) \s+ (?P<binding>{_IDENTIFIER}) \s* =
      | (?:interface|enum) \s+ (?P<nominal>{_IDENTIFIER}) \b
      | type \s+ (?P<alias>{_IDENTIFIER}) \s* =
    )
    """,
    re.VERBOSE,
)


def language_of(rel: str) -> str | None:
    """The reader for `rel`, or None when no reader claims it."""
    lowered = rel.lower()
    # A `.d.ts` stub declares names that are defined elsewhere, so every name
    # in one would read as added-and-unreferenced.
    if lowered.endswith(".d.ts"):
        return None
    if lowered.endswith(PYTHON_EXTENSIONS):
        return "python"
    if lowered.endswith(JS_EXTENSIONS):
        return "javascript"
    return None


def declared_symbols(text: str, rel: str) -> dict[str, str]:
    """Qualified name -> the bare name a reference search looks for.

    `{}` for an unreadable file, an unparseable one, or a language no reader
    claims. Never raises.
    """
    language = language_of(rel)
    if language == "python":
        return _python_symbols(text)
    if language == "javascript":
        return _javascript_symbols(text)
    return {}


#: The node types a declaration can be. Bound once: the walk below asks per
#: statement.
_DEFINITION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _python_symbols(text: str) -> dict[str, str]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return {}
    found: dict[str, str] = {}

    def collect(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if not isinstance(node, _DEFINITION_NODES):
                continue
            qualified = prefix + node.name
            if not _is_dunder(node.name):
                found[qualified] = node.name
            if isinstance(node, ast.ClassDef):
                collect(node.body, qualified + ".")

    collect(tree.body, "")
    return found


def _javascript_symbols(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in _blank_literals(text).splitlines():
        # `match`, not `search`: a declaration indented under a `declare
        # module` block or a class body names something this scanner cannot
        # place, and only the start of a line can be module level.
        match = _JS_DECLARATION.match(line)
        if match is None:
            continue
        name = next(value for value in match.groups() if value)
        found[name] = name
    return found


def _blank_literals(text: str) -> str:
    """`text` with comment and string-literal characters replaced by spaces.

    A `class` inside a comment or a quoted string is not a declaration, and
    the scanner above has no way to tell without this pass. Newlines survive
    so line structure and column zero still mean what they meant.

    `'` and `"` states are closed at the newline, because JS closes them
    there too: an apostrophe in JSX text (`<p>don't</p>`) then costs the rest
    of one line instead of the rest of the file.
    """
    out = list(text)
    index = 0
    end = len(text)
    quote: str | None = None
    while index < end:
        char = text[index]
        if quote is not None:
            if char == "\n":
                if quote != "`":
                    quote = None
                index += 1
                continue
            if char == "\\" and index + 1 < end:
                out[index] = " "
                if text[index + 1] != "\n":
                    out[index + 1] = " "
                index += 2
                continue
            if char == quote:
                quote = None
            out[index] = " "
            index += 1
            continue
        pair = text[index:index + 2]
        if pair == "//":
            while index < end and text[index] != "\n":
                out[index] = " "
                index += 1
            continue
        if pair == "/*":
            while index < end:
                closing = text[index:index + 2] == "*/"
                if text[index] != "\n":
                    out[index] = " "
                index += 1
                if closing:
                    out[index] = " "
                    index += 1
                    break
            continue
        if char in "'\"`":
            quote = char
            out[index] = " "
            index += 1
            continue
        index += 1
    return "".join(out)
