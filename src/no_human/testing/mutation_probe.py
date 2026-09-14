"""Mutation probe: does a new/changed test actually pin the behaviour it is
named for, or does it stay green no matter what the code under test does?

For every test a diff adds or changes, this module infers the code under
test, applies exactly ONE executable-code mutation to it, re-runs only that
one test, and requires the test to FAIL ("killed"). A test that stays green
under mutation ("survived") is Class-1 evidence the test asserts nothing —
reported so the reviewer can block on it, never inferred silently.

Named ceilings, stated because this is easy to over-claim:

* PYTHON/PYTEST ONLY. A changed test file in any other language is reported
  ``undetermined`` — this module never silently skips it.
* STATIC MAPPING ONLY. The code under test is inferred from the test's
  top-level imports and the first-party calls made in its body (with a
  naming-pattern tiebreak), never executed to find out. A test this cannot
  map statically is ``undetermined``, never guessed at.
* ONE MUTATION AT A TIME. Exactly one executable AST node changes per probe
  run — never a string or comment edit, checked mechanically against the
  token stream, not by convention.
* NEVER WRITES TO THE TREE IT WAS GIVEN. Every mutation happens inside a
  disposable ``git worktree add --detach`` copy; ``repo_path`` is opened
  read-only (``git diff``/``show``/``ls-tree``) throughout, and the final
  proof that it is unchanged is a content-hash comparison
  (:func:`no_human.core.reviewer_worktree.snapshot`/``compare``) — never a
  ``git checkout``/``reset``/``stash``/``clean``.

This module never raises: every failure path returns a
:class:`MutationProbeResult` with ``verdict="error"`` and a reason, or a
per-test :class:`TestProbe` with ``verdict="undetermined"`` and a reason —
"could not check" is always reported, never read as a pass.
"""

from __future__ import annotations

import ast
import hashlib
import io
import logging
import os
import shutil
import subprocess
import tempfile
import time
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

from ..core import reviewer_worktree
from .repro_gate import _nothing_executed, _pytest_python, _run_pytest_proc
from .runner import _env_for
from .tamper_guard import is_test_file

log = logging.getLogger(__name__)

DEFAULT_MAX_TESTS = 12
DEFAULT_MAX_MUTATIONS_PER_TEST = 3
DEFAULT_TIMEOUT_SECONDS = 300
#: Timeout for the local git plumbing this module issues (diff/show/ls-tree/
#: worktree add/remove) — not the pytest runs themselves, which are bounded
#: by `repro_gate._RUN_TIMEOUT` inside `_run_pytest_proc`.
_GIT_TIMEOUT = 30.0


@dataclass
class TestProbe:
    """The verdict for one test the diff added or changed."""

    node_id: str
    verdict: str  # "killed" | "survived" | "undetermined"
    target: str = ""  # "src/pkg/rates.py:volumetric"
    mutation: str = ""  # "line 41: `if x:` -> `if not (x):`"
    reason: str = ""  # why undetermined, or the failure tail proving the kill


@dataclass
class MutationProbeResult:
    """The whole probe's outcome for one review."""

    verdict: str = "pass"  # "pass" | "fail" | "error" | "skipped"
    probes: list[TestProbe] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    tree_intact: bool = False


# --------------------------------------------------------------------------
# Step 1: which tests did the diff add or change?
# --------------------------------------------------------------------------


def _show(repo_path: Path, ref: str, rel: str, timeout: float) -> str | None:
    """Text of ``rel`` at ``ref``, or None (absent, binary, unreadable)."""
    proc = subprocess.run(
        ["git", "show", f"{ref}:{rel}"],
        cwd=repo_path, capture_output=True, timeout=timeout,
    )
    if proc.returncode != 0 or b"\x00" in proc.stdout:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def _ls_tree(repo_path: Path, ref: str, timeout: float) -> set[str]:
    """All tracked paths at ``ref``, or an empty set on any git failure."""
    try:
        proc = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", ref],
            cwd=repo_path, capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return set()
    if proc.returncode != 0:
        return set()
    return set((proc.stdout or "").splitlines())


