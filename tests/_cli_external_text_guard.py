"""Structural AST guard: does every `console.print` / `*.print` / `*.rule` /
`*.add_row` call in `src/no_human/cli/*.py` protect its externally-sourced
arguments before Rich's markup parser sees them?

WHY A NEW GUARD rather than widening `tests/_bench_ast_guard.py`. That guard's
own docstring documents three blind spots it was shipped with and never fixed:
it only looks at `ast.FormattedValue` nodes (so a bare `console.print(x)`, a
`"foo " + x` concat, an `"{}".format(x)`, or an `x % y` are all invisible to
it), and it requires `escape(...)` to appear ANYWHERE in the interpolation's
subtree rather than at its root (so `f"{escape(other) + real_target}"` would
satisfy it while `real_target` prints raw). Its allowlist is also scope-pinned
to the bench command group and its docstring says as much: "if this guard is
ever widened past the bench group, this allowlist must be re-derived per
scope, not carried over." So this is a separate module with its own class
definition, not a patch to that one.

THE CLASS THIS GUARD ENFORCES ("externally-sourced text"), by shape:

  1. Attribute access whose attribute name is a text-bearing field of a repo
     data model that carries human- or model-authored prose: `Task`
     (title/description/acceptance_criteria/...), `Blocker` /
     `BlockerOption` (the agent's own escalation report — question,
     root_cause_hypothesis, wake_condition, goal, evidence, tried, label),
     and `ContextChunk` (source/title/content/ref pulled from an external
     context source). Fields are read via `dataclasses.fields(...)` so a
     FIELD ADDED to any of these models is covered automatically — no name
     has to be re-typed into this file for that to happen.
  2. Subscript access by a string literal key drawn from the measured set of
     DB/event row dict keys that carry the same class of text (`'text'`,
     `'title'`, `'actor'`, `'line'`, `'reason'`).
  3. A local name — bound by a `for` loop target, a comprehension target, or
     a plain assignment, anywhere in the enclosing function — whose bound
     value is itself one of the above. This is what makes
     `for c in t.acceptance_criteria: console.print(f"- {c}")` visible: `c`
     is untainted by shape #1 alone (it is a bare `Name` at the print site),
     but is tainted by the loop that produced it.

Detection is transparent through shapes that preserve the underlying
characters — slicing (`x[:200]`), `or`/ternary fallbacks, string
concatenation, `%`-formatting, `.format()`, and calls to `str`/`repr`/other
unknown functions passed the tainted value as an argument — because
truncating, defaulting, or reformatting external text does not make it any
safer to hand to a markup parser. It stops at `esc(...)` / `escape(...)`,
which is the one call this guard treats as neutralizing its argument, and at
known int-only calls (`len`, `int`, `float`, `round`, `sum`, `abs`, `bool`),
which cannot carry a bracket by construction.

A print/rule/add_row argument is an OFFENDER if, at its root (the top-level
expression for a bare argument, or the value inside each `FormattedValue` for
an f-string), it is shaped as external text per the above and is not directly
wrapped in `esc(...)` or `escape(...)` at that root.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

from no_human.blockers.taxonomy import Blocker, BlockerOption
from no_human.context.base import ContextChunk
from no_human.core.task import Task
from no_human.intake.grill import GrillQuestion

PRINT_LIKE_ATTRS = {"print", "rule", "add_row"}
_PROTECT_NAMES = {"esc", "escape"}
_SAFE_INT_CALLS = {"len", "int", "float", "round", "sum", "abs", "bool"}
_TRANSPARENT_CALLS = {"str", "repr"}


def _dataclass_fields(cls, *, exclude: set[str]) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)} - exclude


#: Task fields NOT in this set: `status` (a `TaskStatus` enum, closed
#: vocabulary), `context`/`plan`/`config` (internal dict state, not rendered
#: as free text at these call sites), `created_at`/`updated_at`/
#: `wake_check_at` (program-stamped timestamps), `parent_id`/`follows_id`
#: (internal ids, not user-authored text).
TASK_TEXT_FIELDS = _dataclass_fields(Task, exclude={
    "status", "context", "plan", "config",
    "created_at", "updated_at", "wake_check_at",
    "parent_id", "follows_id",
})

#: Blocker fields NOT in this set: `category` (enum), `transient` (bool),
#: `confidence` (float), `options` (a list of `BlockerOption` — its own
#: `.label` is covered separately), `resume_branch`/`resume_commit` (git
#: refs the harness derives, not agent prose), `raised_at` (timestamp).
BLOCKER_TEXT_FIELDS = _dataclass_fields(Blocker, exclude={
    "category", "transient", "confidence", "options", "raised_at",
    "resume_branch", "resume_commit",
})

#: BlockerOption fields NOT in this set: `action` (a dict describing an
#: applyable change, not prose).
BLOCKER_OPTION_TEXT_FIELDS = _dataclass_fields(BlockerOption, exclude={"action"})

#: ContextChunk fields NOT in this set: `score` (a float ordering hint).
CONTEXT_CHUNK_TEXT_FIELDS = _dataclass_fields(ContextChunk, exclude={"score"})

#: GrillQuestion fields NOT in this set: `round` (an int), and the three
#: per-step token counters (ints). `question` and each entry of `suggestions`
#: are model-generated prose asked interactively during `nh task new`'s grill
#: — the exact class of value this guard exists for, just from the intake
#: pipeline rather than a stored `Task`.
GRILL_QUESTION_TEXT_FIELDS = _dataclass_fields(GrillQuestion, exclude={
    "round", "tokens_used", "cache_read_tokens", "cache_creation_tokens",
})

#: `_Finding` (src/no_human/cli/verifiers_cmd.py) is a plain `__slots__`
#: class, not a dataclass, so its fields cannot be read via
#: `dataclasses.fields`; its two prose-bearing attributes are named here.
#: `.file` and `.severity` are excluded: `.severity` is validated against a
#: known set before use, and `.file` is a repo-relative path, not free prose.
_FINDING_TEXT_ATTRS = {"label", "text"}

#: Not a repo dataclass field at all: raw child-process output
#: (`subprocess.run(...).stdout`/`.stderr`), and the free-text explanation
#: fields on the CI-gate/landing outcome objects returned by
#: `WakeWatcher._ci_gate_step` and `land_one` (both `Any`-typed at the call
#: site: `outcome.reason`, `outcome.web_url`, `result.gate_reason`,
#: `outcome["evidence"]`/`result.stderr` already covered by the process-output
#: pair). All of these can legitimately contain a git diff, a compiler
#: message, a URL, or free prose from a CI provider — exactly the shape this
#: bug is about, and not merely "an id that happens to share a field name."
_PROCESS_AND_GATE_TEXT_ATTRS = {"stdout", "stderr", "reason", "gate_reason", "web_url"}

EXTERNAL_ATTR_NAMES: set[str] = (
    TASK_TEXT_FIELDS | BLOCKER_TEXT_FIELDS | BLOCKER_OPTION_TEXT_FIELDS
    | CONTEXT_CHUNK_TEXT_FIELDS | GRILL_QUESTION_TEXT_FIELDS
    | _FINDING_TEXT_ATTRS | _PROCESS_AND_GATE_TEXT_ATTRS
)

#: DB/event row dict keys carrying the same class of text, measured across
#: `src/no_human/cli/*.py`: an event's free-text `text`, a memory/pattern/
#: task-ish row's `title`, an event's `actor`, a raw output `line`, a
#: step/landing `reason`, and a landing outcome's `evidence`.
EXTERNAL_SUBSCRIPT_KEYS = {"text", "title", "actor", "line", "reason", "evidence"}


def _markup_disabled(call: ast.Call) -> bool:
    """`console.print(x, markup=False)` never hands `x` to Rich's markup
    parser at all — a second, equally valid way of neutralising this bug
    class, already used at `task show`'s completion-event line (the actor is
    sanitised at the write end; this is defence in depth for the whole
    line). A call shaped this way is not an offender no matter what its
    positional args look like."""
    return any(
        kw.arg == "markup" and isinstance(kw.value, ast.Constant) and kw.value.value is False
        for kw in call.keywords
    )


def _is_protected(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _PROTECT_NAMES
    )


def _call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _is_external_shape(node: ast.AST | None, tainted: set[str]) -> bool:
    if node is None:
        return False
    if _is_protected(node):
        return False
    if isinstance(node, ast.Attribute):
        return node.attr in EXTERNAL_ATTR_NAMES
    if isinstance(node, ast.Name):
        return node.id in tainted
    if isinstance(node, ast.Subscript):
        s = node.slice
        if isinstance(s, ast.Constant) and isinstance(s.value, str):
            return s.value in EXTERNAL_SUBSCRIPT_KEYS
        # Non-string-key subscript (a slice, an index): transparent — the
        # externality, if any, lives in what is being sliced/indexed.
        return _is_external_shape(node.value, tainted)
    if isinstance(node, ast.BoolOp):
        return any(_is_external_shape(v, tainted) for v in node.values)
    if isinstance(node, ast.IfExp):
        return _is_external_shape(node.body, tainted) or _is_external_shape(node.orelse, tainted)
    if isinstance(node, ast.BinOp):
        return _is_external_shape(node.left, tainted) or _is_external_shape(node.right, tainted)
    if isinstance(node, ast.Tuple) or isinstance(node, ast.List):
        return any(_is_external_shape(e, tainted) for e in node.elts)
    if isinstance(node, ast.JoinedStr):
        return any(
            _is_external_shape(v.value, tainted)
            for v in node.values if isinstance(v, ast.FormattedValue)
        )
    if isinstance(node, ast.Call):
        name = _call_name(node)
        if name in _SAFE_INT_CALLS:
            return False
        if name in _TRANSPARENT_CALLS:
            return any(_is_external_shape(a, tainted) for a in node.args)
        # An unknown function/method call: transparent by default. Reformatting,
        # truncating, or slugifying external text does not make the result any
        # safer to hand to a markup parser, and this is the shape the bench
        # guard's own docstring names as a blind spot (`.format()`-style calls).
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
        return (
            any(_is_external_shape(a, tainted) for a in node.args)
            or _is_external_shape(receiver, tainted)
        )
    return False


def _target_names(target: ast.AST) -> list[str]:
    """Every `Name` bound by an assignment/for/comprehension target,
    including tuple/list unpacking (`for i, ac in enumerate(...)`,
    `a, b = pair`). A `Starred` element's inner name is included too."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for elt in target.elts:
            names.extend(_target_names(elt))
        return names
    return []


