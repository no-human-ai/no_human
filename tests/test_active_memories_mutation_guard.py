"""`_active_memories` is a property, not a plain attribute: every read runs
the memory-term screen and hands back a list nobody else holds a reference
to. That makes the write form irrelevant (see the comment above the property
in orchestrator.py) but it also means

    self._active_memories.append(mem)

is a silent no-op — it mutates a throwaway list and raises nothing. Nothing
short of a test catches this: the symptom (a memory that never reaches the
prompt) is indistinguishable from the screen legitimately holding it.

This file has two jobs:

1. A static guard (`_find_active_memories_mutations`) that scans
   `orchestrator.py`'s source text for in-place mutation of
   `self._active_memories` and fails the suite if it finds one. This is a
   regex over source, which this repo has been burned by before (a reviewer
   defeated an assignment-side guard here with nine spellings) — so its
   detection surface is tested explicitly below, and its docstring says
   plainly what it cannot see.
2. Identity tests proving the getter never hands back the backing list —
   including the empty-backing-list case, which a short-circuit in an
   earlier version of the getter got wrong (an empty backing list was
   returned BY IDENTITY, so a read-then-append actually persisted).
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest

_ORCHESTRATOR_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "no_human" / "core" / "orchestrator.py"
)

# Ordinary list-mutation methods. NOT included: `+=` — the docstring on the
# property is explicit that `+=` is get, `__iadd__`, set, and therefore DOES
# persist through the setter; it is the sanctioned spelling, not a bug.
_MUTATION_METHODS = (
    "append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse",
)

_DIRECT_MUTATION_RE = re.compile(
    r"self\._active_memories\s*"
    r"(?:\.\s*(?:" + "|".join(_MUTATION_METHODS) + r")\s*\("
    r"|\[[^\]]*\]\s*=(?!=))"
)

_ALIAS_ASSIGN_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*self\._active_memories\s*$"
)


def _mask_strings_and_comments(source: str) -> str:
    """Blank out the TEXT of every STRING and COMMENT token, byte-for-byte in
    place (same line/column layout), so the regex below sees only executable
    code. Line-level masking is not enough: `self._active_memories.append({"title": ...})`
    is real code that also CONTAINS string literals, and a whole-line skip
    would hide it along with any docstring/comment prose that merely talks
    about the mutation (this property's own docstring uses
    `self._active_memories.append(x)` as its example of what NOT to do).
    On any tokenize failure, returns the source unmodified — fails toward
    over-detection (a possible false positive), never under-detection."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source
    lines = source.splitlines(keepends=True)
    for tok in tokens:
        if tok.type not in (tokenize.STRING, tokenize.COMMENT):
            continue
        (srow, scol), (erow, ecol) = tok.start, tok.end
        if srow == erow:
            line = lines[srow - 1]
            lines[srow - 1] = line[:scol] + (" " * (ecol - scol)) + line[ecol:]
            continue
        first = lines[srow - 1]
        nl = "\n" if first.endswith("\n") else ""
        lines[srow - 1] = first[:scol] + " " * (len(first) - len(nl) - scol) + nl
        for i in range(srow, erow - 1):
            mid = lines[i]
            mnl = "\n" if mid.endswith("\n") else ""
            lines[i] = " " * (len(mid) - len(mnl)) + mnl
        last = lines[erow - 1]
        lines[erow - 1] = " " * ecol + last[ecol:]
    return "".join(lines)


def _find_active_memories_mutations(source: str) -> list[str]:
    """Return `"<lineno>: <line>"` for every line that looks like an in-place
    mutation of `self._active_memories` (or a simple local alias of it).

    DETECTS:
      - `self._active_memories.append(...)` / `.extend` / `.insert` /
        `.remove` / `.pop` / `.clear` / `.sort` / `.reverse`
      - `self._active_memories[i] = ...` / slice assignment
      - the same, one level aliased: `x = self._active_memories` on its own
        line, followed anywhere later in the file by `x.append(...)` etc.
        (tracked file-wide once assigned; NOT re-scoped to a function, so a
        later reuse of the same local name in another method mid-file can
        also be flagged — a false positive in the safe direction).

    CANNOT SEE (documented, not silently assumed away):
      - aliases built any other way: through a second hop
        (`y = x; y.append(...)`), a function argument, a comprehension, a
        container (`[self._active_memories][0].append(...)`), `getattr`, or
        `setattr`.
      - mutation via a method called through indirection, e.g.
        `getattr(self._active_memories, "append")(x)`.
      - anything split across a line continuation or built as a multi-line
        expression the regex doesn't span.
      - this IS an AST-adjacent (tokenize-based) scan for the "is this line
        code at all" question, but the mutation pattern itself is matched by
        regex, not by walking the parse tree — so a semantically equivalent
        but differently-shaped expression (e.g. mutation reached through a
        computed attribute name) can still slip past.
    This is exactly the class of gap the repo's docstring on the property
    already warns about: a regex is a claim about what is WRITTEN, not a
    closed set of ways to write it. It is a second line of defense on top of
    the read-side screen, not a replacement for it.
    """
    findings: list[str] = []
    aliases: set[str] = set()
    masked = _mask_strings_and_comments(source)
    masked_lines = masked.splitlines()
    original_lines = source.splitlines()
    for lineno, line in enumerate(masked_lines, start=1):
        original = original_lines[lineno - 1]
        if _DIRECT_MUTATION_RE.search(line):
            findings.append(f"{lineno}: {original.strip()}")
            continue
        alias_match = _ALIAS_ASSIGN_RE.match(line)
        if alias_match:
            aliases.add(alias_match.group(1))
            continue
        for alias in aliases:
            alias_re = re.compile(
                r"\b" + re.escape(alias) + r"\s*"
                r"(?:\.\s*(?:" + "|".join(_MUTATION_METHODS) + r")\s*\("
                r"|\[[^\]]*\]\s*=(?!=))"
            )
            if alias_re.search(line):
                findings.append(f"{lineno}: {original.strip()}")
    return findings