def _test_functions(tree: ast.Module) -> dict[str, ast.stmt]:
    """``{name: FunctionDef}`` for top-level ``test_*`` functions and
    one-level-nested ``test_*`` methods, keyed like a pytest node id suffix
    (``name`` or ``Class::name``)."""
    out: dict[str, ast.stmt] = {}
    for node in tree.body:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")):
            out[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if (isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and sub.name.startswith("test_")):
                    out[f"{node.name}::{sub.name}"] = sub
    return out


def changed_test_functions(
    repo_path: Path, before_ref: str, after_ref: str, *, timeout: float = _GIT_TIMEOUT,
) -> tuple[list[dict], list[TestProbe]]:
    """Test functions the diff adds or changes.

    Returns ``(candidates, pre_probes)``: ``candidates`` are Python test
    functions ready for mapping/mutation, each a dict with ``node_id``,
    ``rel``, ``module_ast`` (the after-ref AST) and ``test_fn`` (the
    function's AST node). ``pre_probes`` are :class:`TestProbe` for changed
    test files this function already knows cannot be probed (non-Python).

    Raises on a git failure — the caller turns that into a result-level
    ``error``, since "the diff could not even be read" is not a per-test
    fact.
    """
    proc = subprocess.run(
        ["git", "diff", "--name-status", "-M", f"{before_ref}..{after_ref}"],
        cwd=repo_path, capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git diff failed: {proc.stderr.strip()[:300]}")

    changed: list[dict] = []
    pre_probes: list[TestProbe] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or parts[0].startswith("D"):
            continue
        rel = parts[-1]
        if not is_test_file(rel):
            continue
        if not rel.endswith(".py"):
            pre_probes.append(TestProbe(
                node_id=rel, verdict="undetermined",
                reason="non-Python test file — this probe is pytest-only",
            ))
            continue
        after_text = _show(repo_path, after_ref, rel, timeout)
        if after_text is None:
            continue  # deleted or binary at after-ref — nothing to probe
        before_text = _show(repo_path, before_ref, rel, timeout)
        try:
            after_tree = ast.parse(after_text)
        except SyntaxError:
            continue
        before_tree = None
        if before_text is not None:
            try:
                before_tree = ast.parse(before_text)
            except SyntaxError:
                before_tree = None
        after_fns = _test_functions(after_tree)
        before_fns = _test_functions(before_tree) if before_tree is not None else {}
        for key in sorted(after_fns):
            node = after_fns[key]
            if key in before_fns:
                seg_after = ast.get_source_segment(after_text, node)
                seg_before = ast.get_source_segment(before_text, before_fns[key])
                if seg_after == seg_before:
                    continue
            changed.append({
                "node_id": f"{rel}::{key}",
                "rel": rel,
                "module_ast": after_tree,
                "test_fn": node,
            })
    changed.sort(key=lambda c: c["node_id"])
    return changed, pre_probes


# --------------------------------------------------------------------------
# Step 2: which code is this test actually testing?
# --------------------------------------------------------------------------


def _first_party_imports(module_ast: ast.Module) -> dict[str, tuple[str, str]]:
    """``{local_name: (module_dotted, orig_name)}`` from top-level imports.

    Relative imports (``from . import x``) are not resolved statically here
    — their dotted path depends on the importing package, which this module
    does not walk — so they simply contribute no candidate."""
    imports: dict[str, tuple[str, str]] = {}
    for node in module_ast.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                imports[local] = (alias.name, alias.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom):
            if node.module is None or node.level:
                continue
            for alias in node.names:
                local = alias.asname or alias.name
                imports[local] = (node.module, alias.name)
    return imports


def _root_name(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _called_symbols(
    test_fn: ast.stmt, imports: dict[str, tuple[str, str]],
) -> dict[tuple[str, str], int]:
    """``{(module_dotted, symbol): call_count}`` for first-party calls made
    in the test's body."""
    counts: dict[tuple[str, str], int] = {}
    for node in ast.walk(test_fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id not in imports:
                continue
            mod_dotted, orig_name = imports[func.id]
            key = (mod_dotted, orig_name)
        elif isinstance(func, ast.Attribute):
            root = _root_name(func)
            if root is None or root not in imports:
                continue
            mod_dotted, _ = imports[root]
            key = (mod_dotted, func.attr)
        else:
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


def _resolve_module_path(mod_dotted: str, tree_files: set[str]) -> str | None:
    """Repo-relative path for ``mod_dotted`` at the after-ref, if tracked."""
    base = mod_dotted.replace(".", "/")
    for candidate in (
        f"{base}.py", f"{base}/__init__.py",
        f"src/{base}.py", f"src/{base}/__init__.py",
    ):
        if candidate in tree_files:
            return candidate
    return None


def _rank_candidates(
    test_name: str, candidates: list[tuple[str, str, int]],
) -> tuple[str, str]:
    """Pick one ``(target_path, symbol)`` from resolved candidates.

    Ranked by: (1) the symbol's tokens are a prefix of the test name's
    tokens (``test_volumetric_rounds_up`` names ``volumetric`` before
    ``rounds``/``up``) — longest such symbol wins; (2) highest call count;
    (3) lexicographic, for determinism."""
    stripped = test_name.removeprefix("test_")
    test_tokens = stripped.split("_")

    def is_prefix_match(symbol: str) -> bool:
        sym_tokens = symbol.split("_")
        return test_tokens[:len(sym_tokens)] == sym_tokens

    naming_matches = [c for c in candidates if is_prefix_match(c[1])]
    if naming_matches:
        naming_matches.sort(key=lambda c: (-len(c[1].split("_")), c[0], c[1]))
        best = naming_matches[0]
        return best[0], best[1]
    ranked = sorted(candidates, key=lambda c: (-c[2], c[0], c[1]))
    best = ranked[0]
    return best[0], best[1]


def _infer_target(
    test_fn: ast.stmt, module_ast: ast.Module, tree_files: set[str],
) -> tuple[str, str] | None:
    """``(target_path, symbol)`` this test statically appears to exercise,
    or None when no first-party symbol imported and called resolves."""
    imports = _first_party_imports(module_ast)
    if not imports:
        return None
    counts = _called_symbols(test_fn, imports)
    if not counts:
        return None
    candidates = []
    for (mod_dotted, symbol), count in counts.items():
        path = _resolve_module_path(mod_dotted, tree_files)
        if path is None:
            continue
        candidates.append((path, symbol, count))
    if not candidates:
        return None
    name = getattr(test_fn, "name", "")
    return _rank_candidates(name, candidates)


# --------------------------------------------------------------------------
# Step 3: generate and apply one executable-node mutation at a time.
# --------------------------------------------------------------------------


@dataclass
class _Mutation:
    lineno: int
    col_offset: int
    end_lineno: int
    end_col_offset: int
    new_text: str
    description: str


def _find_symbol(tree: ast.Module, symbol: str) -> ast.AST | None:
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == symbol):
            return node
    return None


def _statement_bodies(node: ast.AST) -> list[list[ast.stmt]]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return [node.body]
    if isinstance(node, ast.ClassDef):
        return [
            sub.body for sub in node.body
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
    return []


def _is_declaration_or_docstring(stmt: ast.stmt) -> bool:
    if isinstance(stmt, (
        ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
        ast.ClassDef, ast.Global, ast.Nonlocal,
    )):
        return True
    return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str))  # bare string (docstring-shaped)


def _mutations_for(source_text: str, symbol: str) -> list[_Mutation]:
    """Ordered, capped candidate mutations for ``symbol`` in ``source_text``.

    Order: (1) boolean condition flips on ``if``/``while``, (2) return-value
    negations, (3) executable-statement removal (replaced with ``pass``,
    never a declaration/import/docstring, and never the sole statement in a
    body — that would prefer removal over the flip/negation candidates
    instead of just adding to them)."""
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return []
    target = _find_symbol(tree, symbol)
    if target is None:
        return []

    candidates: list[_Mutation] = []
    for node in ast.walk(target):
        if isinstance(node, (ast.If, ast.While)):
            seg = ast.get_source_segment(source_text, node.test)
            if not seg:
                continue
            candidates.append(_Mutation(
                node.test.lineno, node.test.col_offset,
                node.test.end_lineno, node.test.end_col_offset,
                f"not ({seg})",
                f"line {node.test.lineno}: flip `{seg}` -> `not ({seg})`",
            ))
    for node in ast.walk(target):
        if isinstance(node, ast.Return) and node.value is not None:
            seg = ast.get_source_segment(source_text, node.value)
            if not seg:
                continue
            candidates.append(_Mutation(
                node.value.lineno, node.value.col_offset,
                node.value.end_lineno, node.value.end_col_offset,
                f"not ({seg})",
                f"line {node.value.lineno}: negate return `{seg}` -> `not ({seg})`",
            ))
    for body in _statement_bodies(target):
        if len(body) <= 1:
            continue
        for stmt in body:
            if _is_declaration_or_docstring(stmt):
                continue
            seg = ast.get_source_segment(source_text, stmt)
            if not seg:
                continue
            candidates.append(_Mutation(
                stmt.lineno, stmt.col_offset, stmt.end_lineno, stmt.end_col_offset,
                "pass",
                f"line {stmt.lineno}: remove statement `{seg.splitlines()[0]}` -> `pass`",
            ))
    return candidates


def _pos_to_offset(text: str, lineno: int, col: int) -> int:
    lines = text.splitlines(keepends=True)
    return sum(len(ln) for ln in lines[:lineno - 1]) + col


def _overlaps_string_or_comment(text: str, start: int, end: int) -> bool:
    """True when [start, end) overlaps a STRING/COMMENT token — or when the
    token stream cannot even be built, which fails CLOSED (rejects the
    mutation) rather than risk an unproven edit."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenizeError, SyntaxError, IndentationError, ValueError):
        return True
    for tok in tokens:
        if tok.type not in (tokenize.STRING, tokenize.COMMENT):
            continue
        tok_start = _pos_to_offset(text, *tok.start)
        tok_end = _pos_to_offset(text, *tok.end)
        if not (end <= tok_start or start >= tok_end):
            return True
    return False


def _apply_one(original_text: str, mutation: _Mutation) -> str | None:
    """The mutated text, or None when the mutation cannot be trusted:
    an empty/inverted range, a no-op edit, an edit overlapping a
    string/comment token, a syntax break, or an edit that leaves the AST
    unchanged (``ast.dump`` before/after identical)."""
    start = _pos_to_offset(original_text, mutation.lineno, mutation.col_offset)
    end = _pos_to_offset(original_text, mutation.end_lineno, mutation.end_col_offset)
    if end <= start:
        return None
    old_segment = original_text[start:end]
    if old_segment == mutation.new_text:
        return None
    if _overlaps_string_or_comment(original_text, start, end):
        return None
    mutated = original_text[:start] + mutation.new_text + original_text[end:]
    try:
        mutated_tree = ast.parse(mutated)
        original_tree = ast.parse(original_text)
    except SyntaxError:
        return None
    if ast.dump(mutated_tree) == ast.dump(original_tree):
        return None
    return mutated


# --------------------------------------------------------------------------
# Step 4: run the probe.
# --------------------------------------------------------------------------


def _finalize(
    repo_path: Path, before_snap: reviewer_worktree.Snapshot, timeout: float,
    result: MutationProbeResult,
) -> MutationProbeResult:
    """AC3: the final proof the reviewed tree is unchanged — a content-hash
    comparison, never a revert. A dirty tree forces the whole result to
    ``error`` and discards any ``killed`` verdict already recorded (it was
    reached on a run that has now proven it did not restore what it wrote)."""
    try:
        delta = reviewer_worktree.compare(repo_path, before_snap, timeout=timeout)
    except reviewer_worktree.WorktreeCheckFailed as exc:
        result.verdict = "error"
        result.tree_intact = False
        result.reasons.append(f"could not verify the tree was left intact: {exc}")
        return result
    if not delta.is_empty():
        result.verdict = "error"
        result.tree_intact = False
        changed_paths = sorted(set(delta.added) | set(delta.modified) | set(delta.deleted))
        result.reasons.append(
            "the reviewed tree was not byte-identical after the mutation "
            f"probe — changed paths: {changed_paths[:20]}"
        )
        for probe in result.probes:
            if probe.verdict == "killed":
                probe.verdict = "undetermined"
                probe.reason = (
                    "discarded — the tree-integrity check failed after "
                    "this probe ran"
                )
        return result
    result.tree_intact = True
    return result


def _probe_one(
    item: dict, tree_files: set[str], worktree: Path, env: dict, python: str,
    max_mutations: int,
) -> TestProbe:
    node_id = item["node_id"]
    target = _infer_target(item["test_fn"], item["module_ast"], tree_files)
    if target is None:
        return TestProbe(
            node_id=node_id, verdict="undetermined",
            reason=("could not determine the code under test statically "
                    "(no first-party symbol imported and called)"),
        )
    target_rel, symbol = target
    target_label = f"{target_rel}:{symbol}"
    target_path = worktree / target_rel

    rc, out = _run_pytest_proc([node_id], worktree, env, python)
    if rc is None:
        return TestProbe(
            node_id=node_id, verdict="undetermined", target=target_label,
            reason=f"the test could not be run in the probe copy: {out[-500:]}",
        )
    why = _nothing_executed(rc, out)
    if why is not None:
        return TestProbe(
            node_id=node_id, verdict="undetermined", target=target_label,
            reason=f"the test does not run cleanly unmutated in the probe copy — {why}",
        )
    if rc != 0:
        return TestProbe(
            node_id=node_id, verdict="undetermined", target=target_label,
            reason=f"the test does not pass unmutated in the probe copy:\n{out[-500:]}",
        )

    try:
        original_text = target_path.read_text()
    except OSError as exc:
        return TestProbe(
            node_id=node_id, verdict="undetermined", target=target_label,
            reason=f"could not read {target_rel} in the probe copy: {exc}",
        )
    original_hash = hashlib.sha256(original_text.encode()).hexdigest()

    tried = 0
    tried_descriptions: list[str] = []
    for mutation in _mutations_for(original_text, symbol):
        if tried >= max_mutations:
            break
        mutated_text = _apply_one(original_text, mutation)
        if mutated_text is None:
            continue
        tried += 1
        tried_descriptions.append(mutation.description)
        try:
            target_path.write_text(mutated_text)
            mrc, mout = _run_pytest_proc([node_id], worktree, env, python)
        finally:
            target_path.write_text(original_text)
            restored_hash = hashlib.sha256(target_path.read_text().encode()).hexdigest()
            if restored_hash != original_hash:
                raise RuntimeError(
                    f"failed to restore {target_rel} after mutation in the probe copy"
                )
        if mrc is None:
            continue  # environment failure on this candidate — try the next
        if _nothing_executed(mrc, mout) is not None:
            continue  # broken collection under mutation is not a kill
        if mrc != 0:
            return TestProbe(
                node_id=node_id, verdict="killed", target=target_label,
                mutation=mutation.description, reason=mout[-1000:],
            )

    if tried == 0:
        return TestProbe(
            node_id=node_id, verdict="undetermined", target=target_label,
            reason=f"no executable mutation could be generated for {target_label}",
        )
    return TestProbe(
        node_id=node_id, verdict="survived", target=target_label,
        mutation="; ".join(tried_descriptions),
        reason="the test stayed green under every generated mutation",
    )


def run_mutation_probe(
    repo_path: Path, before_ref: str, after_ref: str, *,
    max_tests: int = DEFAULT_MAX_TESTS,
    max_mutations: int = DEFAULT_MAX_MUTATIONS_PER_TEST,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    worktree_timeout: float = _GIT_TIMEOUT,
) -> MutationProbeResult:
    """Mutate the code under test for every test the diff adds or changes,
    inside a disposable worktree copy, and require each to fail.

    Never raises — every failure path returns ``verdict="error"`` with a
    reason. ``repo_path`` is never written to; see the module docstring for
    the isolation and integrity-proof discipline this follows."""
    repo_path = Path(repo_path)
    deadline = time.monotonic() + timeout
    try:
        before_snap = reviewer_worktree.snapshot(repo_path, timeout=worktree_timeout)
    except reviewer_worktree.WorktreeCheckFailed as exc:
        return MutationProbeResult(
            verdict="error",
            reasons=[f"could not snapshot the tree before probing: {exc}"],
        )

    result = MutationProbeResult(verdict="pass")
    try:
        changed_tests, pre_probes = changed_test_functions(
            repo_path, before_ref, after_ref, timeout=worktree_timeout)
    except Exception as exc:  # noqa: BLE001 — this module never raises
        result.verdict = "error"
        result.reasons.append(f"could not determine which tests the diff changed: {exc}")
        return _finalize(repo_path, before_snap, worktree_timeout, result)

    result.probes.extend(pre_probes)
    if not changed_tests and not pre_probes:
        result.verdict = "skipped"
        result.reasons.append("the diff changes no test file")
        return _finalize(repo_path, before_snap, worktree_timeout, result)

    if not changed_tests:
        # Only non-Python test files changed — nothing left to build a
        # worktree for.
        return _finalize(repo_path, before_snap, worktree_timeout, result)

    python = _pytest_python(repo_path)
    if python is None:
        result.verdict = "error"
        result.reasons.append("no Python interpreter available to run the mutation probe")
        return _finalize(repo_path, before_snap, worktree_timeout, result)

    to_probe = changed_tests[:max_tests]
    overflow = changed_tests[max_tests:]
    for item in overflow:
        result.probes.append(TestProbe(
            node_id=item["node_id"], verdict="undetermined",
            reason=(f"probe budget exhausted ({len(changed_tests)} tests "
                    f"changed, {max_tests} probed)"),
        ))

    tree_files = _ls_tree(repo_path, after_ref, worktree_timeout)

    try:
        tmp = Path(tempfile.mkdtemp(prefix="nh-mutation-probe-"))
    except OSError as exc:
        result.verdict = "error"
        result.reasons.append(f"could not create a scratch directory: {exc}")
        return _finalize(repo_path, before_snap, worktree_timeout, result)

    worktree = tmp / "probe"
    try:
        added = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), after_ref],
            cwd=repo_path, capture_output=True, text=True, timeout=worktree_timeout,
        )
        if added.returncode != 0:
            result.verdict = "error"
            result.reasons.append(
                f"could not build the disposable probe worktree at {after_ref}: "
                f"{added.stderr.strip()[:300]}"
            )
            for item in to_probe:
                result.probes.append(TestProbe(
                    node_id=item["node_id"], verdict="undetermined",
                    reason="the disposable probe worktree could not be built",
                ))
            return _finalize(repo_path, before_snap, worktree_timeout, result)

        env = _env_for(repo_path)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(worktree), str(worktree / "src"), env.get("PYTHONPATH", "")])

        try:
            for item in to_probe:
                if time.monotonic() > deadline:
                    result.probes.append(TestProbe(
                        node_id=item["node_id"], verdict="undetermined",
                        reason="mutation probe wall-clock budget exhausted",
                    ))
                    continue
                result.probes.append(
                    _probe_one(item, tree_files, worktree, env, python, max_mutations))
        except Exception as exc:  # noqa: BLE001 — this module never raises
            result.verdict = "error"
            result.reasons.append(f"mutation probe crashed while probing: {exc}")
            return _finalize(repo_path, before_snap, worktree_timeout, result)
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=repo_path, capture_output=True, timeout=worktree_timeout,
        )
        shutil.rmtree(tmp, ignore_errors=True)

    if any(p.verdict == "survived" for p in result.probes):
        result.verdict = "fail"
    return _finalize(repo_path, before_snap, worktree_timeout, result)
