"""Replay a recorded attempt's events through the REAL stuck detector.

`replay.py` (the golden-task harness) runs no_human end-to-end against a
sandboxed repo. This module does something narrower and cheaper: it takes
the event stream a PAST attempt already produced — persisted verbatim in
`task_events` while the attempt ran — and replays it through today's
`StuckDetector`/`drive_stuck_detector` (`..core.stuck_drive`) to see
whether a change to the advisory/hard-tier logic or a loop signal would fire
differently on real history. That is the only way changes to this kind of
event-driven logic get checked against real runs before they land: a
proposed change to the edit-loop hard tier once passed review even though it
would have stopped 20 of 26 recorded hard aborts from firing, and replaying
those runs is what would have caught it.

Two traps decide whether a replay like this means anything at all:

TRAP 1 — the detector is driven from raw event fields (tool_use_id,
exit_code, result_chars, edit payloads, tool_input), not just tool names.
`drive_stuck_detector` is IMPORTED from `..core.stuck_drive`, not
reimplemented here, specifically so this module can never drift from what
`Orchestrator._agent_sink` actually calls in production (it imports the
same function from the same module) — a hand-rolled
replica that only sees tool names cannot reproduce the edit-loop progress
gate (`record_edit` / `note_test_run` / `record_test_outcome`) behind the
hard tiers.

TRAP 2 — the repo root the detector gates edit paths against
(`is_agent_owned` / `is_outside_repo`) is NOT a recorded field. With
worktree isolation on (the default), it is the per-task worktree directory
— `<worktree_root>/<task_id>.<owner_pid>.<token>` (see
`no_human.config.worktree_root`) — not `tasks.repo_path`. Recover it from
the worktree-prefix of the attempt's own recorded edit paths
(`_recover_repo_root`); fall back to `tasks.repo_path` only when no such
prefix is found (isolation off). Get this wrong and every edit reads as
agent-owned, silently disabling both guards and making every edit-loop fire
in this replay unreproducible.

DATABASE READING: `load_attempt_events` opens the source file `mode=ro` (no
`immutable=1`) and copies it through SQLite's online backup API into an
in-memory snapshot before reading anything — see `_open_snapshot` for why: a
plain file copy opened `immutable=1` ignores an un-checkpointed `-wal`
sidecar and silently drops every event written since the last checkpoint,
which for a live no_human deployment is usually most of the recent history.

LIMIT: this replay can only guard logic driven by RECORDED events — tool
names, tool_use_id, exit_code, result_chars, edit payloads, emitted kinds.
It cannot see prompts, tool-result text (only its status/length are
recorded, by design — see `_test_run_summary` in `..core.stuck_drive`),
worktree state, or collected test ids. A change to logic that reads any of
those is not covered by this module, at all.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from json import loads as _json_loads
from pathlib import Path
from typing import Any, Iterable

from ..agent.backend import AgentEvent
from ..core.bounds import StuckDetector
from ..core.stuck_drive import drive_stuck_detector

#: `data` JSON blob keys that are NOT part of `AgentEvent.meta` — see the
#: shape comment on `Orchestrator._agent_sink`'s persisted-row dict:
#: ``{"source": role, "kind": ..., "text": ..., "tool_name": ...,
#: "tool_input": {...}, **event.meta, "ts": time.time(), "task_id": ...}``.
_ROW_ONLY_KEYS = frozenset({"source", "kind", "text", "tool_name", "tool_input", "ts", "task_id"})

#: `_TASK_END_KINDS`-adjacent map from a recorded event `kind` to a
#: `classify_outcome` label, mirroring the literal kind strings
#: `Orchestrator` actually emits (`self.emit("paused_quota", ...)`,
#: `self.emit("escalated", ...)`, `self.emit("cancelled_hard", ...)`,
#: `self.emit("blocked", ...)`, `self.emit("cancelled", ...)` — the
#: cooperative pause `_honor_cancel` emits, deliberately NOT in
#: `_TASK_END_KINDS` because a pause is resumable, not an end — and
#: `self.emit("commit", ...)`). Order doesn't matter here: `classify_outcome`
#: takes the LAST kind seen that's in this map, not the first.
_OUTCOME_BY_KIND = {
    "paused_quota": "paused_quota",
    "escalated": "escalated",
    "blocked": "blocked",
    "cancelled": "cancelled",
    "cancelled_hard": "cancelled",
    "commit": "committed",
}

_DIED = "died"


def _open_snapshot(db_path: str) -> sqlite3.Connection:
    """Point-in-time, read-only snapshot of *db_path* via SQLite's online
    backup API — the safe way to read a database a live no_human process may
    still be writing to.

    Opens the SOURCE `mode=ro` (deliberately NOT `immutable=1` on a plain
    file copy — `immutable=1` skips SQLite's normal WAL-aware open sequence,
    so any writes already checkpointed into a `-wal` sidecar next to the
    source and not yet folded into the main `.db` file are silently
    invisible). `mode=ro` alone still does a normal WAL-aware read, and the
    backup API then copies the fully merged, transactionally consistent
    result into an in-memory destination nothing else can write to for the
    rest of this process — equivalent in effect to copying the `.db` file
    together with its `-wal`/`-shm` sidecars and opening that copy `mode=ro`,
    without needing the caller to manage those sidecar files itself.
    """
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    try:
        dst = sqlite3.connect(":memory:")
        src.backup(dst)
    finally:
        src.close()
    dst.row_factory = sqlite3.Row
    return dst


def _parse_started_at(value: str) -> float:
    """`attempts.started_at` is `TEXT DEFAULT (datetime('now'))`
    (migrations/0001_init.sql) — a plain SQLite `datetime('now')`-format UTC
    string, e.g. ``"2026-09-18 12:34:56"``. This is NOT an epoch float like
    `task_events.ts` (a REAL column written via `time.time()`); it must be
    parsed and converted before the two are compared.
    """
    return (
        datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


def _to_agent_event(row: dict[str, Any]) -> AgentEvent:
    """Reconstruct the `AgentEvent` `drive_stuck_detector` expects from a
    persisted `task_events.data` row, undoing the flattening
    `Orchestrator._agent_sink` did on the way in (see `_ROW_ONLY_KEYS`)."""
    meta = {k: v for k, v in row.items() if k not in _ROW_ONLY_KEYS}
    return AgentEvent(
        kind=row.get("kind") or "",
        text=row.get("text") or "",
        tool_name=row.get("tool_name"),
        tool_input=row.get("tool_input"),
        meta=meta,
    )


def _build_attempt_windows(
    attempt_rows: Iterable[tuple[str, str]], events: list[dict[str, Any]]
) -> dict[str, tuple[float, float | None]]:
    """Acceptance criterion 5: window each attempt of a task SEPARATELY,
    without ever relying on `attempts.attempt_number` (confirmed unreliable
    as an ordering key — `Store.latest_open_attempt`'s docstring records a
    real case where a lower attempt_number was written 15 minutes after a
    higher one).

    An attempt's window starts at the LAST recorded `attempt_start` event
    whose `ts` is at or before that attempt row's `started_at` plus one
    second (`Orchestrator.emit("attempt_start", f"attempt {n}/{max}", ...)`
    — the `n` counter in the event text RESTARTS after a resume, which is
    exactly why this never parses that text, only compares `ts`), and ends
    at the next attempt's own window start (attempts ordered by their
    parsed `started_at`, not by row id or attempt_number). The last attempt
    of a task has an open-ended window (`None`).

    *attempt_rows* is an iterable of ``(attempt_id, started_at_text)``.
    Pure / DB-free so it's directly unit-testable without a fixture DB.
    """
    parsed = sorted(
        ((attempt_id, _parse_started_at(started_at)) for attempt_id, started_at in attempt_rows),
        key=lambda pair: pair[1],
    )
    if not parsed:
        return {}
    attempt_start_ts = sorted(e["ts"] for e in events if e.get("kind") == "attempt_start")
    starts: list[float] = []
    for _attempt_id, started_at_ts in parsed:
        cutoff = started_at_ts + 1.0
        candidates = [t for t in attempt_start_ts if t <= cutoff]
        # Fall back to the row's own started_at when no attempt_start event
        # qualifies (e.g. a truncated/legacy event log) rather than raising —
        # a best-effort window beats no window.
        starts.append(candidates[-1] if candidates else started_at_ts)
    windows: dict[str, tuple[float, float | None]] = {}
    for i, (attempt_id, _started_at_ts) in enumerate(parsed):
        end_ts = starts[i + 1] if i + 1 < len(parsed) else None
        windows[attempt_id] = (starts[i], end_ts)
    return windows


def _recover_repo_root(task_id: str, events: list[dict[str, Any]], fallback: str) -> str:
    """TRAP 2: recover the repo root `drive_stuck_detector` must gate edit
    paths against. Not a recorded field — with worktree isolation on (the
    default) it's the per-task worktree
    (`<worktree_root>/<task_id>.<owner_pid>.<token>`, see
    `no_human.config.worktree_root`), not `tasks.repo_path`.

    Scans the attempt's own recorded edit-tool paths for a path component
    starting with ``f"{task_id}."`` and truncates there. Falls back to
    *fallback* (`tasks.repo_path`) only when no such component is found,
    i.e. isolation was off for this attempt.
    """
    marker = f"{task_id}."
    for ev in events:
        if ev.get("kind") != "tool_use":
            continue
        if ev.get("tool_name") not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            continue
        inp = ev.get("tool_input") or {}
        path = inp.get("file_path") or inp.get("path") or inp.get("notebook_path") or ""
        if not path:
            continue
        parts = Path(str(path)).parts
        for i, part in enumerate(parts):
            if part.startswith(marker):
                return str(Path(*parts[: i + 1]))
    return fallback


@dataclass
class AttemptReplay:
    """One attempt's windowed event stream, bundled with what
    `drive_stuck_detector` needs beyond the events themselves — see TRAP 2.
    """

    attempt_id: str
    task_id: str
    repo_root: str
    events: list[dict[str, Any]]


def load_attempt_events(db_uri: str, attempt_id: str) -> AttemptReplay:
    """Load one attempt's windowed, chronological event stream from
    *db_uri* (a filesystem path to a no_human `.db`), read via a safe
    online-backup snapshot (`_open_snapshot`) so a live writer can never
    race this read and an un-checkpointed WAL can never be silently
    dropped.

    Returns an `AttemptReplay` bundling the windowed events (acceptance
    criterion 5) with the recovered `repo_root` (TRAP 2) — both are needed
    to drive `drive_stuck_detector` faithfully; `classify_outcome` only
    needs `.events`.
    """
    conn = _open_snapshot(db_uri)
    try:
        arow = conn.execute(
            "SELECT task_id, started_at FROM attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if arow is None:
            raise KeyError(f"no attempt {attempt_id!r} in {db_uri!r}")
        task_id = arow["task_id"]
        trow = conn.execute("SELECT repo_path FROM tasks WHERE id = ?", (task_id,)).fetchone()
        repo_path = (trow["repo_path"] if trow is not None else None) or ""
        attempt_rows = [
            (row["id"], row["started_at"])
            for row in conn.execute(
                "SELECT id, started_at FROM attempts WHERE task_id = ?", (task_id,)
            ).fetchall()
        ]
        event_rows = conn.execute(
            "SELECT ts, data FROM task_events WHERE task_id = ? ORDER BY ts, id", (task_id,)
        ).fetchall()
    finally:
        conn.close()

    events: list[dict[str, Any]] = []
    for row in event_rows:
        ev = _json_loads(row["data"])
        ev["ts"] = row["ts"]  # the REAL column, authoritative over any copy in the JSON blob
        events.append(ev)

    windows = _build_attempt_windows(attempt_rows, events)
    if attempt_id not in windows:
        raise KeyError(f"attempt {attempt_id!r} has no computable window in {db_uri!r}")
    start_ts, end_ts = windows[attempt_id]
    windowed = [
        e for e in events
        if start_ts <= e["ts"] and (end_ts is None or e["ts"] < end_ts)
    ]

    repo_root = _recover_repo_root(task_id, windowed, repo_path)
    return AttemptReplay(attempt_id=attempt_id, task_id=task_id, repo_root=repo_root, events=windowed)


def classify_outcome(events: Iterable[dict[str, Any]]) -> str:
    """Classify one attempt's windowed event stream (chronological; internal
    windowing means the caller does NOT need to pre-filter to only the
    "significant" events — this scans the whole stream itself) into one of:
    ``paused_quota`` / ``escalated`` / ``blocked`` / ``cancelled`` /
    ``committed`` / ``died``.

    The label is the LAST significant event's outcome — mirroring what
    motivated this module: per-attempt metrics today mislabel ~100 attempt
    rows tagged 'interrupted: superseded' in a 30-day window, when the last
    significant event before the next attempt was actually a quota pause
    (33), a commit (21-22), an escalation (19), nothing (17, i.e. `died`
    here), a cancel (7), or a block (5). An attempt with no significant
    event in its window is ``died`` — it ended without any of the outcomes
    above ever firing.
    """
    last_outcome: str | None = None
    for ev in events:
        outcome = _OUTCOME_BY_KIND.get(ev.get("kind"))
        if outcome is not None:
            last_outcome = outcome
    return last_outcome or _DIED


def _signal_name(reason: str) -> str:
    """`doom-loop: ...` / `edit-loop: ...` / `ping-pong: ...` -> the part
    before the first `: ` — every advisory and hard reason string from
    `StuckDetector` (`bounds.py`'s `stuck_reason`/`hard_stuck_reason`) uses
    this same `"{signal}: {detail}"` shape."""
    return reason.split(":", 1)[0]


def replay_fires(
    events: Iterable[dict[str, Any]], detector: StuckDetector, *, repo_root: str = ""
) -> set[str]:
    """Feed *events* (chronological — `AttemptReplay.events`, or any
    equivalent stream) through the exact production entry point
    (`drive_stuck_detector`, imported from `..core.stuck_drive` — see TRAP
    1 in this module's docstring for why that matters) against *detector*,
    and collect the SET of fire identifiers observed across the whole
    stream: ``"advisory:doom-loop"``, ``"advisory:edit-loop"``,
    ``"advisory:ping-pong"``, ``"hard-abort:doom-loop"``,
    ``"hard-abort:edit-loop"``, ``"hard-abort:ping-pong"``.

    This is the replay half of `diff`: call it once against a detector built
    from today's thresholds and once against a detector built from a
    candidate change, then pass the two resulting sets to `diff`.
    """
    fires: set[str] = set()
    for row in events:
        event = _to_agent_event(row)
        advisories, hard = drive_stuck_detector(detector, event, repo_root=repo_root)
        for reason in advisories:
            fires.add(f"advisory:{_signal_name(reason)}")
        if hard:
            fires.add(f"hard-abort:{_signal_name(hard)}")
    return fires


def diff(before: set[str], after: set[str]) -> dict[str, set[str]]:
    """Compare two `replay_fires` results (typically: today's thresholds vs.
    a candidate change, replayed over the SAME event stream) and report
    what changed.

    Returns ``{"lost": ..., "new": ...}`` — each a SET of fire identifiers
    (`"advisory:..."` / `"hard-abort:..."`). ``lost`` is what fired in
    *before* but not *after* (a change that silences a real recorded fire —
    e.g. the edit-loop hard-tier regression this module exists to catch);
    ``new`` is the reverse.
    """
    before, after = set(before), set(after)
    return {"lost": before - after, "new": after - before}
