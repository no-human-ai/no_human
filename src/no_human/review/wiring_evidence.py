"""Deterministic wiring evidence for the reviewer's goal-reachability check.

Lists symbols a diff ADDS that have no reference anywhere in the after-state
tree outside their own defining file and test paths. That is the raw material
of the "implemented but never called by the production path" defect class —
surfaced as labeled evidence next to the lint section, never as a verdict: an
unreferenced symbol may be wired through dynamic dispatch this module cannot
see, or be exactly the uncalled artifact the request asks for.

Named ceilings, stated because the prompt block repeats them:

* LANGUAGES: Python and the JS/TS family, via `review.symbols`. A diff in any
  other language contributes nothing and the section is absent.
* DEPTH: module level and class bodies. A `def` inside a function and an
  unexported JS binding are file-private by construction and are skipped —
  `review.symbols` carries the argument.
* STATIC: references are found by ``git grep -w`` over the after-state tree.
  Dynamic dispatch, registries, ``getattr``, string-built names and console
  entry points are invisible to it.
* BY BARE NAME: a method is searched for as ``close``, not ``Store.close``,
  so an unrelated ``other.close()`` elsewhere in the tree reads as a
  reference. That direction is deliberate — the rule below is to fail toward
  silence — but it means a listed method is stronger evidence than an
  unlisted one is.

Advisory only, like lint_evidence: any failure — a bad ref, unparseable
source, a timeout — returns an empty or partial result and must never block
or slow the review gate.
"""

from __future__ import annotations

import logging
import subprocess
import time

from .symbols import declared_symbols, language_of

log = logging.getLogger(__name__)

# Hard cap on the whole collection pass, same rationale as
# lint_evidence.LINT_TIMEOUT: advisory evidence must never stall the gate.
# Enforced as ONE deadline across every subprocess, not per subprocess — with
# a `git grep` per symbol, a per-call timeout bounds nothing in aggregate.
WIRING_TIMEOUT = 30
# One `git grep` per distinct name, so bound the symbol count. Reading class
# bodies and a second language family raised the count a diff can produce;
# the deadline above is what actually bounds the cost.
MAX_WIRING_SYMBOLS = 50
MAX_WIRING_BYTES = 4096


def _is_test_path(path: str) -> bool:
    """True for paths whose references do not count as production wiring."""
    parts = path.split("/")
    name = parts[-1]
    return (
        "tests" in parts
        or "test" in parts
        or "__tests__" in parts
        or "__mocks__" in parts
        or name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
        or name == "conftest.py"
    )


def _show(repo_path, ref: str, rel: str, timeout: float) -> str | None:
    """Text of ``rel`` at ``ref``, or None (absent, binary, unreadable)."""
    proc = subprocess.run(
        ["git", "show", f"{ref}:{rel}"],
        cwd=repo_path, capture_output=True, timeout=timeout,
    )
    if proc.returncode != 0 or b"\x00" in proc.stdout:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def _referencing_paths(
    repo_path, after_ref: str, name: str, timeout: float,
) -> list[str] | None:
    """Paths in ``after_ref`` holding ``name`` as a word, or None when the
    search could not be trusted."""
    grep = subprocess.run(
        ["git", "grep", "-l", "-w", "--fixed-strings", "-e", name, after_ref],
        cwd=repo_path, capture_output=True, text=True, errors="replace",
        timeout=timeout, encoding="utf-8",
    )
    if grep.returncode not in (0, 1):
        return None
    # `git grep -l <ref>` lines are "<ref>:<path>".
    return [
        hit.split(":", 1)[1] if ":" in hit else hit
        for hit in (grep.stdout or "").splitlines()
    ]


def collect_wiring_evidence(
    repo_path, before_ref: str, after_ref: str, *, timeout: int = WIRING_TIMEOUT,
) -> list[tuple[str, str]]:
    """``(defining_path, qualified_name)`` pairs the diff adds with no
    reference found outside the defining file and test paths, sorted. ``[]``
    on ANY failure.

    A symbol whose reference search fails (git error, timeout) is treated as
    referenced — this evidence fails toward silence, never toward accusation.
    The same rule governs the deadline: when it runs out mid-search the
    symbols already decided are returned and the rest are simply not asked
    about, so a slow repo under-reports rather than mis-reports.
    """
    deadline = time.monotonic() + timeout

    def remaining() -> float:
        return deadline - time.monotonic()

    try:
        proc = subprocess.run(
            ["git", "diff", "--name-status", "-M", f"{before_ref}..{after_ref}"],
            cwd=repo_path, capture_output=True, text=True, errors="replace",
            timeout=remaining(), encoding="utf-8",
        )
        if proc.returncode != 0:
            return []
        added: list[tuple[str, str, str]] = []
        for line in (proc.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or parts[0].startswith("D"):
                continue
            rel = parts[-1]
            if language_of(rel) is None or _is_test_path(rel):
                continue
            after_text = _show(repo_path, after_ref, rel, remaining())
            if after_text is None:
                continue
            before_text = _show(repo_path, before_ref, rel, remaining()) or ""
            before_symbols = declared_symbols(before_text, rel)
            added.extend(
                (rel, qualified, name)
                for qualified, name in declared_symbols(after_text, rel).items()
                if qualified not in before_symbols
            )
        added = sorted(added)[:MAX_WIRING_SYMBOLS]

        unreferenced: list[tuple[str, str]] = []
        searched: dict[str, list[str] | None] = {}
        for rel, qualified, name in added:
            if remaining() <= 0:
                log.warning(
                    "wiring evidence ran out of budget after %ds", timeout)
                break
            if name not in searched:
                searched[name] = _referencing_paths(
                    repo_path, after_ref, name, remaining(),
                )
            paths = searched[name]
            if paths is None:
                continue  # untrusted search — treat as referenced
            outside = any(
                path != rel and not _is_test_path(path) for path in paths
            )
            if not outside:
                unreferenced.append((rel, qualified))
        return sorted(unreferenced)
    except subprocess.TimeoutExpired:
        log.warning("wiring evidence timed out after %ds", timeout)
        return []
    except Exception:  # noqa: BLE001 — advisory evidence, never blocks review
        log.warning("wiring evidence failed", exc_info=True)
        return []


def format_wiring_evidence(unreferenced: list[tuple[str, str]]) -> str:
    """Render the labeled block for the reviewer prompt; "" when empty.

    Capped at MAX_WIRING_BYTES with a truncation note, like lint evidence.
    """
    if not unreferenced:
        return ""
    header = (
        "WIRING EVIDENCE (deterministic, static — Python and JS/TS, module "
        "level and class bodies; dynamic dispatch/registries not visible): "
        "symbols this diff adds with no reference found outside their "
        "defining file and test paths:"
    )
    lines = [header]
    size = len(header)
    shown = 0
    for rel, name in unreferenced:
        line = f"  {rel}: {name}"
        if size + 1 + len(line) > MAX_WIRING_BYTES:
            break
        lines.append(line)
        size += 1 + len(line)
        shown += 1
    if shown < len(unreferenced):
        lines.append(f"  ... truncated ({len(unreferenced) - shown} more)")
    lines.append(
        "  Evidence, not a verdict: a listed symbol may be wired dynamically, "
        "or be exactly the uncalled artifact the request asks for."
    )
    return "\n".join(lines)
