"""Builder for `testdata/event_replay_fixture.db`, the fixture
`tests/test_event_replay.py` replays through today's real `StuckDetector`
(`no_human.eval.event_replay`, acceptance criterion 1 of no_human#422).

Built through `Store.create_task`/`create_attempt`/`save_events` (real
product methods), not hand-written DDL, so the schema is the product's real
one, migrations included — same convention `tests/_memory_triage_fixture.py`
follows and explains at more length.

THIS IS A REAL RECORDED HARD-ABORT, NOT A HAND-WRITTEN APPROXIMATION. A
repo-wide sweep for `*fixture*` at intake found no existing event-replay
fixture checked in (only unrelated ones, e.g. `testdata/memory_triage_fixture.db`).
Rather than hand-write a synthetic event stream, `tests/_event_replay_fixture_events.json`
holds the ACTUAL recorded `attempt_start`/`tool_use`/`tool_result` events
of one real attempt of a real no_human task run on this machine — the one
whose live `task_events` table this repo's own `StuckDetector` fired an
edit-loop hard-abort against on 2026-09-16 (`src/no_human/core/orchestrator.py`
edited 15× with no observed progress). Extracted via the same safe
online-backup-snapshot method `event_replay._open_snapshot` uses (never a
plain-copy-plus-`immutable=1`, which would have silently dropped anything
still sitting in an un-checkpointed `-wal`), then reduced to only the event
`kind`s `drive_stuck_detector`/`classify_outcome`/windowing actually consume
(`attempt_start`, `tool_use`, `tool_result` — dropping `thinking`/`text`/
`usage`/etc., which the detector never reads and which is where any
free-text content would have lived) and passed through ONE global
find-and-replace of the real worktree path prefix
(``/Users/<realname>/.no_human/worktrees/<realtaskid>.<realpid>.<realtoken>``)
for a synthetic one (``/workspace/example-repo/.no_human/worktrees/__TASK_ID__.4242.deadbeef``,
with `__TASK_ID__` swapped for this fixture's own freshly generated task id
at build time, below) — command text, tool_use_id, exit_code (where
recorded — this real attempt's Bash results carry `is_error`/`result_chars`
instead, which `_test_run_summary` treats identically, see its docstring),
result_chars and every edit payload (`old_string`/`new_string`) are
otherwise byte-identical to what really fired. Verified (ad hoc, at
extraction time) that no other `/Users/...` prefix, email-shaped string or
occurrence of the real name/task id survives that replacement anywhere in
the 189-event file. `tasks.repo_path` below is likewise a synthetic
placeholder, never the real one.

WHAT IT WRITES: one task, one attempt, replaying that real event stream
verbatim (order and fields preserved) so it reproduces the SAME edit-loop
hard-abort under `StuckDetector`'s default thresholds (`edit_abort=15`) when
replayed through `drive_stuck_detector` (`no_human.core.orchestrator`). The
worktree-style prefix embedded in every edit path (see above) is what makes
TRAP 2 concrete: replaying with `repo_root=tasks.repo_path` (the wrong root)
makes every edit read as agent-owned (`is_agent_owned` sees the `.no_human`
path component) and none of them count, so no hard-abort fires; only
recovering `repo_root` from the worktree prefix of the edit paths themselves
(`event_replay._recover_repo_root`) reproduces the real fire.
`tests/test_event_replay.py` asserts both directions.

Regenerate (only needed if `tests/_event_replay_fixture_events.json`
changes — this script no longer touches the source machine's real database):

    uv run python tests/_event_replay_fixture.py testdata/event_replay_fixture.db
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from no_human.core.db import Store
from no_human.core.task import Task

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE_PATH = REPO_ROOT / "testdata" / "event_replay_fixture.db"
EVENTS_JSON_PATH = Path(__file__).resolve().parent / "_event_replay_fixture_events.json"

#: Titled/tagged so a test can find the row without depending on the
#: random id `Task.new`/`create_attempt` assign.
TASK_TITLE = "event replay fixture: real edit-loop hard-abort (anonymized)"

#: Synthetic, normalized — no local path or private name. `repo_path` never
#: appears in this fixture's edit paths (isolation was on for the real
#: attempt this was extracted from, exactly like production defaults), so
#: it is used only as `_recover_repo_root`'s never-taken fallback branch.
REPO_PATH = "/workspace/example-repo"

#: Must match the placeholder baked into `_event_replay_fixture_events.json`
#: at extraction time (see the module docstring).
TASK_ID_PLACEHOLDER = "__TASK_ID__"

#: The real attempt's own `started_at`, from the source database's
#: `attempts` row (`2026-09-16 04:59:48`, UTC — not a local path or a
#: private name, just a timestamp, so left unnormalized). `create_attempt`
#: only offers `datetime('now')`'s default, so this is stamped after the
#: fact — see `stamp_started_at`.
REAL_STARTED_AT = "2026-09-16 04:59:48"

#: The REAL next attempt's own `started_at` (source database's second
#: `attempts` row for the same task, `2026-09-16 05:22:23`, UTC). Written as
#: a second, EMPTY attempt row (no events of its own) purely so
#: acceptance-criterion-5 windowing has a real second boundary to close
#: attempt 1's window against — matching what really happened: the source
#: attempt's hard-abort event and this second attempt's own recorded
#: `attempt_start` event (ts ~1.8s apart) are both present, verbatim, inside
#: `_event_replay_fixture_events.json`'s 189 events, so without this second
#: row the window used by `load_attempt_events` for attempt 1 would stay
#: open-ended and (incorrectly, for test purposes) swallow that second
#: `attempt_start` event too.
REAL_SECOND_STARTED_AT = "2026-09-16 05:22:23"


def load_events(task_id: str) -> list[dict]:
    """Load `_event_replay_fixture_events.json` (see the module docstring
    for its provenance) and substitute *task_id* for the placeholder baked
    into the worktree-style edit paths and every event's `task_id` field."""
    raw = json.loads(EVENTS_JSON_PATH.read_text(encoding="utf-8"))
    events = []
    for ev in raw:
        text = json.dumps(ev).replace(TASK_ID_PLACEHOLDER, task_id)
        events.append(json.loads(text))
    return events


