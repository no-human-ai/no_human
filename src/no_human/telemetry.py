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

Every event also carries `environment` (`real`/`bench`/`test`/`ci`/`dev`),
classified fresh on each `record()` call by `environment()` — never
suppressing an event, only tagging it so real installs are countable amid
dogfood volume. `ensure_instance_id` mints and persists a real uuid4 ONLY for
`real`/`dev` context (an actual install / a developer's own checkout); the
`bench`/`test`/`ci` contexts instead reuse a fixed per-environment sentinel
uuid4 from `_ENV_SENTINEL_IDS` so every bench run / pytest run / CI job
collapses onto a handful of ids instead of minting a fresh one every time.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Closed allowlist: event kind -> allowed prop names. Anything else raises.
_ALLOWED_EVENTS: dict[str, frozenset[str]] = {
    "app_started": frozenset({"environment"}),
    "task_created": frozenset({"source", "environment"}),
    "task_completed": frozenset({"status", "duration_bucket", "attempts", "environment"}),
    "task_failed": frozenset({"category", "environment"}),
    "approve_clicked": frozenset({"environment"}),
    "feature_used": frozenset({"name", "environment"}),
}

# Recognized CI platform markers (intake-resolved: covers ~95% of CI
# deployments; more can be added here without touching the classification
# logic in `environment()`).
_CI_MARKERS = ("GITHUB_ACTIONS", "GITLAB_CI", "CIRCLECI", "TRAVIS", "JENKINS_HOME")

_VALID_ENVIRONMENTS = frozenset({"real", "bench", "test", "ci", "dev"})

# Fixed, hand-written, canonical version-4 uuids — one per non-"real"/"dev"
# environment — so bench/test/ci runs collapse onto a handful of stable ids
# instead of each minting (and persisting) a fresh uuid4. Never written to a
# real user's config.yaml (see `ensure_instance_id`).
_ENV_SENTINEL_IDS = {
    "bench": "b0000000-0000-4000-8000-000000000001",
    "test": "7e570000-0000-4000-8000-000000000002",
    "ci": "c1000000-0000-4000-8000-000000000003",
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
    for value in props.values():
        if isinstance(value, bool):
            continue
        if isinstance(value, str):
            if len(value) > _MAX_PROP_STR:
                return False
        elif isinstance(value, int):
            if abs(value) > 2 ** 31:  # server: abs(value) > MAX_PROP_INT
                return False
        else:
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


def _is_source_checkout() -> bool:
    """True if this module is running from a git/pyproject source tree
    rather than an installed package (site-packages/wheel) — a `.git`
    directory or `pyproject.toml` within a few parents of this file."""
    try:
        here = Path(__file__).resolve()
        for parent in list(here.parents)[:6]:
            if (parent / ".git").exists() or (parent / "pyproject.toml").exists():
                return True
    except OSError:
        pass
    return False


def _is_throwaway_home() -> bool:
    """True if HOME looks like a disposable sandbox — a pytest tmp dir, a
    CI runner's ephemeral home, or a git worktree checkout — rather than a
    real user's persistent machine."""
    try:
        home = str(Path.home())
        tmpdir = os.environ.get("TMPDIR", "")
        if home.startswith("/tmp") or home.startswith("/var/tmp"):
            return True
        if tmpdir and home.startswith(tmpdir):
            return True
        if "pytest-of-" in home:
            return True
        if ".worktree" in home or "worktrees" in Path(home).parts:
            return True
    except OSError:
        pass
    return False


def environment() -> str:
    """Classify the running process for telemetry attribution.

    Evaluated fresh on every call (never cached), so a monkeypatched env var
    in a test takes effect immediately. Precedence, first match wins:

    1. `NH_ENV`, if set to a recognized value — an explicit self-declaration
       (e.g. the `bench` CLI group sets `NH_ENV=bench`).
    2. `PYTEST_CURRENT_TEST` in the environment -> "test" (pytest sets this
       natively for the duration of every test).
    3. A recognized CI platform marker (`_CI_MARKERS`) -> "ci".
    4. A source checkout (`.git`/`pyproject.toml` near this file) running
       under a throwaway HOME (a sandbox/worktree, not a real machine)
       -> "dev".
    5. Otherwise -> "real".

    Fail-open: any probe error falls through toward "real" rather than
    raising — telemetry must never break the caller.
    """
    try:
        forced = os.environ.get("NH_ENV", "")
        if forced in _VALID_ENVIRONMENTS:
            return forced
        if "PYTEST_CURRENT_TEST" in os.environ:
            return "test"
        if any(os.environ.get(marker) for marker in _CI_MARKERS):
            return "ci"
        if _is_source_checkout() and _is_throwaway_home():
            return "dev"
    except Exception:
        pass
    return "real"


def record(kind: str, config: dict[str, Any] | None = None, **props: Any) -> None:
    """Queue one telemetry event. No-op unless consented AND a destination
    resolves (`telemetry.endpoint`, else PostHog).

    Raises ``ValueError`` for a kind or prop name outside `_ALLOWED_EVENTS`
    (validated even when disabled — an unlisted event is a bug either way).
    Every other failure is swallowed: telemetry must never break the caller.

    Stamps `environment` (see `environment()`) onto every event unless the
    caller already passed one explicitly — never suppresses an event, only
    tags it.
    """
    allowed = _ALLOWED_EVENTS.get(kind)
    if allowed is None:
        raise ValueError(f"telemetry: unknown event kind {kind!r}")
    unknown = set(props) - set(allowed)
    if unknown:
        raise ValueError(
            f"telemetry: props {sorted(unknown)!r} not allowed for {kind!r}")
    try:
        section = _conf(config)
        if not (bool(section.get("enabled")) and _destination(section)):
            return
        props = {**props}
        props.setdefault("environment", environment())
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

    In `bench`/`test`/`ci` contexts (see `environment()`), a fresh uuid4
    would mint on EVERY run — every pytest run, every bench replay, every CI
    job — drowning real installs and making `instance_id` uncountable. Those
    contexts instead reuse a fixed per-environment sentinel from
    `_ENV_SENTINEL_IDS` and it is never persisted to config.yaml (persisting
    a sentinel into a real user's config would permanently make that install
    uncountable). `real`/`dev` contexts keep today's mint-and-persist path
    unchanged.
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
    env = environment()
    sentinel = _ENV_SENTINEL_IDS.get(env)
    if sentinel is not None:
        section["instance_id"] = sentinel  # process-local only; never persisted
        return sentinel
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


def _strip_environment(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop `environment` from each event's props for the Lambda wire path.

    The deployed ingestion Lambda validates prop KEYS against its own closed
    allowlist and 400s the batch WHOLESALE on an unknown key; `environment`
    is consumed by PostHog only until the server-side allowlist ships it too.
    Shallow-copies — never mutates the queued event dicts (they may still be
    re-flushed to a different destination, or re-read, after this call).
    """
    return [{**ev, "props": {k: v for k, v in ev.get("props", {}).items()
                              if k != "environment"}} for ev in events]


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
            body = json.dumps({
                "instance_id": ensure_instance_id(section),
                "version": __version__,
                "events": _strip_environment(events),
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
