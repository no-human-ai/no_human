"""A brand-new user's first command must return.

The defect, reproduced 2026-08-01: install the wheel into a clean venv, point
HOME at an empty directory, and run `nh status` from outside the repo. It never
returned — not in 45s, not ever — with no useful output. `nh doctor`,
`nh task list` and `nh init` (via `nh onboard`) were the same; `nh --help` and
`nh --version` were fine, because they never open the store.

Two independent defects stacked up, and this file holds both to account.

1. THE WHEEL SHIPPED NO SCHEMA. `migrations/*.sql` lives at the repo root, and
   the wheel packaged only `src/no_human`, so no wheel ever built contained a
   single migration. `core/db.py` computed `parents[3]/"migrations"`, which in a
   wheel install points at `<venv>/lib/python3.X/migrations` — absent. And
   `Path.glob` on a missing directory does not raise, it yields NOTHING, so
   `_migrate` ran zero migrations and reported success. `_ensure_task_columns`
   then ran `PRAGMA table_info(tasks)`, which also does not raise on a missing
   table (it returns no rows), concluded every column was missing, and hit
   `ALTER TABLE tasks ADD COLUMN …` — the first statement in the whole chain
   that actually objects: `OperationalError: no such table: tasks`.

2. THAT ERROR HUNG THE PROCESS. `aiosqlite.connect()` starts a worker thread
   which is NOT a daemon and whose loop is a blocking `tx.get()` that ends only
   when `close()` enqueues a stop sentinel. `Store.connect()` raised after that
   thread existed, so `__aenter__` never returned, `__aexit__` never ran,
   `close()` was never called — and the interpreter sat in `threading._shutdown`
   joining a thread that would never finish. The traceback was printed and then
   the process simply never exited.

Defect 2 is the one that turned a clear error into a silent hang, and it is not
specific to defect 1: EVERY failure path in `connect()` had it. So it is tested
on its own, at the level it actually manifests — process exit — rather than
only as a unit assertion that could pass while the product still hangs.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


# --------------------------------------------------------------------------- #
# Packaging — the reason a wheel had no schema                                  #
# --------------------------------------------------------------------------- #

def test_pyproject_force_includes_migrations_in_wheel_and_sdist():
    """The declaration that puts the schema in the wheel.

    `force-include`, matching the `web/dist` precedent: hatchling raises
    `FileNotFoundError: Forced include not found` when the source is absent, so
    a release cut without the migrations fails the BUILD instead of shipping a
    CLI that hangs on first use. An `include`/`artifacts` glob would match
    nothing and build a quiet, schemaless wheel — which is this bug.
    """
    cfg = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    targets = cfg["tool"]["hatch"]["build"]["targets"]

    assert targets["wheel"]["force-include"]["migrations"] == "no_human/migrations"
    # The sdist keeps the repo-relative path so a wheel built FROM the sdist —
    # what `uv build` does, and what `pip install` does whenever it cannot use a
    # prebuilt wheel — still finds `migrations` where the wheel target reads it.
    assert targets["sdist"]["force-include"]["migrations"] == "migrations"


def test_repo_actually_has_migrations_to_ship():
    """Guards the guard: force-include is only loud if the source exists."""
    assert sorted((REPO_ROOT / "migrations").glob("*.sql")), \
        "no migrations at the repo root — the force-include has nothing to ship"


def test_resolve_migrations_dir_prefers_checkout_then_package_data(
    tmp_path, monkeypatch
):
    """Both layouts resolve, and neither can shadow the other.

    Driven against real directory trees rather than mocks: the function's whole
    job is to ask the filesystem a question, so a mock would only assert that
    the code calls the mock. Each layout is built on disk and the resolver is
    asked to find it.
    """
    db_module = importlib.import_module("no_human.core.db")
    resolve = db_module._resolve_migrations_dir

    # Layout A: source checkout / PyInstaller onedir bundle — <root>/migrations,
    # reached from <root>/src/no_human/core/db.py by parents[3]. The frozen
    # desktop server relies on exactly this: `build-installer.sh` copies the
    # migrations to the bundle root and its db.py sits at
    # <bundle>/_internal/no_human/core/db.py, so parents[3] is the bundle root.
    checkout = tmp_path / "checkout"
    core_dir = checkout / "src" / "no_human" / "core"
    core_dir.mkdir(parents=True)
    (checkout / "migrations").mkdir()
    (checkout / "migrations" / "0001_init.sql").write_text("SELECT 1;")

    monkeypatch.setattr(db_module, "__file__", str(core_dir / "db.py"))
    assert resolve() == checkout / "migrations"

    # Layout B: installed wheel — <site-packages>/no_human/migrations. parents[3]
    # points at <venv>/lib/python3.X, outside the package, and holds nothing.
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    wheel_core = site / "no_human" / "core"
    wheel_core.mkdir(parents=True)
    (site / "no_human" / "migrations").mkdir()
    (site / "no_human" / "migrations" / "0001_init.sql").write_text("SELECT 1;")

    monkeypatch.setattr(db_module, "__file__", str(wheel_core / "db.py"))
    assert resolve() == site / "no_human" / "migrations"

    # Neither present: fall back to the checkout path, so the error names the
    # directory a developer expects to see.
    bare = tmp_path / "bare"
    bare_core = bare / "src" / "no_human" / "core"
    bare_core.mkdir(parents=True)
    monkeypatch.setattr(db_module, "__file__", str(bare_core / "db.py"))
    assert resolve() == bare / "migrations"


def test_resolve_migrations_dir_ignores_a_directory_with_no_sql(
    tmp_path, monkeypatch
):
    """An empty `migrations/` husk must not count as the schema.

    A directory that exists but holds no *.sql is exactly as unusable as one
    that is absent, and an `.exists()` check would have accepted it. This is the
    same husk trap `_resolve_web_dist` already guards against with `index.html`.
    """
    db_module = importlib.import_module("no_human.core.db")
    resolve = db_module._resolve_migrations_dir

    root = tmp_path / "checkout"
    core_dir = root / "src" / "no_human" / "core"
    core_dir.mkdir(parents=True)
    (root / "migrations").mkdir()  # husk: exists, but holds no SQL

    pkg = root / "src" / "no_human" / "migrations"
    pkg.mkdir()
    (pkg / "0001_init.sql").write_text("SELECT 1;")

    monkeypatch.setattr(db_module, "__file__", str(core_dir / "db.py"))
    assert resolve() == pkg


# --------------------------------------------------------------------------- #
# Fail closed — a missing schema must say so                                    #
# --------------------------------------------------------------------------- #

async def test_connect_fails_closed_and_names_the_path_when_migrations_missing(
    tmp_path, monkeypatch
):
    """`glob` on a missing directory yields nothing instead of raising, so the
    natural spelling of `_migrate` was a fail-OPEN in the one place that must
    fail closed. The old code sailed through it and surfaced two frames later as
    `no such table: tasks`, naming neither the cause nor the path searched.
    """
    db_module = importlib.import_module("no_human.core.db")
    empty = tmp_path / "no-migrations-here"
    empty.mkdir()
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", empty)

    with pytest.raises(RuntimeError) as excinfo:
        await db_module.Store(tmp_path / "nh.db").connect()

    message = str(excinfo.value)
    assert str(empty) in message, f"error did not name the path searched: {message}"
    assert "migrations" in message.lower(), message


# --------------------------------------------------------------------------- #
# The hang itself — a failed connect() must not strand a non-daemon thread      #
# --------------------------------------------------------------------------- #

def _aiosqlite_threads() -> set[int]:
    return {
        t.ident
        for t in threading.enumerate()
        if t.is_alive() and "_connection_worker_thread" in t.name
    }


@pytest.mark.parametrize("failure", ["missing_migrations", "broken_sql"])
async def test_a_failed_connect_leaves_no_live_sqlite_worker_thread(
    tmp_path, monkeypatch, failure
):
    """The mechanism, asserted directly.

    `aiosqlite.connect()` has already started a non-daemon worker thread by the
    time anything downstream can fail. If `connect()` lets an exception out
    without closing, that thread stays blocked on `tx.get()` forever and
    `threading._shutdown` joins it forever. `connect()` must therefore be
    atomic: a usable Store, or no thread.

    Both failure paths are exercised, and the second is the one that matters.
    An empty directory trips the fail-closed `RuntimeError` before any SQL runs,
    and that exception's reference chain is short enough that CPython refcounting
    calls `Connection.__del__` — and so reaps the thread — even with the atomic
    close removed. A failing statement inside `executescript` keeps the
    connection reachable, which is the shape the shipped bug actually had. With
    only the empty-directory case, deleting the fix from `connect()` leaves every
    test in this file green.

    Threads are compared as a before/after delta rather than an absolute count
    because the rest of the suite legitimately holds open connections.
    """
    db_module = importlib.import_module("no_human.core.db")
    migrations = tmp_path / "no-migrations-here"
    migrations.mkdir()
    if failure == "broken_sql":
        (migrations / "0001_broken.sql").write_text("CREATE TABLE ;")
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", migrations)

    before = _aiosqlite_threads()

    with pytest.raises(Exception):
        await db_module.Store(tmp_path / "nh.db").connect()

    # close() awaits the stop future, but the thread returns from its loop just
    # after resolving it — allow a bounded moment for the OS to reap it rather
    # than racing on a sub-millisecond window.
    deadline = time.monotonic() + 5.0
    leaked = _aiosqlite_threads() - before
    while leaked and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        leaked = _aiosqlite_threads() - before

    assert not leaked, (
        "connect() raised and left a live aiosqlite worker thread behind. That "
        "thread is NOT a daemon, so the interpreter will block in "
        "threading._shutdown joining it and the command will never return."
    )


# The child does a failing connect() and then simply tries to exit. It must not
# import anything from the test process, and it talks back over stdout.
_EXIT_PROBE = r"""
import asyncio, pathlib, sys, tempfile
import no_human
from no_human.core import db as db_module