def _collect_taint(func: ast.AST) -> set[str]:
    """Names, anywhere in `func`, bound (by `=` or a `for`/comprehension
    target) from an externally-shaped value. Fixed-point over a handful of
    passes so `a = t.title; b = a` taints `b` too.

    A tuple/list target (`for i, ac in enumerate(t.acceptance_criteria, 1)`,
    `a, b = pair`) taints EVERY name it binds, not just a positionally-matched
    one: this guard does not model `enumerate`/`zip`/`dict.items()` shapes
    well enough to say which element of the pair carries the external value,
    and understating taint (leaving `ac` unprotected because only `i` was
    tracked) is the one direction this guard cannot afford to be wrong in.
    The cost is that `i` above must also be wrapped in `esc()`, which is
    harmless — `esc()` stringifies ints without raising and without altering
    their rendering.
    """
    tainted: set[str] = set()
    for _ in range(6):
        changed = False
        for node in ast.walk(func):
            pairs: list[tuple[str, ast.AST]] = []
            if isinstance(node, ast.Assign):
                value = node.value
                for target in node.targets:
                    for name in _target_names(target):
                        pairs.append((name, value))
            elif isinstance(node, (ast.For, ast.comprehension)):
                for name in _target_names(node.target):
                    pairs.append((name, node.iter))
            for name, value in pairs:
                if name not in tainted and _is_external_shape(value, tainted):
                    tainted.add(name)
                    changed = True
        if not changed:
            break
    return tainted


