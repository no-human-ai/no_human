"""`nh start` must never silently serve a STALE `web/dist` on a source checkout.

Live evidence (2026-09-04): the running server served `index-dWpodNF_.js`,
built 2026-09-03 20:31 — before that day's reconnect-banner/runtime-chip/
DOCS-step fixes landed in `web/src`. `web/dist` is gitignored, so a source
checkout never refreshes the served bundle on its own; a rebuild produced
`index-dXmN0vJY.js`. `core/web_build.py` is the fix: a staleness check run
from `nh start`, before the board is mounted.

Driven against real directory trees with explicit `os.utime` mtimes, never
mocks and never sleeps — mirrors `test_wheel_ships_board.py`'s convention.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from no_human.core import web_build

REPO_ROOT = Path(__file__).resolve().parents[1]

_OLD = 1_700_000_000.0  # 2023-11-14 — "ancient" for any fixture purpose
_MID = 1_700_000_100.0
_NEW = 1_700_000_200.0


def _source_layout(tmp_path: Path, *, dist_mtime: float, src_mtime: float) -> Path:
    """`<root>/web/{src/App.tsx,package.json,vite.config.js,dist/index.html,
    dist/assets/index-dWpodNF_.js}` with explicit mtimes."""
    root = tmp_path / "checkout"
    web_dir = root / "web"
    src_dir = web_dir / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "App.tsx").write_text("export default function App() {}\n")
    (web_dir / "package.json").write_text("{}\n")
    (web_dir / "vite.config.js").write_text("export default {}\n")

    dist_dir = web_dir / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html></html>\n")
    (assets_dir / "index-dWpodNF_.js").write_text("console.log(1)\n")

    os.utime(src_dir / "App.tsx", (src_mtime, src_mtime))
    os.utime(web_dir / "package.json", (src_mtime - 10, src_mtime - 10))
    os.utime(web_dir / "vite.config.js", (src_mtime - 10, src_mtime - 10))
    os.utime(dist_dir / "index.html", (dist_mtime, dist_mtime))
    os.utime(assets_dir / "index-dWpodNF_.js", (dist_mtime, dist_mtime))

    return web_dir


class _RecordingRun:
    def __init__(self, returncode: int = 0):
        self.calls: list[tuple[list, str]] = []
        self.returncode = returncode

    def __call__(self, argv, cwd, **kwargs):
        self.calls.append((list(argv), cwd))

        class _Proc:
            pass

        proc = _Proc()
        proc.returncode = self.returncode
        proc.stdout = b"ok\n"
        return proc


def _failing_run(*args, **kwargs):
    raise AssertionError("run() must not be called")


# --------------------------------------------------------------------------- #
# AC 1: stale source-layout dist                                              #
# --------------------------------------------------------------------------- #

def test_stale_web_dist_triggers_a_rebuild_with_the_right_cwd(tmp_path):
    web_dir = _source_layout(tmp_path, dist_mtime=_OLD, src_mtime=_NEW)
    root = web_dir.parent
    run = _RecordingRun(returncode=0)

    # ensure_fresh_board resolves web_dir via repo_web_dir(); exercise the
    # narrower `rebuild()` directly against our fixture's web_dir first.
    v = web_build.inspect_board(web_dir)
    assert v.state == "stale"

    ok, detail = web_build.rebuild(web_dir, run=run)
    assert ok, detail
    assert len(run.calls) == 2
    (argv1, cwd1), (argv2, cwd2) = run.calls
    assert argv1 == ["npm", "--prefix", "web", "ci"]
    assert argv2 == ["npm", "--prefix", "web", "run", "build"]
    assert cwd1 == str(root)
    assert cwd2 == str(root)

    # Full orchestrator, with repo_web_dir monkeypatched to our fixture, must
    # take the same rebuild path and must NOT print the raw staleness warning
    # as its terminal outcome (it should announce the rebuild + success). The
    # stub `run` simulates a real `npm run build` by touching dist's mtime
    # forward, so the post-rebuild re-inspection reads "fresh" as it would
    # for a genuine build.
    calls: list[tuple[list, str]] = []

    def _run_and_touch(argv, cwd, **kwargs):
        calls.append((list(argv), cwd))
        if argv[-1] == "build":
            fresh_mtime = _NEW + 1000
            os.utime(web_dir / "dist" / "index.html", (fresh_mtime, fresh_mtime))
            os.utime(web_dir / "dist" / "assets" / "index-dWpodNF_.js",
                      (fresh_mtime, fresh_mtime))

        class _Proc:
            pass

        proc = _Proc()
        proc.returncode = 0
        proc.stdout = b"ok\n"
        return proc

    emitted2: list[str] = []
    import no_human.core.web_build as wb_mod
    orig = wb_mod.repo_web_dir
    wb_mod.repo_web_dir = lambda: web_dir
    try:
        outcome = web_build.ensure_fresh_board(emit=emitted2.append, env={}, run=_run_and_touch)
    finally:
        wb_mod.repo_web_dir = orig

    assert len(calls) == 2
    assert calls[0][0] == ["npm", "--prefix", "web", "ci"]
    assert calls[1][0] == ["npm", "--prefix", "web", "run", "build"]
    assert calls[0][1] == str(root)
    assert calls[1][1] == str(root)
    assert outcome.state == "fresh"
    assert emitted2, "expected at least one informational emit"
    warning_text = web_build.staleness_warning(v, web_dir)
    assert emitted2[-1] != warning_text, (
        "the terminal outcome must be the success line, not the raw staleness warning"
    )


def test_stale_web_dist_warns_instead_of_building_under_nh_no_auto_build(tmp_path):
    web_dir = _source_layout(tmp_path, dist_mtime=_OLD, src_mtime=_NEW)
    emitted: list[str] = []

    import no_human.core.web_build as wb_mod
    orig = wb_mod.repo_web_dir
    wb_mod.repo_web_dir = lambda: web_dir
    try:
        outcome = web_build.ensure_fresh_board(
            emit=emitted.append, env={"NH_NO_AUTO_BUILD": "1"}, run=_failing_run,
        )
    finally:
        wb_mod.repo_web_dir = orig

    assert outcome.state == "stale"
    assert len(emitted) == 1
    assert "npm --prefix web ci && npm --prefix web run build" in emitted[0]
    assert "index-dWpodNF_.js" in emitted[0]


# --------------------------------------------------------------------------- #
# AC 2: frozen / wheel layout is exempt                                       #
# --------------------------------------------------------------------------- #

def test_frozen_layout_never_builds_or_warns_about_web_dist_staleness(tmp_path):
    # Arm A: frozen bundle — web/dist present, no web/src, dist mtime ancient.
    root_a = tmp_path / "frozen"
    web_dir_a = root_a / "web"
    dist_dir_a = web_dir_a / "dist"
    dist_dir_a.mkdir(parents=True)
    (dist_dir_a / "index.html").write_text("<html></html>\n")
    os.utime(dist_dir_a / "index.html", (_OLD, _OLD))

    emitted: list[str] = []
    v = web_build.inspect_board(web_dir_a)
    assert v.state == "skipped"
    assert v.layout == "frozen"

    import no_human.core.web_build as wb_mod
    orig = wb_mod.repo_web_dir
    wb_mod.repo_web_dir = lambda: web_dir_a
    try:
        outcome = web_build.ensure_fresh_board(
            emit=emitted.append, env={}, run=_failing_run,
        )
    finally:
        wb_mod.repo_web_dir = orig
    assert outcome.state == "skipped"
    assert emitted == []

    # Arm B: wheel layout — no web/ dir at all.
    root_b = tmp_path / "wheel"
    root_b.mkdir()
    web_dir_b = root_b / "web"  # does not exist

    emitted_b: list[str] = []
    v_b = web_build.inspect_board(web_dir_b)
    assert v_b.state == "skipped"

    wb_mod.repo_web_dir = lambda: web_dir_b
    try:
        outcome_b = web_build.ensure_fresh_board(
            emit=emitted_b.append, env={}, run=_failing_run,
        )
    finally:
        wb_mod.repo_web_dir = orig
    assert outcome_b.state == "skipped"
    assert emitted_b == []


# --------------------------------------------------------------------------- #
# AC 3: fresh dist starts silently                                            #
# --------------------------------------------------------------------------- #

def test_fresh_web_dist_starts_silently(tmp_path):
    web_dir = _source_layout(tmp_path, dist_mtime=_NEW, src_mtime=_OLD)
    emitted: list[str] = []

    import no_human.core.web_build as wb_mod
    orig = wb_mod.repo_web_dir
    wb_mod.repo_web_dir = lambda: web_dir
    try:
        outcome = web_build.ensure_fresh_board(
            emit=emitted.append, env={}, run=_failing_run,
        )
    finally:
        wb_mod.repo_web_dir = orig

    assert outcome.state == "fresh"
    assert emitted == []


def test_fresh_web_dist_equal_mtime_does_not_flap_to_stale(tmp_path):
    web_dir = _source_layout(tmp_path, dist_mtime=_MID, src_mtime=_MID)
    v = web_build.inspect_board(web_dir)
    assert v.state == "fresh"


# --------------------------------------------------------------------------- #
# Intake answer 1: a failing build warns loudly and still starts               #
# --------------------------------------------------------------------------- #

def test_a_failing_build_warns_loudly_and_still_starts(tmp_path):
    web_dir = _source_layout(tmp_path, dist_mtime=_OLD, src_mtime=_NEW)
    emitted: list[str] = []
    run = _RecordingRun(returncode=1)

    import no_human.core.web_build as wb_mod
    orig = wb_mod.repo_web_dir
    wb_mod.repo_web_dir = lambda: web_dir
    try:
        outcome = web_build.ensure_fresh_board(emit=emitted.append, env={}, run=run)
    finally:
        wb_mod.repo_web_dir = orig

    assert outcome is not None
    assert any("npm --prefix web ci && npm --prefix web run build" in m for m in emitted)

    # Second arm: npm absent entirely (FileNotFoundError).
    def _missing_npm(argv, cwd, **kwargs):
        raise FileNotFoundError("npm not found")

    emitted2: list[str] = []
    wb_mod.repo_web_dir = lambda: web_dir
    try:
        outcome2 = web_build.ensure_fresh_board(emit=emitted2.append, env={}, run=_missing_npm)
    finally:
        wb_mod.repo_web_dir = orig

    assert outcome2 is not None
    assert any("npm --prefix web ci && npm --prefix web run build" in m for m in emitted2)


# --------------------------------------------------------------------------- #
# Intake answer 2: missing dist + NH_NO_AUTO_BUILD does not force a build      #
# --------------------------------------------------------------------------- #

def test_missing_web_dist_under_no_auto_build_does_not_force_a_build(tmp_path):
    root = tmp_path / "checkout"
    web_dir = root / "web"
    src_dir = web_dir / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "App.tsx").write_text("export default function App() {}\n")
    # no dist/ at all

    emitted: list[str] = []
    import no_human.core.web_build as wb_mod
    orig = wb_mod.repo_web_dir
    wb_mod.repo_web_dir = lambda: web_dir
    try:
        outcome = web_build.ensure_fresh_board(
            emit=emitted.append, env={"NH_NO_AUTO_BUILD": "1"}, run=_failing_run,
        )
    finally:
        wb_mod.repo_web_dir = orig

    assert outcome.state == "missing"
    assert emitted, "expected a loud warning naming the missing board"


# --------------------------------------------------------------------------- #
# Intake answer 3: scan ignores node_modules/dist, honors lock file changes    #
# --------------------------------------------------------------------------- #

def test_the_source_scan_ignores_node_modules_and_dist(tmp_path):
    web_dir = _source_layout(tmp_path, dist_mtime=_MID, src_mtime=_OLD)

    node_modules = web_dir / "node_modules" / "somepkg"
    node_modules.mkdir(parents=True)
    (node_modules / "index.js").write_text("module.exports = {}\n")
    os.utime(node_modules / "index.js", (_NEW, _NEW))

    dist_dir = web_dir / "dist"
    (dist_dir / "sourcemap.js.map").write_text("{}\n")
    os.utime(dist_dir / "sourcemap.js.map", (_NEW, _NEW))

    v = web_build.inspect_board(web_dir)
    assert v.state == "fresh", "node_modules/dist churn must not read as stale"

    lock = web_dir / "package-lock.json"
    lock.write_text("{}\n")
    os.utime(lock, (_NEW, _NEW))

    v2 = web_build.inspect_board(web_dir)
    assert v2.state == "stale", "a new package-lock.json must trigger staleness"
    assert v2.newest_path == lock


# --------------------------------------------------------------------------- #
# Review fix: staleness must cover non-JS/TS assets under web/src too         #
# --------------------------------------------------------------------------- #

def test_a_css_only_change_under_web_src_is_detected_as_stale(tmp_path):
    """A stylesheet edit under `web/src` rebuilds a different bundle just as
    a `.tsx` edit does — the scan must not be limited to code suffixes."""
    web_dir = _source_layout(tmp_path, dist_mtime=_MID, src_mtime=_OLD)

    css = web_dir / "src" / "App.css"
    css.write_text(".App { color: red; }\n")
    os.utime(css, (_OLD, _OLD))

    v = web_build.inspect_board(web_dir)
    assert v.state == "fresh"

    os.utime(css, (_NEW, _NEW))
    v2 = web_build.inspect_board(web_dir)
    assert v2.state == "stale", "a newer .css file under web/src must trigger staleness"
    assert v2.newest_path == css


# --------------------------------------------------------------------------- #
# Load-bearing ordering: freshness check must run before api.app import       #
# --------------------------------------------------------------------------- #

def test_nh_start_checks_freshness_before_importing_the_api_app():
    commands_path = REPO_ROOT / "src" / "no_human" / "cli" / "commands.py"
    text = commands_path.read_text()

    start_idx = text.index("\ndef start(host, port, workers, no_open):")
    tail = text[start_idx:]
    # Bound the search to the body of `start` (up to the next top-level def).
    next_def = tail.index("\ndef ", 1)
    body = tail[:next_def]

    fresh_idx = body.index("_ensure_board_fresh()")
    import_idx = body.index('from ..api.app import app as _app')
    assert fresh_idx < import_idx, (
        "_ensure_board_fresh() must run before `from ..api.app import app as _app` "
        "— the StaticFiles mount is decided at import time, so a rebuild after "
        "the import would never be picked up"
    )