def stamp_started_at(db_path: Path, attempt_id: str, started_at_text: str) -> None:
    """Overwrite `attempts.started_at` on the working copy — `create_attempt`
    only offers the `datetime('now')` DEFAULT, and this fixture needs the
    REAL recorded value (`REAL_STARTED_AT`) so `tests/test_event_replay.py`
    can assert the acceptance-criterion-5 windowing math against it
    precisely (the fixture's `attempt_start` event's `ts` is ~0.8s after
    this, matching what was really recorded)."""
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute(
            "UPDATE attempts SET started_at = ? WHERE id = ?",
            (started_at_text, attempt_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"stamp_started_at: no attempt {attempt_id!r} in {db_path}")
        con.commit()
    finally:
        con.close()


async def build_fixture(path: Path) -> tuple[str, str]:
    """Write the fixture database at *path* (overwriting it if present).
    Returns ``(task_id, attempt_id)`` for the FIRST attempt (the one whose
    events are replayed and whose window closes against the second, empty
    attempt row's `started_at` — see `REAL_SECOND_STARTED_AT`)."""
    if path.exists():
        path.unlink()
    store = await Store(path).connect()
    try:
        task = Task.new(
            TASK_TITLE, source="freeform", repo_path=REPO_PATH,
            description=(
                "Fixture task: replays a real recorded attempt's events "
                "(anonymized) that hard-aborted on an edit-loop, to prove "
                "no_human.eval.event_replay reproduces it against today's "
                "StuckDetector. See tests/_event_replay_fixture.py."
            ),
        )
        task = await store.create_task(task)
        attempt_id = await store.create_attempt(task.id, 1)
        stamp_started_at(path, attempt_id, REAL_STARTED_AT)
        # A second, EMPTY attempt row — no events of its own — solely so
        # attempt 1's window closes at a real boundary instead of staying
        # open-ended (see REAL_SECOND_STARTED_AT).
        second_attempt_id = await store.create_attempt(task.id, 2)
        stamp_started_at(path, second_attempt_id, REAL_SECOND_STARTED_AT)

        events = load_events(task.id)
        await store.save_events(task.id, events)

        await store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        await store.close()

    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.commit()
    finally:
        con.close()
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()

    return task.id, attempt_id


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FIXTURE_PATH
    task_id, attempt_id = asyncio.run(build_fixture(target))
    print(f"wrote {target} (task_id={task_id}, attempt_id={attempt_id})")