def _check_call_args(call: ast.Call, tainted: set[str], out: list[tuple[int, str]]) -> None:
    for arg in call.args:
        if isinstance(arg, ast.JoinedStr):
            for value in arg.values:
                if not isinstance(value, ast.FormattedValue):
                    continue
                root = value.value
                if not _is_protected(root) and _is_external_shape(root, tainted):
                    out.append((value.lineno, ast.unparse(root)))
        else:
            if not _is_protected(arg) and _is_external_shape(arg, tainted):
                out.append((arg.lineno, ast.unparse(arg)))


def find_unprotected_external_prints(path: str | Path) -> list[tuple[int, str]]:
    """Every offending root in every `print`/`rule`/`add_row` call in `path`.

    Returns sorted, de-duplicated `(lineno, unparsed-root-expression)` pairs.
    """
    return _scan(Path(path).read_text())


def _scan(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    out: list[tuple[int, str]] = []
    func_stack: list[ast.AST] = []
    taint_cache: dict[int, set[str]] = {}

    class Visitor(ast.NodeVisitor):
        def _visit_func(self, node: ast.AST) -> None:
            func_stack.append(node)
            self.generic_visit(node)
            func_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_func(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_func(node)

        def visit_Call(self, node: ast.Call) -> None:
            if _call_name(node) in PRINT_LIKE_ATTRS and not _markup_disabled(node):
                func = func_stack[-1] if func_stack else None
                if func is not None:
                    tainted = taint_cache.setdefault(id(func), None)
                    if tainted is None:
                        tainted = _collect_taint(func)
                        taint_cache[id(func)] = tainted
                else:
                    tainted = set()
                _check_call_args(node, tainted, out)
            self.generic_visit(node)

    Visitor().visit(tree)
    return sorted(set(out))
