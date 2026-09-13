#!/usr/bin/env python3
"""Recount the three "What the gate caught" figures pinned in `README.md`
against `scripts/readme_metrics.json`.

The README prints a DATED SNAPSHOT (`source_release_tag: metrics-2026-09`,
window 2026-07-10..2026-09-13), not a live count -- the loop keeps running,
so anything that recomputes the figures at test time would go stale the day
after it lands. This script exists so an operator can regenerate the
snapshot on demand; it never runs automatically, and it never rewrites
`readme_metrics.json` itself (a script that could re-bless its own snapshot
would defeat the point of pinning it).

USAGE:

    python scripts/recount_readme_metrics.py <db-path> [--start YYYY-MM-DD]
        [--end YYYY-MM-DD] [--json]

`<db-path>` is a REQUIRED positional with no default -- no code path here
reaches the operator's live database by omission. `--start`/`--end` default
to the window recorded in `scripts/readme_metrics.json` (the window has one
home, not two copies that can drift apart).

LIVE-DATABASE GUARD (`validate_db_path`), the same idiom
`scripts/memory_triage_runbook.py` uses (copied here rather than imported,
so this script has no dependency on that one or on the `no_human` package):
the given path is resolved with symlinks followed and refused outright if it
equals, or sits under, the live `NO_HUMAN_HOME` (env override, default
`~/.no_human`) -- computed as pure path arithmetic (no `mkdir`, no
`exists()`), so the guard -- and this whole script's test suite -- works
identically on a machine that has no `~/.no_human` directory at all. A
missing path, a non-regular file, a file that is not a readable SQLite
database, or a database with no `attempts` table are refused the same way:
an absent or foreign database is a FAILURE here, never a comfortable zero.

READ PATH: once the path clears the guard, `_backup_to_temp_copy` opens the
SOURCE read-only and immutable (`file:...?mode=ro&immutable=1`) and uses
SQLite's own `Connection.backup()` API to copy it into a fresh
`tempfile.TemporaryDirectory()`. Every query below runs against that COPY;
the source is never written to and never even opened for anything but the
backup read. `immutable=1` is load-bearing, not decorative: a plain
`mode=ro` connection to a WAL-mode database still creates `-wal`/`-shm`
sidecars NEXT TO THE SOURCE the instant it is read (SQLite needs them to
serve a consistent WAL read even when it will never write) -- `immutable=1`
tells SQLite the file will not change for the connection's lifetime, which
skips that bookkeeping entirely. This script never reaches the backup call
against the live database (the guard above refuses first).

SCHEMA TOLERANCE: `tasks.kind` is added defensively at runtime by
`Store._ensure_task_columns` for already-created databases (see
`migrations/0003_task_kind.sql`) and appears in no `.sql` migration file
itself. This script probes `PRAGMA table_info(tasks)` and, if `kind` is
absent, treats every task as non-`code_review` and reports
`kind_column_present: false` rather than raising `no such column`. It never
`ALTER`s the copy to add it.

DEFINITIONS (the whole reason a loose text match gets this wrong):

  - scope: `attempts` joined to `tasks`; in scope when
    `os.path.basename(repo_path.rstrip("/\\\\"))` is exactly `no_human` or
    `no_human-public`. A NULL `repo_path`, or a basename that merely
    *contains* one of those names (e.g. `foo-no_human`), is out of scope.
  - window: `started_at`, taken as its first 10 characters (`YYYY-MM-DD`,
    which both the `datetime('now')` default and an ISO `...T...` timestamp
    share), compared inclusively against `--start`/`--end`. A NULL or empty
    `started_at` is excluded and tallied under `skipped_no_timestamp`,
    never silently dropped.
  - `attempts_reviewed`: in scope and in window, `kind != 'code_review'`,
    `review_passed IS NOT NULL` -- the attempt reached a review verdict at
    all. This is the README's "of 1,709" denominator.
  - `reviewer_rejections`: the above, plus `review_passed = 0`, plus
    `failure_reason` does NOT start with `"review failed: reviewer"` or
    `"review failed: Reviewer"` (both cases are reviewer INFRASTRUCTURE
    failures -- a dead session, a transport failure -- never a rejection of
    the diff). A NULL `failure_reason` still counts as a rejection.
  - `tamper_stops`: `test_results` parsed as JSON (tolerating NULL, empty,
    unparseable, and double-encoded JSON with one extra `json.loads`) whose
    top-level object has a truthy `tamper_flag`. This is a PARSE, never a
    substring match -- `'"tamper_flag": true' in text` would also match the
    literal text `"tamper_flag: true"`, which is not JSON at all and must
    not count. Anything that fails to parse is tallied under
    `unparseable_test_results`, never fatal and never counted as a stop.
  - `refused_proofs`: `failure_reason` starts with `"repro gate fail: "` and
    does NOT start with the more specific `"repro gate fail: resume-shape:"`
    -- a resumed attempt's repro failure is prefixed `resume-shape:` inside
    the same detail string and is excluded, because it is testing a
    different claim (the resumed checkpoint's own tree) than a first-attempt
    refusal.

Scope is otherwise the whole `attempts` table for a database of any size;
there is no LIMIT and no sampling.

OUTPUT: a human-readable table by default; `--json` emits the four figures
under the snapshot's own key names, plus the window, scope, resolved DB
path, and the diagnostics above (`kind_column_present`,
`skipped_no_timestamp`, `unparseable_test_results`, `population`). Nothing
is written anywhere except the disposable temp copy, which is removed
before this script exits.

NON-HAPPY PATHS: a path inside the live home, a missing path, a directory,
a non-SQLite file, or a SQLite file with no `attempts` table are all
refused (exit 1, message on stderr, nothing opened for real). A database
with rows but ZERO of them landing in scope-and-window is refused too (exit
1, "empty population") -- a silent 0/0 "match" would be the worst possible
false green for a script whose entire job is catching drift.

Re-running this against a LATER database will legitimately produce LARGER
numbers than the committed snapshot. That is not a bug in this script; it
is why the snapshot is a dated, committed file and not something computed
at test time.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SNAPSHOT_PATH = SCRIPT_DIR / "readme_metrics.json"

SCOPE_BASENAMES = frozenset({"no_human", "no_human-public"})
CODE_REVIEW_KIND = "code_review"
REVIEWER_INFRA_PREFIXES = ("review failed: reviewer", "review failed: Reviewer")
REFUSED_PROOF_PREFIX = "repro gate fail: "
RESUME_SHAPE_PREFIX = "repro gate fail: resume-shape:"


class DbRefusal(Exception):
    """Raised by `validate_db_path` -- a path that fails the live-DB guard or
    a basic sanity check. Nothing is opened for real before this is raised;
    the message names the exact refusal."""


def _live_home() -> Path:
    raw = os.environ.get("NO_HUMAN_HOME")
    base = Path(raw) if raw else (Path.home() / ".no_human")
    return base.expanduser().resolve()


def validate_db_path(raw_path: str) -> Path:
    """Fail-closed gate run before opening *raw_path* for real. Refuses
    (raises `DbRefusal`, opens nothing):

    - the path resolves (symlinks followed) inside the live `NO_HUMAN_HOME`
      (env override, default `~/.no_human`) -- the operator's live database.
    - the path does not exist, or is not a regular file.
    - the file is not a readable SQLite database, or has no `attempts`
      table -- an absent/unreadable/foreign DB is a FAILURE, never a
      comfortable zero.
    """
    target = Path(raw_path).expanduser()
    resolved = target.resolve()
    home = _live_home()
    if resolved == home or home in resolved.parents:
        raise DbRefusal(
            f"refusing: {resolved} resolves inside the live NO_HUMAN_HOME "
            f"({home}) -- this script must never touch the operator's live "
            f"database; point it at a throwaway copy instead")
    if not resolved.exists():
        raise DbRefusal(f"refusing: {resolved} does not exist")
    if not resolved.is_file():
        raise DbRefusal(f"refusing: {resolved} is not a regular file")
    try:
        con = sqlite3.connect(f"file:{resolved}?mode=ro&immutable=1", uri=True)
        try:
            con.execute("PRAGMA schema_version")
            row = con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'attempts'"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise DbRefusal(
            f"refusing: {resolved} is not a readable SQLite database "
            f"({exc})") from exc
    if row is None:
        raise DbRefusal(
            f"refusing: {resolved} has no 'attempts' table -- not a "
            f"no_human database")
    return resolved


def _backup_to_temp_copy(source: Path, dest: Path) -> None:
    """Copy *source* into *dest* via SQLite's backup API rather than
    `shutil.copy` -- see the module docstring for why `immutable=1` is
    load-bearing. Every read in this script runs against *dest*, never
    *source*."""
    src_con = sqlite3.connect(f"file:{source}?mode=ro&immutable=1", uri=True)
    dest_con = sqlite3.connect(dest)
    try:
        src_con.backup(dest_con)
    finally:
        dest_con.close()
        src_con.close()


def _load_snapshot_window() -> tuple[str, str]:
    data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    window = data["window"]
    return window["start"], window["end"]


def _has_kind_column(con: sqlite3.Connection) -> bool:
    cols = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
    return "kind" in cols


def _date_key(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) < 10:
        return None
    return text[:10]


def _basename_in_scope(repo_path) -> bool:
    if not repo_path:
        return False
    stripped = str(repo_path).rstrip("/\\")
    if not stripped:
        return False
    return os.path.basename(stripped) in SCOPE_BASENAMES


def _tamper_flag(test_results) -> bool | None:
    """True/False on a successful parse, None when unparseable -- never a
    substring match against the raw text."""
    if test_results is None:
        return False
    text = str(test_results).strip()
    if not text:
        return False
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, dict):
        return bool(value.get("tamper_flag"))
    return False


def _is_reviewer_infra_failure(failure_reason: str) -> bool:
    return failure_reason.startswith(REVIEWER_INFRA_PREFIXES)


def _is_refused_proof(failure_reason: str) -> bool:
    return (failure_reason.startswith(REFUSED_PROOF_PREFIX)
            and not failure_reason.startswith(RESUME_SHAPE_PREFIX))


def recount(db_path: Path, *, start: str, end: str) -> dict:
    """Read *db_path* (already a disposable copy) and return the four
    figures plus diagnostics. Never opens anything but *db_path* itself."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        kind_present = _has_kind_column(con)
        kind_select = "t.kind AS kind, " if kind_present else ""
        rows = con.execute(
            f"SELECT a.review_passed AS review_passed, "
            f"a.failure_reason AS failure_reason, "
            f"a.test_results AS test_results, a.started_at AS started_at, "
            f"t.repo_path AS repo_path, {kind_select}"
            f"a.id AS id "
            f"FROM attempts a JOIN tasks t ON t.id = a.task_id"
        ).fetchall()
    finally:
        con.close()

    attempts_reviewed = 0
    reviewer_rejections = 0
    tamper_stops = 0
    refused_proofs = 0
    population = 0
    skipped_no_timestamp = 0
    unparseable_test_results = 0

    for row in rows:
        if not _basename_in_scope(row["repo_path"]):
            continue
        date_key = _date_key(row["started_at"])
        if date_key is None:
            skipped_no_timestamp += 1
            continue
        if not (start <= date_key <= end):
            continue
        population += 1

        kind = row["kind"] if kind_present else None
        is_code_review = kind == CODE_REVIEW_KIND

        review_passed = row["review_passed"]
        review_passed = None if review_passed is None else int(review_passed)
        failure_reason = row["failure_reason"] or ""

        if review_passed is not None and not is_code_review:
            attempts_reviewed += 1
            if review_passed == 0 and not _is_reviewer_infra_failure(failure_reason):
                reviewer_rejections += 1

        tamper = _tamper_flag(row["test_results"])
        if tamper is None:
            unparseable_test_results += 1
        elif tamper:
            tamper_stops += 1

        if _is_refused_proof(failure_reason):
            refused_proofs += 1

    return {
        "figures": {
            "attempts_reviewed": attempts_reviewed,
            "reviewer_rejections": reviewer_rejections,
            "tamper_stops": tamper_stops,
            "refused_proofs": refused_proofs,
        },
        "window": {"start": start, "end": end},
        "scope": {"repo_basenames": sorted(SCOPE_BASENAMES)},
        "kind_column_present": kind_present,
        "population": population,
        "skipped_no_timestamp": skipped_no_timestamp,
        "unparseable_test_results": unparseable_test_results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recount_readme_metrics.py",
        description=(
            "Recount the README's \"What the gate caught\" figures from a "
            "THROWAWAY COPY of the no_human database. See the module "
            "docstring for the exact definitions and the live-database "
            "guard."),
    )
    parser.add_argument(
        "db_path",
        help="Path to a copy of the no_human database. Refused outright if "
             "it resolves inside the live NO_HUMAN_HOME (default "
             "~/.no_human) -- point this at a throwaway copy, never the "
             "operator's live database.")
    parser.add_argument(
        "--start", default=None,
        help="Window start, YYYY-MM-DD (default: scripts/readme_metrics.json).")
    parser.add_argument(
        "--end", default=None,
        help="Window end, YYYY-MM-DD (default: scripts/readme_metrics.json).")
    parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="Emit JSON instead of a human-readable table.")
    return parser


def _print_table(result: dict) -> None:
    figures = result["figures"]
    window = result["window"]
    print(f"window: {window['start']} .. {window['end']}")
    print(f"scope: {', '.join(result['scope']['repo_basenames'])}")
    print(f"kind_column_present: {result['kind_column_present']}")
    print(f"population (scope+window): {result['population']}")
    for key, value in figures.items():
        print(f"{key}: {value}")
    print(f"skipped_no_timestamp: {result['skipped_no_timestamp']}")
    print(f"unparseable_test_results: {result['unparseable_test_results']}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        db_path = validate_db_path(args.db_path)
    except DbRefusal as exc:
        print(str(exc), file=sys.stderr)
        return 1

    default_start, default_end = _load_snapshot_window()
    start = args.start or default_start
    end = args.end or default_end

    with tempfile.TemporaryDirectory(prefix="nh-recount-") as tmp_dir:
        dest = Path(tmp_dir) / "readme_metrics_copy.db"
        _backup_to_temp_copy(db_path, dest)
        result = recount(dest, start=start, end=end)

    result["db_path"] = str(db_path)
    if args.as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_table(result)

    if result["population"] == 0:
        print(
            "refusing: empty population -- no attempts fall inside scope "
            "and window",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
