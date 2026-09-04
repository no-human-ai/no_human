"""Anonymous, opt-out usage telemetry (server-side events).

Default ON (`telemetry.enabled: true` — consent). The onboarding and Settings
toggles were removed (operator, 2026-08-26): the one opt-out now is
`config.yaml` `telemetry.enabled: false`. With no `telemetry.endpoint`
configured, server-side events post to PostHog's `/batch/` endpoint on
`telemetry.posthog_host` instead; setting `telemetry.endpoint` (the
first-party ingestion Lambda) always takes precedence over PostHog. When
enabled AND a destination resolves, a CLOSED allowlist of event kinds is
buffered to ``~/.no_human/telemetry-queue.jsonl``
and flushed in small batches by a daemon thread to that destination.
Everything is fail-open: a dead endpoint, a full disk or a malformed
queue line can never break a task run — the only exception `record` raises on
purpose is ``ValueError`` for an event kind or prop outside the allowlist,
because an unlisted event is a privacy bug, not an operational hiccup.

NEVER include: task ids, titles, repo names, paths, prompts, tokens. Props are
validated against `_ALLOWED_EVENTS` — kind AND prop names are closed sets.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Closed allowlist: event kind -> allowed prop names. Anything else raises.
_ALLOWED_EVENTS: dict[str, frozenset[str]] = {
    "app_started": frozenset(),
    "task_created": frozenset({"source"}),
    "task_completed": frozenset({"status", "duration_bucket", "attempts"}),
    "task_failed": frozenset({"category", "reason_category"}),
    "approve_clicked": frozenset(),
    "feature_used": frozenset({"name"}),
}

#: CLOSED enum of task_failed reason categories. COARSE ON PURPOSE: it says
#: which STAGE killed the task, never anything about the task. Never a
#: failure string, title, path, repo or free-form text.
FAILURE_REASON_CATEGORIES = frozenset({
    "budget_exhausted", "review_failed", "max_attempts",
    "infra", "tamper_blocked", "blocker_parked", "other",
})

# Value-level allowlist: (kind, prop) -> allowed VALUES, for props whose
# name alone is not enough to keep them privacy-safe (a free-form string
# would pass the prop-NAME check above). Checked in both `record()` and
# `_sendable()` — see their docstrings.
_ALLOWED_PROP_VALUES: dict[tuple[str, str], frozenset[str]] = {
    ("task_failed", "reason_category"): FAILURE_REASON_CATEGORIES,
}

# Mirror of the first-party Lambda's per-event validation. The Lambda
# rejects a batch WHOLESALE on one bad event and a rejected batch stays
# queued, so every check it enforces must be enforced here too or one bad
# line wedges all flushing behind invisible 400s: exact key set, allowlisted
# name, allowlisted prop KEYS, ts an int (not bool) in the Lambda's
# 2020-2100 band, and prop VALUES str<=128 / int<2^31 / bool. PostHog's
# `/batch/` is far more permissive, but the same filtering is kept for it
# too: it is the one enforcement of the privacy allowlist regardless of
# destination, and a line rejected by one destination should not ship to
# the other unfiltered.
_MIN_TS = 1577836800          # 2020-01-01, the Lambda's lower bound
_MAX_TS = 4102444800          # 2100-01-01, the Lambda's upper bound
_MAX_PROP_STR = 128


def _sendable(event: Any) -> bool:
    if not isinstance(event, dict) or not set(event) <= {"name", "ts", "props"}:
        return False
    name = event.get("name")
    # isinstance first: an unhashable name (list/dict from a corrupted line)
    # would make the allowlist lookup RAISE, and flush's fail-open except
    # would then retain the batch — the wedge again (review round 4).
    if not isinstance(name, str):
        return False
    allowed = _ALLOWED_EVENTS.get(name)
    if allowed is None:
        return False
    ts = event.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, int) or not _MIN_TS <= ts <= _MAX_TS:
        return False
    props = event.get("props", {})
    if not isinstance(props, dict) or not set(props) <= allowed:
        return False
    for prop, value in props.items():
        if isinstance(value, bool):
            pass
        elif isinstance(value, str):
            if len(value) > _MAX_PROP_STR:
                return False
        elif isinstance(value, int):
            if abs(value) > 2 ** 31:  # server: abs(value) > MAX_PROP_INT
                return False
        else:
            return False
        # Type shape confirmed (str/int/bool) before this membership check —
        # an unhashable value (list/dict from a corrupted line) would make
        # `in allowed_values` RAISE, and flush's fail-open except would then
        # retain the batch, the wedge the isinstance-first ordering above
        # already guards `name` against.
        allowed_values = _ALLOWED_PROP_VALUES.get((name, prop))
        if allowed_values is not None and value not in allowed_values:
            return False
    return True


MAX_QUEUE_LINES = 500   # bounded buffer; oldest lines dropped first
FLUSH_BATCH = 50        # max events per POST
_HTTP_TIMEOUT = 3.0

_LOCK = threading.Lock()


def _queue_path() -> Path:
    # Resolved per call (not module-level) so a temp-HOME test suite never
    # touches the operator's real ~/.no_human.
    return Path.home() / ".no_human" / "telemetry-queue.jsonl"


def duration_bucket(minutes: float) -> str:
    """Bucket a task duration so no precise timing ever leaves the machine."""
    if minutes < 10:
        return "<10m"
    if minutes < 30:
        return "10-30m"
    if minutes < 60:
        return "30-60m"
    return ">60m"


# Coarse mapping from a `blockers.taxonomy.BlockerCategory` NAME (plain
# string — this module must not import `blockers.taxonomy`) to a
# FAILURE_REASON_CATEGORIES value. Anything not listed here is "other".
_BLOCKER_FAILURE_CATEGORY = {
    "BUDGET_EXHAUSTED": "budget_exhausted",
    "TRANSIENT_INFRA": "infra",
    "QUOTA": "infra",
    "DEPENDENCY_WAIT": "blocker_parked",
    "USER_PAUSED": "blocker_parked",
    "STAGNATION": "review_failed",
    "MISSING_ACCESS": "infra",
    # AMBIGUITY / SCOPE_EXPLOSION / IMPOSSIBLE / NOVEL_UNKNOWN -> "other"
}


def failure_reason_category(explicit: str | None = None,
                            blocker_category: str | None = None) -> str:
    """Coarse category for a terminal failure. LOOKUP ONLY — never parses a
    failure string. Anything unrecognised is "other". Never raises (callers
    are in a fail-open path).

    `explicit` (an already-known internal category name, e.g. "infra") wins
    when it is itself a valid enum value; otherwise falls back to mapping
    `blocker_category` (a `BlockerCategory.name`, e.g. "BUDGET_EXHAUSTED")
    through the table above; otherwise "other".
    """
    if explicit in FAILURE_REASON_CATEGORIES:
        return explicit
    return _BLOCKER_FAILURE_CATEGORY.get(blocker_category or "", "other")


def _conf(config: dict[str, Any] | None) -> dict[str, Any]:
    if config is None:
        from .config import load_config
        config = load_config().data
    section = config.get("telemetry") or {}
    return section if isinstance(section, dict) else {}


def _destination(section: dict[str, Any]) -> tuple[str, str] | None:
    """Resolve where a batch ships, or ``None`` if nothing is configured.

    An explicit `telemetry.endpoint` (the first-party ingestion Lambda)
    always wins when set. Otherwise, PostHog's `/batch/` endpoint on
    `telemetry.posthog_host` is the shipped default — used whenever both a
    host and a publishable client token are present.
    """
    endpoint = str(section.get("endpoint") or "").strip()
    if endpoint:
        return ("lambda", endpoint)
    publishable = str(section.get("posthog_publishable") or "").strip()
    host = str(section.get("posthog_host") or "").strip().rstrip("/")
    if publishable and host:
        return ("posthog", f"{host}/batch/")
    return None


def enabled(config: dict[str, Any] | None = None) -> bool:
    section = _conf(config)
    return bool(section.get("enabled")) and _destination(section) is not None


def record(kind: str, config: dict[str, Any] | None = None, **props: Any) -> None:
    """Queue one telemetry event. No-op unless consented AND a destination
    resolves (`telemetry.endpoint`, else PostHog).

    Raises ``ValueError`` for a kind or prop name outside `_ALLOWED_EVENTS`,
    or a prop VALUE outside `_ALLOWED_PROP_VALUES` for props with a closed
    value enum (validated even when disabled — an unlisted event/value is a
    bug either way). Every other failure is swallowed: telemetry must never
    break the caller.
    """
    allowed = _ALLOWED_EVENTS.get(kind)
    if allowed is None:
        raise ValueError(f"telemetry: unknown event kind {kind!r}")
    unknown = set(props) - set(allowed)
    if unknown:
        raise ValueError(
            f"telemetry: props {sorted(unknown)!r} not allowed for {kind!r}")
    for prop, value in props.items():
        allowed_values = _ALLOWED_PROP_VALUES.get((kind, prop))
        if allowed_values is None:
            continue
        # `value in allowed_values` on an unhashable value (a caller passing
        # a list/dict by mistake) would raise TypeError, not the ValueError
        # this validation promises — fail the same way either way.
        try:
            ok = value in allowed_values
        except TypeError:
            ok = False
        if not ok:
            raise ValueError(
                f"telemetry: value {value!r} not allowed for "
                f"{kind!r}.{prop!r}")
    try:
        section = _conf(config)
        if not (bool(section.get("enabled")) and _destination(section)):
            return
        # "name" is the QUEUE-LINE field (this on-disk contract, pinned by
        # test) — not the PostHog wire field, which is "event" (see
        # `_posthog_body`, :231). For the Lambda, "name" IS the wire field
        # too: found live 2026-08-16, the first deploy sent "kind" and every
        # batch got a 400; the queue never drained.
        event = {"name": kind, "ts": int(time.time()), "props": props}
        _append(event)
        _spawn_flush(section)
    except ValueError:
        raise
    except Exception:
        return  # fail-open


def _append(event: dict[str, Any]) -> None:
    path = _queue_path()
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        if path.exists():
            lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
        lines.append(json.dumps(event, separators=(",", ":")))
        if len(lines) > MAX_QUEUE_LINES:
            lines = lines[-MAX_QUEUE_LINES:]  # drop-oldest
        path.write_text("\n".join(lines) + "\n")


def _spawn_flush(section: dict[str, Any]) -> None:
    threading.Thread(
        target=flush, args=(section,), name="nh-telemetry-flush", daemon=True,
    ).start()


def ensure_instance_id(section: dict[str, Any]) -> str:
    """The first-party Lambda 400s the WHOLE batch unless instance_id is a
    uuid4; PostHog accepts any `distinct_id`, canonical or not.

    Config may hold no id, a hand-edited non-uuid4 value, or a valid one in a
    non-canonical form — this validates, canonicalizes, and mints +
    best-effort-persists when needed: the batch always ships with a valid id;
    persistence keeps it stable across runs. Public because both the Lambda
    and PostHog code paths need it, and callers may want to ensure an id
    exists before enabling telemetry.
    """
    import uuid
    raw = str(section.get("instance_id") or "")
    try:
        parsed = uuid.UUID(raw)
        if parsed.version == 4:
            # CANONICALIZED: uuid.UUID also accepts braced/dashless/urn
            # forms, which the Lambda's exact-36-char check rejects — a
            # hand-edited id in any of those forms would wedge every batch
            # while validating fine here (review round 3).
            return str(parsed)
    except Exception:
        pass
    minted = str(uuid.uuid4())
    section["instance_id"] = minted  # this process reuses it even if persist fails
    try:
        from .config import CONFIG_PATH
        from .integrations import _write_config_values
        _write_config_values(CONFIG_PATH, {"telemetry.instance_id": minted})
    except Exception as exc:  # noqa: BLE001
        # fail-open: an unpersisted id still beats a wedged queue — but warn
        # once so a persistently-failing config.yaml write is visible instead
        # of silently re-minting a new id (and re-triggering this) every run.
        log.warning(
            "telemetry: could not persist instance id to config.yaml: %s", exc)
    return minted


def _posthog_body(section: dict[str, Any], events: list[dict[str, Any]],
                   version: str) -> dict[str, Any]:
    """Build a PostHog `/batch/` request body from already-validated events."""
    instance_id = ensure_instance_id(section)
    batch = [{
        "event": ev["name"],
        "distinct_id": instance_id,
        "timestamp": datetime.fromtimestamp(ev["ts"], timezone.utc).isoformat(),
        "properties": {
            **ev.get("props", {}),
            "app_version": version,
            "instance_id": instance_id,
        },
    } for ev in events]
    return {
        "api_key": str(section.get("posthog_publishable") or ""),
        "batch": batch,
    }


def flush(section: dict[str, Any] | None = None,
          config: dict[str, Any] | None = None) -> int:
    """POST up to `FLUSH_BATCH` queued events to the resolved destination.

    Returns the number of events actually sent (0 on any failure — the queue
    keeps them for a later flush; fail-open, 3s timeout, stdlib urllib only).
    """
    if section is None:
        section = _conf(config)
    dest = _destination(section)
    if not (bool(section.get("enabled")) and dest):
        return 0
    kind, endpoint = dest
    try:
        path = _queue_path()
        with _LOCK:
            if not path.exists():
                return 0
            lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
            batch_lines = lines[:FLUSH_BATCH]
        if not batch_lines:
            return 0
        events = []
        for ln in batch_lines:
            try:
                event = json.loads(ln)
            except json.JSONDecodeError:
                continue  # a corrupt line is dropped, never re-sent forever
            if "name" not in event and "kind" in event:
                # Queue lines written by the first release used "kind";
                # normalize on the way out so they drain instead of 400ing.
                event["name"] = event.pop("kind")
            # The Lambda rejects a batch WHOLESALE on one bad event, and a
            # rejected batch stays queued — one poisoned line would block
            # every later flush until drop-oldest eviction. Non-conforming
            # events are dropped here like corrupt JSON lines are.
            if not _sendable(event):
                continue
            events.append(event)
        if not events:
            # Every line in this batch was corrupt/non-conforming. The Lambda
            # 400s an empty events array, and an empty PostHog batch is
            # equally pointless to send; lines are only removed after a
            # successful POST — so POSTing here would wedge the queue behind
            # the poisoned head forever (review round 4: organically reachable
            # via >=50 reset-clock lines). Delete the batch, send nothing.
            with _LOCK:
                current = []
                if path.exists():
                    current = [ln for ln in path.read_text().splitlines()
                               if ln.strip()]
                dropped = set(batch_lines)
                kept = [ln for ln in current if ln not in dropped]
                path.write_text("\n".join(kept) + "\n" if kept else "")
            return 0
        from . import __version__  # the same string `nh --version` prints
        if kind == "posthog":
            body = json.dumps(_posthog_body(section, events, __version__)).encode()
        else:
            # The deployed Lambda's server-side allowlist has not shipped
            # `reason_category` yet (client-side pin:
            # test_client_allowlist_matches_the_deployed_lambda_contract) and
            # rejects a batch WHOLESALE on an unknown prop key — a rejected
            # batch stays queued, so sending it unguarded would wedge the
            # queue of every fleet with `telemetry.endpoint` set until the
            # server ships. Strip it from a COPY of each event's props on
            # every build (lines are only removed after a successful POST,
            # so a retried batch re-runs this strip) — never mutate the
            # queued events themselves. PostHog (the shipped default) is
            # unaffected and gets the full prop set.
            lambda_events = [
                {**ev, "props": {k: v for k, v in ev.get("props", {}).items()
                                 if k != "reason_category"}}
                for ev in events
            ]
            body = json.dumps({
                "instance_id": ensure_instance_id(section),
                "version": __version__,
                "events": lambda_events,
            }).encode()
        import urllib.request
        req = urllib.request.Request(
            endpoint, data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT):
            pass
        with _LOCK:
            # Only what we sent is removed; anything queued meanwhile stays.
            current = []
            if path.exists():
                current = [ln for ln in path.read_text().splitlines() if ln.strip()]
            sent = set(batch_lines)
            kept = [ln for ln in current if ln not in sent]
            if kept:
                path.write_text("\n".join(kept) + "\n")
            else:
                path.write_text("")
        return len(events)
    except Exception:
        return 0  # fail-open: events stay queued