# --- Detection-surface tests: the checker itself, against synthetic source ---
# (Not the real file — these prove the checker CAN see each spelling before
# we trust it to police the real file below.)

def test_detects_direct_append():
    assert _find_active_memories_mutations("self._active_memories.append(mem)\n")


def test_detects_direct_extend():
    assert _find_active_memories_mutations("self._active_memories.extend(mems)\n")


def test_detects_item_assignment():
    assert _find_active_memories_mutations("self._active_memories[0] = mem\n")


def test_detects_aliased_mutation():
    """The aliased form named in the ticket:
    `m = self._active_memories; m.append(x)` (as two statements — the alias
    is matched on its own assignment line, then the alias name is watched
    for the rest of the synthetic snippet)."""
    src = "m = self._active_memories\nm.append(x)\n"
    assert _find_active_memories_mutations(src)


def test_does_not_flag_assignment_or_iadd():
    """`=` and `+=` are the sanctioned forms (they route through the setter,
    which screens) and must never be flagged."""
    src = (
        "self._active_memories = kept\n"
        "self._active_memories += extra\n"
    )
    assert _find_active_memories_mutations(src) == []


def test_does_not_flag_unrelated_list_mutation():
    """A mutation of some OTHER list must not trip the guard."""
    src = "other_list.append(mem)\nself._active_memories_backing_copy.append(x)\n"
    assert _find_active_memories_mutations(src) == []


def test_does_not_flag_prose_about_mutation():
    """A docstring or comment that mentions the dangerous spelling as an
    EXAMPLE (exactly what this property's own docstring does) must not be
    flagged — otherwise the guard is permanently red against legitimate
    documentation, which is worse than useless."""
    src = (
        '"""\n'
        "Identity is NOT stable, so `self._active_memories.append(x)` is a\n"
        "silent no-op.\n"
        '"""\n'
        "# also: self._active_memories.append(x) is a footgun, do not do it\n"
    )
    assert _find_active_memories_mutations(src) == []


# --- The actual regression guard, against the real file ---

def test_real_orchestrator_has_no_active_memories_mutation():
    """RED/GREEN proof (run manually, not by this test itself): insert
    `self._active_memories.append(mem)` as real code anywhere in
    orchestrator.py and this test fails, listing the offending line; remove
    it and this test passes again. That is the actual acceptance check for
    this ticket — the synthetic tests above only establish the checker can
    see each spelling (and ignore prose) before this one is trusted to
    police the real file.
    """
    source = _ORCHESTRATOR_PATH.read_text(encoding="utf-8")
    findings = _find_active_memories_mutations(source)
    assert findings == [], (
        "in-place mutation of the `_active_memories` property found — this "
        "is a silent no-op (see the property's docstring); use assignment or "
        "`+=` instead:\n" + "\n".join(findings)
    )


# --- Identity + screening behaviour of the property itself ---


@pytest.fixture
def orch():
    """A bare, un-initialised Orchestrator — enough to exercise the property
    and its setter without paying for the real constructor's setup, matching
    the pattern already used in tests/test_memory_term_screen.py."""
    from no_human.core.orchestrator import Orchestrator
    return object.__new__(Orchestrator)


def _mem(title="a rule", content="always run the tests first"):
    return {"title": title, "content": content}


def test_read_returns_a_fresh_list_not_the_backing_list(orch):
    orch._active_memories = [_mem("one"), _mem("two")]
    backing = getattr(orch, orch._ACTIVE_MEMORIES_RAW)
    first_read = orch._active_memories
    second_read = orch._active_memories
    assert first_read is not backing, "the getter returned the backing list by identity"
    assert first_read is not second_read, "two reads returned the same object"
    assert first_read == second_read == backing


def test_read_returns_a_fresh_list_when_backing_is_empty(orch):
    """Regression for the bug this ticket's prior review caught: the getter
    used to short-circuit `if not raw: return raw if raw is not None else []`
    — so with an EMPTY backing list (falsy, but not None) it returned the
    backing list itself. Same identity, and appending to the "read" value
    silently persisted into the backing store. Both non-empty (above) and
    empty backing lists must give a list that is never the backing object.
    """
    orch._active_memories = []
    backing = getattr(orch, orch._ACTIVE_MEMORIES_RAW)
    assert backing == []
    first_read = orch._active_memories
    second_read = orch._active_memories
    assert first_read is not backing, "empty-backing read returned the backing list by identity"
    assert first_read is not second_read, "two empty-backing reads returned the same object"

    first_read.append(_mem("sneaky"))
    assert getattr(orch, orch._ACTIVE_MEMORIES_RAW) == [], (
        "mutating a value read from the property persisted into the backing "
        "store — the identity guarantee was violated for an empty list"
    )


def test_property_read_only_returns_screened_entries(orch):
    from no_human.eval.vendor_terms import BANNED_TERMS

    term = BANNED_TERMS[0]
    orch._active_memories = [
        _mem("clean", "nothing sensitive here"),
        _mem("dirty", f"mentions {term} by name"),
    ]
    result = orch._active_memories
    assert [m["title"] for m in result] == ["clean"]
    # the held rule is tracked (not deleted) so it can be found and cleaned
    assert orch._memories_held_for_terms == ["dirty"]
