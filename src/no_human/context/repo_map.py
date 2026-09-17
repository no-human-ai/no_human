"""A compact repo map seeded into the coder's prompt (M3, cli-agent-style).

The coder burns most of its tokens exploring: Glob → Read → Grep, each result
landing in the transcript forever. A ~3K-token map of the repo — directories,
files, and each Python file's top-level symbols — answers "where does X live"
before the first exploration turn. Stdlib only (os walk + ast), hard-capped,
and cached per (repo, HEAD): same commit, same map, zero cost to rebuild.

The map is a hint, not truth: it seeds navigation and agentic grep verifies.
A parse failure or unreadable file simply drops out of the map.
"""

from __future__ import annotations

import ast
import hashlib
import subprocess
import warnings
from pathlib import Path

MAX_CHARS = 12_000          # ≈3K tokens — the whole point is being cheap
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "dist", "build", "target",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", ".tox", ".idea", ".vscode", ".no_human",
    "coverage", "htmlcov", ".next", ".cache", "vendor", "eggs",
}
_SKIP_SUFFIXES = {
    ".pyc", ".so", ".dylib", ".min.js", ".map", ".lock", ".png", ".jpg",
    ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".zip", ".gz", ".jar", ".class",
}
_CACHE_DIR = Path.home() / ".no_human" / "cache"


def _head_sha(repo_path: Path) -> str:
    # Bounded + tolerant: `git` may be absent (FileNotFoundError) or hang on a
    # stale/NFS checkout — a non-SHA key just means "no cache reuse", never a
    # crash or an indefinite block (this runs inside the API request path too).
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path,
                              capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return "no-git"
    return proc.stdout.strip() if proc.returncode == 0 else "no-git"


def _python_symbols(path: Path) -> list[str]:
    """Top-level defs/classes of a Python file — 'where does X live' answers."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, SyntaxError, ValueError):
        return []
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(f"{node.name}()")
        elif isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and not n.name.startswith("_")][:6]
            out.append(f"{node.name}" + (f"({', '.join(methods)})" if methods else ""))
    return out


def build_repo_map(repo_path: Path) -> str:
    """The map text, freshly computed. Deterministic for a given tree."""
    lines: list[str] = []
    budget = MAX_CHARS

    def walk(d: Path, depth: int) -> None:
        nonlocal budget
        if budget <= 0 or depth > 5:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name))
        except OSError:
            return
        shown = 0
        for e in entries:
            if budget <= 0:
                return
            if e.name.startswith(".") and e.name not in (".github", ".gitlab-ci.yml"):
                continue
            if e.is_dir():
                if e.name in _SKIP_DIRS:
                    continue
                line = "  " * depth + e.name + "/"
                lines.append(line)
                budget -= len(line) + 1
                walk(e, depth + 1)
            else:
                if e.suffix in _SKIP_SUFFIXES:
                    continue
                shown += 1
                if shown > 40:  # a directory of hundreds of files is noise
                    lines.append("  " * depth + "…")
                    budget -= 4
                    break
                syms = _python_symbols(e) if e.suffix == ".py" else []
                line = "  " * depth + e.name
                if syms:
                    sym_txt = "  → " + ", ".join(syms)
                    line += sym_txt[: max(0, 160 - len(line))]
                lines.append(line)
                budget -= len(line) + 1

    walk(repo_path, 0)
    text = "\n".join(lines)
    return text[:MAX_CHARS]


def repo_map(repo_path: Path) -> str:
    """The map, cached per (repo, HEAD). A resumed attempt on the same commit
    costs nothing; a new commit regenerates (~<1s on a medium repo)."""
    repo_path = Path(repo_path)
    if not repo_path.is_dir():
        return ""  # a bogus path yields an empty map, never an exception
    key = hashlib.sha256(f"{repo_path}:{_head_sha(repo_path)}".encode()).hexdigest()[:24]
    cache = _CACHE_DIR / f"repomap-{key}.txt"
    try:
        if cache.is_file():
            return cache.read_text(encoding="utf-8")
    except OSError:
        pass
    text = build_repo_map(repo_path)
    try:
        from ..config import ensure_private_dir
        ensure_private_dir(_CACHE_DIR)
        cache.write_text(text, encoding="utf-8")
    except OSError:
        pass  # cache is an optimization, never a dependency
    return text
