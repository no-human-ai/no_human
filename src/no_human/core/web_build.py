"""Detect a stale `web/dist` on a source checkout and rebuild it (or warn).

Live evidence (verified 2026-09-04): the running `nh start` server served
`index-dWpodNF_.js`, a board bundle built 2026-09-03 20:31 — BEFORE that
day's UI fixes (reconnect banner, runtime chip, DOCS-step removal, mobile
nudge) landed in `web/src`. `web/dist` is gitignored, so a source checkout's
served bundle never refreshes on its own; a rebuild produced
`index-dXmN0vJY.js`, which carries the fixes. The operator's onboarding
crash was reproduced on the stale bundle, which lacked the reconnect handler
already present in source.

`api/app.py::_resolve_web_dist` handles a MISSING `web/dist` (warns, serves
a 503 notice) but had no staleness check at all — this module adds one, run
from `nh start` before the board is mounted.

A released wheel is exempt: `pyproject.toml`/`hatch_build.py` force-include
a freshly built `web/dist` into the wheel at release time (see
`test_wheel_ships_board.py`), and the installed layout has no `web/src`
beside it at all, so `is_source_layout` is `False` and this module is a
no-op there. The frozen desktop bundle is exempt the same way: it ships
`web/dist` but not `web/src`. Only a source checkout — where `web/src`
sits next to `web/dist` and the two can drift — needs this check.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_EXTRA_TRIGGERS = ("package.json", "package-lock.json", "pnpm-lock.yaml", "index.html")
_VITE_GLOB = "vite.config.*"
_SKIP_DIRS = {"node_modules", "dist", ".git", ".vite", "coverage", "test-results"}
_BUILD_ARGVS = (["npm", "--prefix", "web", "ci"], ["npm", "--prefix", "web", "run", "build"])
_BUILD_CMD_TEXT = "npm --prefix web ci && npm --prefix web run build"
_BUILD_TIMEOUT_S = 600.0
_TAIL_LINES = 40


def repo_web_dir() -> Path:
    """The checkout/frozen-bundle `web/` directory.

    `__file__` is ``<repo>/src/no_human/core/web_build.py`` in a checkout, so
    ``parents[3]`` is the repo root — the same candidate `api/app.py::_resolve_web_dist`
    and `core/db.py::_resolve_migrations_dir` use for their first (checkout /
    frozen) candidate. This module never looks at the wheel-install candidate
    (`<site-packages>/no_human/web_dist`) because a wheel install has no
    `web/src` beside it — `is_source_layout` is False there and the whole
    check is skipped.
    """
    return Path(__file__).resolve().parents[3] / "web"


def is_source_layout(web_dir: Path) -> bool:
    """True only when `web/src` sits beside `web/dist` — a source checkout.

    False for a wheel install (`web/` absent entirely) and for a frozen
    desktop bundle (`web/dist` present, `web/src` absent). This single
    predicate is the frozen/wheel exemption: neither layout has any `src` to
    compare `dist` against, and staleness is meaningless without it.
    """
    return (web_dir / "src").is_dir()


def newest_source(web_dir: Path) -> "tuple[float, Path] | None":
    """`(mtime, path)` of the newest file that should trigger a rebuild, or
    `None` if nothing matched.

    Scans EVERY file under `web/src` — not just code suffixes — because the
    served bundle depends on markup, styles, and static assets too (a
    `.css`/`.scss`/`.svg` edit, or a change to a component's stylesheet,
    rebuilds a different bundle just as a `.tsx` edit does). Prunes
    `_SKIP_DIRS` (notably `node_modules` and `dist`) so a stray build
    artifact or dependency tree nested under `src` can never itself look
    "stale". Also considers `web/package.json`,
    `web/package-lock.json`/`web/pnpm-lock.yaml` (lock file changes are
    dependency version changes, and must trigger a rebuild), `web/index.html`
    (vite's entry point, which lives beside `src/`, not under it), and
    `web/vite.config.*`.
    """
    best: "tuple[float, Path] | None" = None

    src_dir = web_dir / "src"
    if src_dir.is_dir():
        for dirpath, dirnames, filenames in os.walk(src_dir):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                path = Path(dirpath) / name
                with contextlib.suppress(OSError):
                    mtime = path.stat().st_mtime
                    if best is None or mtime > best[0]:
                        best = (mtime, path)

    extras = [web_dir / name for name in _EXTRA_TRIGGERS]
    extras += list(web_dir.glob(_VITE_GLOB))
    for path in extras:
        with contextlib.suppress(OSError):
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime
            if best is None or mtime > best[0]:
                best = (mtime, path)

    return best


def bundle_id(dist: Path) -> str:
    """Best-effort name of the served entry chunk, for the warning text.
    Never raises."""
    try:
        matches = sorted((dist / "assets").glob("index-*.js"))
    except OSError:
        return "unknown"
    return matches[0].name if matches else "unknown"


@dataclass(frozen=True)
class BoardFreshness:
    layout: str  # "source" | "frozen"
    state: str  # "fresh" | "stale" | "missing" | "skipped"
    dist_mtime: "float | None"
    newest_path: "Path | None"
    newest_mtime: "float | None"
    bundle: str


def inspect_board(web_dir: Path) -> BoardFreshness:
    """Compute the freshness verdict for `web_dir` without side effects."""
    if not is_source_layout(web_dir):
        return BoardFreshness("frozen", "skipped", None, None, None, "")

    index = web_dir / "dist" / "index.html"
    bundle = bundle_id(web_dir / "dist")
    if not index.is_file():
        return BoardFreshness("source", "missing", None, None, None, bundle)

    with contextlib.suppress(OSError):
        dist_mtime = index.stat().st_mtime
        found = newest_source(web_dir)
        newest_path, newest_mtime = (found[1], found[0]) if found else (None, None)
        # Strictly older, not <=: equal mtimes (coarse-mtime filesystems) must
        # read as fresh, or a freshly built dist could flap to "stale".
        state = "stale" if newest_mtime is not None and dist_mtime < newest_mtime else "fresh"
        return BoardFreshness("source", state, dist_mtime, newest_path, newest_mtime, bundle)

    return BoardFreshness("source", "fresh", None, None, None, bundle)


def rebuild(web_dir: Path, *, run: Callable = subprocess.run,
            timeout_s: float = _BUILD_TIMEOUT_S) -> "tuple[bool, str]":
    """Run `npm --prefix web ci && npm --prefix web run build` from the repo
    root. Never raises — mirrors `testing/ui_evidence.py::_run_build`'s
    shape (sequential argvs, shell=False, folded/capped tail on failure)
    without importing or refactoring it, since that module's build path is
    out of scope here.
    """
    repo_root = web_dir.parent
    started = time.monotonic()
    tail: list[str] = []
    for argv in _BUILD_ARGVS:
        remaining = timeout_s - (time.monotonic() - started)
        if remaining <= 0:
            return False, "build timed out: " + " ".join(tail[-_TAIL_LINES:])
        try:
            proc = run(argv, cwd=str(repo_root), stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=remaining)
        except subprocess.TimeoutExpired:
            return False, "build timed out running: " + " ".join(argv)
        except OSError as exc:
            return False, f"{type(exc).__name__}: {exc} (running {' '.join(argv)})"
        out = proc.stdout or b""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        tail.extend(out.splitlines()[-_TAIL_LINES:])
        if proc.returncode != 0:
            detail = f"build exit {proc.returncode} running {' '.join(argv)}: " + \
                " ".join(line.strip() for line in tail[-_TAIL_LINES:])
            return False, detail[:2048]
    return True, ""


def staleness_warning(v: BoardFreshness, web_dir: Path) -> str:
    """One loud line naming the stale bundle, the newer source file, and the
    exact rebuild command."""
    repo_root = web_dir.parent
    bundle = v.bundle or "unknown"
    if v.newest_path is not None:
        try:
            rel = v.newest_path.relative_to(repo_root)
        except ValueError:
            rel = v.newest_path
        source_bit = f"{rel} (mtime {v.newest_mtime}) is newer than dist (mtime {v.dist_mtime})"
    else:
        source_bit = "web/dist is missing"
    return (
        f"web/dist is stale — served bundle {bundle}, but {source_bit}. "
        f"Rebuild with: cd {repo_root} && {_BUILD_CMD_TEXT}"
    )


def _no_auto_build(env: "dict | None") -> bool:
    value = (env or {}).get("NH_NO_AUTO_BUILD", "")
    return bool(value) and value != "0"


def ensure_fresh_board(*, emit: Callable[[str], None], env: "dict | None" = None,
                        run: Callable = subprocess.run) -> BoardFreshness:
    """Single entrypoint: inspect, and rebuild-or-warn as needed.

    Never raises — a freshness diagnostic must never block `nh start`, the
    same contract `cli/commands.py::_warn_if_editable_install_dangles` uses.
    """
    if env is None:
        env = os.environ
    try:
        web_dir = repo_web_dir()
        v = inspect_board(web_dir)

        if v.state in ("skipped", "fresh"):
            return v  # byte-identical to today: no output at all

        if _no_auto_build(env):
            # Respected even when state == "missing": the env var is an
            # explicit opt-out for fast restarts, and overriding it here
            # would defeat that intent (the downstream missing-board 503
            # route in api/app.py still fires unchanged).
            emit(staleness_warning(v, web_dir))
            return v

        emit(f"board is stale — rebuilding (NH_NO_AUTO_BUILD=1 to skip)… ({_BUILD_CMD_TEXT})")
        ok, detail = rebuild(web_dir, run=run)
        if ok:
            fresh = inspect_board(web_dir)
            emit(f"board rebuilt — now serving {fresh.bundle or 'unknown'}")
            return fresh
        # Build failed: warn loudly, but never block startup — serve the
        # existing (stale/absent) dist as a degraded state.
        emit(f"board rebuild failed: {detail}")
        emit(staleness_warning(v, web_dir))
        return v
    except Exception as exc:  # noqa: BLE001 — never break `nh start`
        with contextlib.suppress(Exception):
            emit(f"board freshness check failed: {type(exc).__name__}: {exc}")
        return BoardFreshness("source", "missing", None, None, None, "")