print("PKG:" + no_human.__file__, flush=True)

# Force the failure the wheel used to have. In a checkout the real migrations
# directory exists, so it has to be taken away. sys.argv[2] picks which failure:
# "missing_migrations" (empty dir → fail-closed RuntimeError before any SQL) or
# "broken_sql" (a statement that fails inside executescript, which is the shape
# the shipped bug had and the one that keeps the connection reachable).
migrations = pathlib.Path(tempfile.mkdtemp()) / "no-migrations-here"
migrations.mkdir()
if sys.argv[2] == "broken_sql":
    (migrations / "0001_broken.sql").write_text("CREATE TABLE ;")
db_module.MIGRATIONS_DIR = migrations

async def main():
    try:
        await db_module.Store(pathlib.Path(sys.argv[1])).connect()
    except BaseException as exc:
        print("RAISED:" + type(exc).__name__, flush=True)
        return
    print("RAISED:NOTHING", flush=True)

asyncio.run(main())
print("REACHED_EXIT", flush=True)
"""


@pytest.mark.parametrize(
    "failure,expected_exc",
    [("missing_migrations", "RuntimeError"), ("broken_sql", "OperationalError")],
)
def test_a_failed_connect_does_not_hang_the_process(tmp_path, failure, expected_exc):
    """The bug as the user met it: the command never comes back.

    Asserted at process level on purpose. The in-process test above pins the
    mechanism, but it cannot observe `threading._shutdown` — a unit test that
    passes while the real binary hangs would be worse than no test at all. So a
    child interpreter is driven into the failure and required to EXIT.

    Pre-fix this child printed its traceback and then hung indefinitely; the
    45s the user waited was not a slow migration, it was forever.

    `broken_sql` is the case with teeth. The `RuntimeError` path exits cleanly
    even without the atomic close in `connect()`, because refcounting reaps the
    connection first; only a failure raised from inside `executescript` holds
    the connection alive long enough to strand the non-daemon worker. Remove the
    fix and this parameter hangs while the other passes.
    """
    env = {k: v for k, v in os.environ.items()}
    env["PYTHONPATH"] = str(SRC)
    # An unwritable HOME must not be what ends the process instead.
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir()

    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _EXIT_PROBE, str(tmp_path / "nh.db"), failure],
            capture_output=True, text=True, timeout=60, env=env,
            cwd=tempfile.gettempdir(),  # never run from the repo
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            "a failed Store.connect() hung the interpreter: the child did not "
            "exit within 60s. This is the shipping bug — aiosqlite's non-daemon "
            "worker thread was never stopped, so threading._shutdown joins it "
            f"forever.\n--- child stdout ---\n{exc.stdout!r}"
        ) from None
    elapsed = time.monotonic() - started

    assert f"RAISED:{expected_exc}" in proc.stdout, (
        f"expected {expected_exc} from the {failure} path; "
        f"got stdout={proc.stdout!r} stderr={proc.stderr[-2000:]!r}"
    )
    assert "REACHED_EXIT" in proc.stdout, proc.stdout
    assert elapsed < 60, elapsed


# --------------------------------------------------------------------------- #
# Real-artifact tier — the only test that would have caught this end to end     #
# --------------------------------------------------------------------------- #

@pytest.mark.slow
def test_wheel_in_a_clean_venv_answers_the_first_commands(tmp_path):
    """Build the real wheel, install it clean, and run the commands that hung.

    Everything above can pass in a checkout while `pip install no-human` ships a
    CLI that never returns — the packaging half of this defect only exists once
    the code has been copied somewhere else. So: a real wheel, a real venv, an
    empty HOME, and a cwd outside the repo, which are jointly what it took to
    reproduce. Each command is given a hard timeout, and a timeout is the
    failure.

    Follows `tests/test_wheel_ships_board.py`: ~1 minute on a warm uv cache.
    Deselect with -m "not slow".
    """
    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH — cannot build the wheel")
    if not (REPO_ROOT / "web" / "dist" / "index.html").is_file():
        pytest.skip(
            "web/dist is not built in this checkout, so the board's forced "
            "include (hatch_build.py) fails a WHEEL build before migrations are "
            "reached. Run `cd web && npm install && npm run build` first. "
            "(CI's python job has no npm, so this is expected there.)"
        )

    dist = tmp_path / "dist"
    build = subprocess.run(["uv", "build", "--wheel", "-o", str(dist)],
                           cwd=REPO_ROOT, capture_output=True, text=True,
                           timeout=600)
    assert build.returncode == 0, f"uv build failed:\n{build.stderr}"
    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"

    # Cheap and decisive: the schema has to be IN the artifact.
    import zipfile
    with zipfile.ZipFile(wheels[0]) as zf:
        shipped = [n for n in zf.namelist()
                   if n.startswith("no_human/migrations/") and n.endswith(".sql")]
    expected = sorted(p.name for p in (REPO_ROOT / "migrations").glob("*.sql"))
    assert sorted(Path(n).name for n in shipped) == expected, (
        f"wheel ships {len(shipped)} migrations, repo has {len(expected)}: "
        f"{shipped}"
    )

    venv = tmp_path / "venv"
    mk = subprocess.run(["uv", "venv", str(venv), "--python", "3.12"],
                        capture_output=True, text=True, timeout=300)
    assert mk.returncode == 0, f"uv venv failed:\n{mk.stderr}"
    bindir = venv / ("Scripts" if os.name == "nt" else "bin")

    inst = subprocess.run(
        ["uv", "pip", "install", "--python", str(bindir / "python"),
         str(wheels[0])],
        capture_output=True, text=True, timeout=900,
        # Do not let the repo's own venv leak in and make a bad wheel pass.
        env={**os.environ, "VIRTUAL_ENV": str(venv)},
    )
    assert inst.returncode == 0, f"install failed:\n{inst.stderr}"

    home = tmp_path / "fakehome"
    home.mkdir()
    # A brand-new user: no ~/.no_human, no config, no DB, and PYTHONPATH must
    # not smuggle the checkout in — that would test the repo, not the wheel.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["HOME"] = str(home)

    nh = bindir / "nh"
    # `nh status` twice: the hang was NOT a one-time migration cost, so the
    # second run against the now-existing DB has to be checked too.
    for args in (["status"], ["status"], ["task", "list"], ["doctor"]):
        started = time.monotonic()
        try:
            proc = subprocess.run([str(nh), *args], capture_output=True,
                                  text=True, timeout=90, env=env,
                                  cwd=tempfile.gettempdir())
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"`nh {' '.join(args)}` never returned (>90s) from a clean "
                f"install with an empty HOME. This is the first-run hang."
            ) from None
        elapsed = time.monotonic() - started
        # `doctor` is a diagnostic gate: it exits 1 when it finds a
        # contradiction, and a clean HOME with no OAuth token on file IS one.
        # This test is about hangs and crashes, not about health, so accept
        # its diagnostic 1 — but keep it decisive by requiring the full
        # rendering below, which a crash or traceback would not produce.
        ok = (0, 1) if args == ["doctor"] else (0,)
        assert proc.returncode in ok, (
            f"`nh {' '.join(args)}` exited {proc.returncode} in {elapsed:.1f}s\n"
            f"--- stdout ---\n{proc.stdout[-3000:]}\n"
            f"--- stderr ---\n{proc.stderr[-3000:]}"
        )
        assert proc.stdout.strip(), (
            f"`nh {' '.join(args)}` produced no output at all"
        )
        if args == ["doctor"]:
            assert "mechanism liveness" in proc.stdout, (
                "`nh doctor` exited without rendering its report — that is a "
                f"crash, not a diagnosis:\n{proc.stdout[-3000:]}"
            )

    # And the DB really was built, rather than the commands merely not crashing.
    db_path = home / ".no_human" / "no_human.db"
    assert db_path.is_file(), f"no database at {db_path}: {list(home.rglob('*'))}"
    import sqlite3
    with sqlite3.connect(db_path) as con:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tasks", "attempts"} <= tables, (
        f"schema was never created; tables present: {sorted(tables)}")
