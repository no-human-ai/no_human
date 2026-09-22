"""AST engine behind `tests/test_no_wallclock_assertions.py`.

Separated from the test file so the detector is independently unit-testable
(see that file's inline-source self-tests) and so pytest does not try to
collect this module itself (it does not match `test_*.py`).

WHAT THIS DETECTS: `find_wallclock_assertions(source, path)` walks a test
module and flags every `assert <clock-derived expr> <cmp> <literal>` (or the
mirrored `<literal> <cmp> <clock-derived expr>`), where `<cmp>` is one of
`<`, `<=`, `>`, `>=`. A "clock-derived expr" is anything whose taint traces
back to a call that reads a wall clock: `time.monotonic()`, `.perf_counter()`,
`.time()` (covers `time.time()`, `loop.time()`,
`asyncio.get_event_loop().time()`), `.monotonic_ns()`, `.perf_counter_ns()`,
`.process_time()`, or `.now()` / `.utcnow()` (covers `datetime.now()` /
`datetime.utcnow()` deltas). Matching is on ATTRIBUTE NAME plus an
import-alias map for the bare-name form (`from time import monotonic as _m`),
never on a hard-coded base-object name — a file can do `import time as
_time` or alias the function itself and the taint still seeds.

Taint propagates through: binary operators (`* 1000`, catching
`elapsed_ms`), a method call on a tainted receiver (`delta.total_seconds()`),
a call with a tainted argument, subscripting a name known to be a tainted
LIST (built by `<name>.append(<tainted>)`), and `max()` / `min()` / `sum()`
over such a list — this is what catches `max(gaps) < 0.25`.

A "literal" opposing operand is a numeric constant, a unary-minus/plus
constant, a compile-time-constant arithmetic expression built only from
those (`3 * 0.1`), or a bare name bound at MODULE scope to one of those
(`elapsed < _LOCK_BUDGET_SECONDS`).

DELIBERATELY NOT FLAGGED (see module docstring cross-references in
`tests/test_no_wallclock_assertions.py` for the reasoning):

  * tainted-vs-tainted comparisons (`windows[1] < windows[0] * 0.75`) — the
    opposing side is not a literal shape, so it is never flagged regardless
    of taint. Load-relative comparisons between two measurements taken in
    the same run are legitimate and are not this defect.
  * a comparison against a `calibrated_budget(...)` call result — a Call is
    never a "literal" opposing operand, so this is excluded by construction,
    not by name-listing the helper.
  * `timeout=`/`wait_for(...)` keyword arguments — these are not the operand
    of an `ast.Compare` at all (they are call arguments), so they are never
    visited by the assert-inspection pass.
  * the `must_not_hang(..., budget=N)` watchdog argument — same reason.

BLIND SPOTS (required reading before trusting a clean scan — AC4 asks the
sweep to name these, not merely to know they exist):

  * interprocedural taint: a duration measured and returned by a helper
    function, then compared in the caller, is invisible — this walks one
    function body at a time and does not track return values across calls.
  * taint through a container store other than a bare `list.append`: a dict
    key (`d["t"] = elapsed`) or an attribute (`self.t = elapsed`) does not
    propagate.
  * a clock rebound to a bare local (`clock = time.monotonic; clock()`) —
    the alias map only understands `from time import monotonic as _m` at
    IMPORT time, not a later local rebinding of the imported name.
  * an env-derived or otherwise dynamically computed bound
    (`float(os.environ["BUDGET"])`) is not a "literal" by this detector's
    definition, so a test could smuggle a fixed number through an
    environment variable and stay unflagged.
  * a test that sleeps and races a background effect with no `assert` node
    to inspect at all (the property is checked by a side channel, e.g. a
    mock call count polled after a fixed sleep) — there is nothing here for
    an AST walk over `ast.Assert` to see.
  * non-Python lanes (`web/`, `desktop/`, `e2e/`) are out of scope for this
    module entirely; it only ever receives `tests/*.py` sources.
  * `pytest-timeout` (`@pytest.mark.timeout(...)`) is a distinct mechanism
    (a plugin-enforced wall-clock kill, not an in-test assertion) and is
    verified ABSENT from this repo's dependencies and test markers rather
    than detected here.

Performance: this is a pure per-file AST walk with no I/O beyond the caller
handing it already-read source text, so scanning every file under `tests/`
comfortably finishes in well under a second.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass

CLOCK_ATTR_NAMES = {
    "monotonic",
    "perf_counter",
    "time",
    "monotonic_ns",
    "perf_counter_ns",
    "process_time",
    "now",
    "utcnow",
}

_ARITH_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_COMPARE_OPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)


@dataclass(frozen=True)
class Finding:
    path: str
    lineno: int
    expr_src: str
    bound: str

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}: {self.expr_src} (bound {self.bound})"


def _pure_literal_value(node: ast.AST):
    """Evaluate `node` IF it is built entirely from numeric constants and
    +/-/*///%/** — no names, no calls. Returns None (a valid sentinel, since
    a legitimate literal value could also coincide with it only as a float
    which is handled by isinstance checks) when the node is not such an
    expression.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        inner = _pure_literal_value(node.operand)
        if inner is None:
            return None
        return -inner if isinstance(node.op, ast.USub) else inner
    if isinstance(node, ast.BinOp) and isinstance(node.op, _ARITH_OPS):
        left = _pure_literal_value(node.left)
        right = _pure_literal_value(node.right)
        if left is None or right is None:
            return None
        try:
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.FloorDiv):
                return left // right
            if isinstance(node.op, ast.Mod):
                return left % right
            if isinstance(node.op, ast.Pow):
                return left ** right
        except ZeroDivisionError:
            return None
    return None


def _build_bare_clock_aliases(tree: ast.Module) -> set[str]:
    """Names bound via `from time import monotonic as _m` (or unaliased) —
    callable bare, without any attribute access, as a clock read.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in CLOCK_ATTR_NAMES:
                    aliases.add(alias.asname or alias.name)
    return aliases


def _module_numeric_constants(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if _pure_literal_value(node.value) is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if _pure_literal_value(node.value) is None:
                continue
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def _is_clock_call(node: ast.Call, bare_aliases: set[str]) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in CLOCK_ATTR_NAMES:
        return True
    if isinstance(func, ast.Name) and func.id in bare_aliases:
        return True
    return False


class _FuncScope:
    """Sequential, forward-only taint pass over one function body. Not a
    real control-flow graph — branches are visited in textual order and
    taint accumulates across them, which over-approximates (a name tainted
    only on one branch is treated as tainted after the `if`) rather than
    under-approximates, matching this guard's bias toward false positives
    over silently missing a real site.
    """

    def __init__(self, bare_aliases: set[str], module_constants: set[str], path: str):
        self.bare_aliases = bare_aliases
        self.module_constants = module_constants
        self.path = path
        self.tainted: set[str] = set()
        self.tainted_lists: set[str] = set()
        self.findings: list[Finding] = []

    # -- expression taint -------------------------------------------------
    def expr_tainted(self, node: ast.AST | None) -> bool:
        if node is None:
            return False
        if isinstance(node, ast.Name):
            return node.id in self.tainted
        if isinstance(node, ast.Call):
            return self.call_tainted(node)
        if isinstance(node, ast.BinOp):
            return self.expr_tainted(node.left) or self.expr_tainted(node.right)
        if isinstance(node, ast.UnaryOp):
            return self.expr_tainted(node.operand)
        if isinstance(node, ast.Subscript):
            base = node.value
            if isinstance(base, ast.Name) and base.id in self.tainted_lists:
                return True
            return self.expr_tainted(base)
        if isinstance(node, ast.Attribute):
            return self.expr_tainted(node.value)
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return any(self.expr_tainted(e) for e in node.elts)
        if isinstance(node, ast.IfExp):
            return self.expr_tainted(node.body) or self.expr_tainted(node.orelse)
        if isinstance(node, ast.NamedExpr):
            return self.expr_tainted(node.value)
        return False

    def call_tainted(self, node: ast.Call) -> bool:
        if _is_clock_call(node, self.bare_aliases):
            return True
        func = node.func
        if isinstance(func, ast.Attribute) and self.expr_tainted(func.value):
            return True
        if isinstance(func, ast.Name) and func.id in {"max", "min", "sum"} and node.args:
            arg0 = node.args[0]
            if isinstance(arg0, ast.Name) and arg0.id in self.tainted_lists:
                return True
        for arg in node.args:
            if self.expr_tainted(arg):
                return True
        for kw in node.keywords:
            if kw.value is not None and self.expr_tainted(kw.value):
                return True
        return False

    # -- literal-bound recognition -----------------------------------------
    def is_literal_bound(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name) and node.id in self.module_constants:
            return True
        return _pure_literal_value(node) is not None

    def bound_repr(self, node: ast.AST) -> str:
        try:
            return ast.unparse(node)
        except Exception:
            return "<?>"

    # -- statement walk -----------------------------------------------------
    def visit_stmts(self, stmts) -> None:
        for stmt in stmts:
            self.visit_stmt(stmt)

    def visit_stmt(self, node: ast.AST) -> None:
        if isinstance(node, ast.Assign):
            tainted = self.expr_tainted(node.value)
            for target in node.targets:
                self._bind_target(target, tainted)
        elif isinstance(node, ast.AnnAssign):
            tainted = self.expr_tainted(node.value) if node.value is not None else False
            self._bind_target(node.target, tainted)
        elif isinstance(node, ast.AugAssign):
            tainted = self.expr_tainted(node.value)
            if isinstance(node.target, ast.Name) and node.target.id in self.tainted:
                tainted = True
            self._bind_target(node.target, tainted)
        elif isinstance(node, ast.Expr):
            self._maybe_record_append(node.value)
        elif isinstance(node, ast.Assert):
            self._check_assert(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nested = _FuncScope(self.bare_aliases, self.module_constants, self.path)
            nested.tainted |= self.tainted
            nested.tainted_lists |= self.tainted_lists
            nested.visit_stmts(node.body)
            self.findings.extend(nested.findings)
        elif isinstance(node, ast.If):
            self.visit_stmts(node.body)
            self.visit_stmts(node.orelse)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            self.visit_stmts(node.body)
            self.visit_stmts(node.orelse)
        elif isinstance(node, ast.While):
            self.visit_stmts(node.body)
            self.visit_stmts(node.orelse)
        elif isinstance(node, ast.Try):
            self.visit_stmts(node.body)
            for handler in node.handlers:
                self.visit_stmts(handler.body)
            self.visit_stmts(node.orelse)
            self.visit_stmts(node.finalbody)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            self.visit_stmts(node.body)

    def _bind_target(self, target: ast.AST, tainted: bool) -> None:
        if isinstance(target, ast.Name):
            if tainted:
                self.tainted.add(target.id)
            else:
                self.tainted.discard(target.id)
                self.tainted_lists.discard(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._bind_target(elt, tainted)

    def _maybe_record_append(self, value: ast.AST) -> None:
        if not isinstance(value, ast.Call):
            return
        func = value.func
        if isinstance(func, ast.Attribute) and func.attr == "append" \
                and isinstance(func.value, ast.Name) and value.args:
            if self.expr_tainted(value.args[0]):
                self.tainted_lists.add(func.value.id)

    def _check_assert(self, node: ast.Assert) -> None:
        self._check_test_expr(node, node.test)

    def _check_test_expr(self, assert_node: ast.Assert, expr: ast.AST) -> None:
        if isinstance(expr, ast.BoolOp):
            for value in expr.values:
                self._check_test_expr(assert_node, value)
            return
        if not isinstance(expr, ast.Compare):
            return
        operands = [expr.left, *expr.comparators]
        for i, op in enumerate(expr.ops):
            if not isinstance(op, _COMPARE_OPS):
                continue
            left, right = operands[i], operands[i + 1]
            left_tainted = self.expr_tainted(left)
            right_tainted = self.expr_tainted(right)
            if left_tainted and not right_tainted and self.is_literal_bound(right):
                self._record(assert_node, right)
            elif right_tainted and not left_tainted and self.is_literal_bound(left):
                self._record(assert_node, left)

    def _record(self, assert_node: ast.Assert, bound_node: ast.AST) -> None:
        try:
            expr_src = ast.unparse(assert_node.test)
        except Exception:
            expr_src = "<?>"
        self.findings.append(Finding(
            path=self.path,
            lineno=assert_node.lineno,
            expr_src=expr_src,
            bound=self.bound_repr(bound_node),
        ))


def find_wallclock_assertions(source: str, path: str) -> list[Finding]:
    """Return every wall-clock-vs-literal assertion found in `source`
    (already-read text of the file at `path`, used only for labeling
    findings). Returns `[]` on a syntax error rather than raising, so a
    guard scanning many files degrades to skipping the unparsable one.
    """
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return []

    bare_aliases = _build_bare_clock_aliases(tree)
    module_constants = _module_numeric_constants(tree)
    findings: list[Finding] = []

    def scan(stmts) -> None:
        for node in stmts:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = _FuncScope(bare_aliases, module_constants, path)
                scope.visit_stmts(node.body)
                findings.extend(scope.findings)
            elif isinstance(node, ast.ClassDef):
                scan(node.body)
            elif isinstance(node, ast.If):
                scan(node.body)
                scan(node.orelse)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                scan(node.body)
                scan(node.orelse)
            elif isinstance(node, ast.Try):
                scan(node.body)
                for handler in node.handlers:
                    scan(handler.body)
                scan(node.orelse)
                scan(node.finalbody)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                scan(node.body)

    scan(tree.body)
    return findings
