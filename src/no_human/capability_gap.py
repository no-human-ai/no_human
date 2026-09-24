"""Opt-IN, default-OFF capability-gap event sink (issue #20).

A capability gap is a bounded task attempt that could not proceed because
something the machine NEEDED was not there — a dead backend, a spent quota,
an access it does not hold, a budget it may not exceed. That is a different
fact from "the code it wrote did not pass the tests", and this channel is
deliberately blind to the second: an ordinary coding/test failure is not a
capability gap and is never emitted (`_CATEGORY_MAP` / `_REASON_MAP` below
name, one by one, what is and is not).

HOW THIS IS NOT `telemetry.py`. That module is a product-analytics channel,
default ON, shipping to a first-party endpoint or PostHog. This one is default
OFF, ships nowhere until an operator names a destination, carries its OWN
pseudonym (never telemetry's `instance_id`, so the two channels cannot be
joined by a recipient of either), and its payload is a structured *fact about
a capability*, not a usage event. They are kept apart on purpose; neither
enables the other, and ``telemetry.enabled`` has no effect on this channel.

WHAT CAN LEAVE, exhaustively. Each field is either a closed enum or a value
whose SHAPE is checked (`_sendable`) — there is no free-text field anywhere in
the event:

* ``event_id``     — a canonical uuid string (a fresh uuid4 when minted here).
* ``ts``           — an ISO-8601 UTC timestamp in `datetime.isoformat` form.
* ``capability_class`` — one of `CAPABILITY_CLASSES`.
* ``reason_code``  — one of `REASON_CODES`.
* ``constraints``  — keys from `CONSTRAINT_VALUES`, each value from that
  key's own closed enum.
* ``source``       — the literal ``"no_human"``, `no_human.__version__`, and
  this install's pseudonym (`instance_pseudonym`); version and pseudonym must
  match `_TOKEN_RE` (1-64 of ``A-Za-z0-9._+-``), so neither can carry a path,
  a space or a URL.
* ``synthetic``    — true for anything not running on a real install.

`record` RAISES ``ValueError`` for a class, reason, constraint key or
constraint value outside those sets — even when the channel is disabled. An
unlisted field is a privacy bug, not an operational hiccup, and that is the
same posture `telemetry.record` takes for the same reason. Everything else is
fail-open: a full disk, a dead endpoint or a malformed spool line can never
break a task run, and a spool line that fails `_sendable` is dropped, never
sent and never left to block the lines after it.

TWO SINKS, one spool. The spool is ALWAYS a file named `SPOOL_NAME` inside
``capability_gap.dir`` (default ``~/.no_human``) — the directory is
configurable, the filename is not, so no setting can point the compaction or
flush rewrite at a file this module did not create. `"jsonl"` makes that file
the sink itself — nothing leaves the machine, a local consumer tails it.
`"http"` makes the same file a spool that a daemon thread drains in batches
to ``capability_gap.endpoint``, one flush in flight at a time. The endpoint must be ``https://`` (or
``http://`` on loopback); anything else — ``file://`` above all — is refused
by `_valid_endpoint`, so a mistyped URL cannot turn this into a local-file
writer pointed wherever the string says.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import urllib.parse
import uuid
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: The wire contract's name and version. A consumer keys its parser on this;
#: any change to the field set below is a new version, never a silent widening.
SCHEMA = "no_human.capability_gap/v1"

#: The seven coarse capability classes proposed in issue #20. Closed set.
#:
#: `verification_unavailable` is the one class the built-in orchestrator
#: derivation (`observe`) cannot currently produce,
#: and `tests/test_capability_gap.py` pins that gap rather than leaving it
#: implied: a review gate that could not RUN is reported through the ordinary
#: TRANSIENT_INFRA / NOVEL_UNKNOWN blockers, which are indistinguishable at
#: the hook from any other infra death. It stays in the vocabulary because the
#: vocabulary is the published boundary, and `record` accepts it from any
#: producer that CAN tell the difference.
CAPABILITY_CLASSES = frozenset({
    "capability_unavailable",
    "candidate_rejected",
    "fallback_selected",
    "capacity_blocked",
    "authorization_blocked",
    "verification_unavailable",
    "budget_exhausted",
})

#: Closed set of machine-readable reason codes. Never free text.
REASON_CODES = frozenset({
    "backend_unavailable",
    "dependency_unavailable",
    "quota_exhausted",
    "rate_limited",
    "timeout",
    "access_denied",
    "scope_constraint",
    "infeasible_request",
    "ambiguous_requirement",
    "no_progress",
    "budget_exhausted",
    "review_unavailable",
})

#: Task kinds, mirroring the vocabulary `telemetry.record`'s `task_created`
#: normalizes onto. Duplicated rather than imported so this channel's wire
#: contract cannot be widened by an edit made for the other one's.
TASK_KINDS = frozenset({
    "feature", "bugfix", "ci_fix", "traceability", "test_gap", "unknown",
    "other",
})

#: How a bounded attempt stopped. Derived from the emit kind, never from prose.
OUTCOMES = frozenset({"failed", "escalated", "parked", "needs_answer"})

#: Bucketed attempt counts — never the precise number.
ATTEMPT_BUCKETS = frozenset({"0", "1", "2-5", "6+"})

#: Coding backends (`worker.backend`, `agent.backend.SUPPORTED_BACKENDS`),
#: normalized. Anything unrecognized becomes "other" so the value space stays
#: closed even if a future backend name reaches here before this set does.
BACKENDS = frozenset({"claude", "codex", "local", "other"})

#: Blocker category -> (capability class, reason code). The whole editorial
#: judgement of this channel lives in this table: what counts as a capability
#: gap, and what is an ordinary failure of the change itself.
_CATEGORY_MAP: dict[str, tuple[str, str]] = {
    # The machine needed something that was not there.
    "TRANSIENT_INFRA": ("capability_unavailable", "backend_unavailable"),
    "DEPENDENCY_WAIT": ("capability_unavailable", "dependency_unavailable"),
    # Two consecutive attempts made no progress: the one blocker a retry
    # provably cannot clear (see `blockers/taxonomy.py`'s STAGNATION route).
    # That is "cannot complete with the current backends/tools" stated by the
    # harness itself, which is exactly what this channel is for.
    "STAGNATION": ("capability_unavailable", "no_progress"),
    # Capacity, not absence: the capability exists and is rationed.
    "QUOTA": ("capacity_blocked", "quota_exhausted"),
    "MISSING_ACCESS": ("authorization_blocked", "access_denied"),
    # A candidate solution refused by an explicit constraint.
    "SCOPE_EXPLOSION": ("candidate_rejected", "scope_constraint"),
    "IMPOSSIBLE": ("candidate_rejected", "infeasible_request"),
    # A human was asked because the machine had no way to decide.
    "AMBIGUITY": ("fallback_selected", "ambiguous_requirement"),
    "BUDGET_EXHAUSTED": ("budget_exhausted", "budget_exhausted"),
    # DELIBERATELY ABSENT, and this list is the argument, not an oversight:
    #   NOVEL_UNKNOWN — unclassified by construction. Emitting it would be a
    #     capability claim with no named cause behind it, which is the one
    #     thing issue #20's non-goals ask this channel not to do.
    #   USER_PAUSED  — a human deliberately stopped the task. Nothing was
    #     missing; somebody pressed pause.
}

#: Fallback for an off-ramp that carries no blocker category — `_fail`'s
#: `reason_category` (`telemetry.FAILURE_REASON_CATEGORIES`, already closed).
#: Only the two that name a MISSING capability are mapped. `review_failed`,
#: `max_attempts` and `tamper_blocked` are ordinary failures of the change
#: itself; `blocker_parked` is answered by `_CATEGORY_MAP` when a category is
#: present and says nothing on its own; `other` names nothing.
_REASON_MAP: dict[str, tuple[str, str]] = {
    "infra": ("capability_unavailable", "backend_unavailable"),
    "budget_exhausted": ("budget_exhausted", "budget_exhausted"),
}

#: Constraint key -> the closed set its value must belong to. A blocker
#: category can only ever appear here if `_CATEGORY_MAP` classifies it, since
#: an unclassified one produces no event at all.
CONSTRAINT_VALUES: dict[str, frozenset[str]] = {
    "blocker_category": frozenset(_CATEGORY_MAP),
    "outcome": OUTCOMES,
    "attempts_bucket": ATTEMPT_BUCKETS,
    "task_kind": TASK_KINDS,
    "backend": BACKENDS,
}

#: The spool's fixed filename inside ``capability_gap.dir``, and the
#: pseudonym's under ``~/.no_human``. Both paths are resolved per call (never
#: at import) so a temp-HOME test suite cannot touch an operator's real home.
SPOOL_NAME = "capability-gap.jsonl"
_PSEUDONYM_NAME = "capability-gap-id"

DEFAULT_MAX_LINES = 10_000   # retained spool lines after a compaction
COMPACT_AT_BYTES = 8 << 20   # compact only past this size; appends stay O(1)
FLUSH_BATCH = 50             # max events per POST on the http sink
_HTTP_TIMEOUT = 3.0

#: The exact key set of a v1 event, used by `_sendable` to reject a spool line
#: that does not have the shape `build_event` produces.
_EVENT_KEYS = frozenset({
    "schema", "event_id", "ts", "capability_class", "reason_code",
    "constraints", "source", "synthetic",
})
_SOURCE_KEYS = frozenset({"product", "version", "instance"})

#: The only shape `source.version` and `source.instance` may take: no path
#: separator, no space, no ``:`` or ``@`` — nothing a path, URL or secret
#: phrase needs. `re.fullmatch`, so a trailing newline cannot slip past ``$``.
_TOKEN_RE = re.compile(r"[A-Za-z0-9._+-]{1,64}")

_LOCK = threading.Lock()
#: Held for a whole flush, taken non-blocking: a flush that finds it held is
#: skipped (the next recorded event spawns another), so two threads can never
#: read, POST and remove the same batch twice.
_FLUSH_LOCK = threading.Lock()

#: Process-local fallback pseudonym, minted once if the on-disk one can be
#: neither read nor written. Never persisted, so it changes between runs —
#: the honest outcome when the machine will not hold state, and one that still
#: lets the events of a single run be grouped.
_FALLBACK_PSEUDONYM: str | None = None


def _home() -> Path:
    """``~/.no_human``, resolved per call rather than at import.

    `config.NO_HUMAN_HOME` is a module-level constant, bound to whatever HOME
    the process started with; reusing it would point a temp-HOME test suite at
    the operator's real home. Same reason `telemetry._queue_path` resolves its
    own path per call.
    """
    return Path.home() / ".no_human"


def _conf(config: dict[str, Any] | None) -> dict[str, Any]:
    if config is None:
        from .config import load_config
        config = load_config().data
    section = config.get("capability_gap") or {}
    return section if isinstance(section, dict) else {}


def _spool_path(section: dict[str, Any]) -> Path:
    """``<capability_gap.dir>/capability-gap.jsonl`` — the directory is
    configurable, the filename never is. `_append` compacts and `flush`
    rewrites this file, so a setting that could name the FILE could point
    that rewrite at any file the operator owns; a fixed name inside the
    configured directory cannot."""
    raw = str(section.get("dir") or "").strip()
    return (Path(raw).expanduser() if raw else _home()) / SPOOL_NAME


def _refuse_symlink(path: Path) -> None:
    """A symlink named like the spool would redirect the rewrite elsewhere."""
    if path.is_symlink():
        raise OSError(f"capability_gap: refusing symlinked spool {path}")


def _is_token(value: Any) -> bool:
    return isinstance(value, str) and _TOKEN_RE.fullmatch(value) is not None


def _valid_endpoint(url: str) -> bool:
    """True for an endpoint this channel is allowed to POST to.

    ``https`` anywhere, ``http`` on loopback only. Everything else is refused
    — ``file://`` and ``ftp://`` most of all, because `urllib.request.urlopen`
    honours them and a mistyped scheme would quietly turn a network sink into
    a writer pointed at whatever path the string named.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme == "https":
        return bool(parsed.hostname)
    if parsed.scheme == "http":
        return parsed.hostname in ("localhost", "127.0.0.1", "::1")
    return False


def _destination(section: dict[str, Any]) -> tuple[str, str] | None:
    """``("jsonl", <path>)``, ``("http", <url>)``, or ``None`` when nothing
    usable is configured — which is also what an ``http`` sink with a bad
    endpoint resolves to, so a typo disables the channel rather than POSTing
    somewhere unintended."""
    sink = str(section.get("sink") or "jsonl").strip().lower()
    if sink == "jsonl":
        return ("jsonl", str(_spool_path(section)))
    if sink == "http":
        endpoint = str(section.get("endpoint") or "").strip()
        return ("http", endpoint) if _valid_endpoint(endpoint) else None
    return None


def enabled(config: dict[str, Any] | None = None) -> bool:
    """True when the channel is switched on AND a destination resolves."""
    section = _conf(config)
    return bool(section.get("enabled")) and _destination(section) is not None


def attempt_bucket(n: int) -> str:
    """Bucket an attempt count so no precise per-task number leaves the
    machine. The same shape `telemetry.orphan_bucket` uses, kept separate so
    the two wire contracts cannot drift into each other."""
    if n <= 0:
        return "0"
    if n == 1:
        return "1"
    if n <= 5:
        return "2-5"
    return "6+"


def normalize_backend(name: str | None) -> str:
    value = (name or "").strip().lower()
    return value if value in BACKENDS else "other"


def normalize_task_kind(kind: str | None) -> str:
    value = (kind or "").strip().lower()
    return value if value in TASK_KINDS else "other"


def is_synthetic(section: dict[str, Any]) -> bool:
    """Whether this install is a real deployment or not.

    ``capability_gap.synthetic`` forces the answer when set to a bool. Left at
    its default (``None``) the classification is `telemetry.environment`'s:
    anything but ``"real"`` — a pytest run, a bench replay, a CI job, a
    source checkout under a throwaway HOME — is synthetic; a checkout run with
    a normal HOME is "real" unless the flag is set. Issue #20 asks for the flag
    precisely so a pilot cannot mistake dogfood volume for demand.
    """
    forced = section.get("synthetic")
    if isinstance(forced, bool):
        return forced
    from . import telemetry
    return telemetry.environment() != "real"


def instance_pseudonym(section: dict[str, Any]) -> str:
    """A stable, opaque id for this install — never telemetry's `instance_id`.

    ``capability_gap.instance_pseudonym`` wins when set AND it matches
    `_TOKEN_RE`; a value that does not (a path, a URL, a phrase) is ignored.
    Otherwise a uuid4 is
    minted once and kept in ``~/.no_human/capability-gap-id``, NOT in
    config.yaml: this channel must be able to identify itself without writing
    to the operator's configuration, and keeping it out of config.yaml also
    keeps it out of ``/api/config``'s echo.

    A file whose content does not match `_TOKEN_RE` is re-minted.

    Fail-open. If the file can neither be read nor written, a process-local
    uuid4 is used instead — the run's events still group, they just do not
    group with the next run's.
    """
    global _FALLBACK_PSEUDONYM
    configured = str(section.get("instance_pseudonym") or "").strip()
    if _is_token(configured):
        return configured
    path = _home() / _PSEUDONYM_NAME
    try:
        existing = path.read_text(encoding="utf-8", errors="replace").strip()
        if _is_token(existing):
            return existing
    except OSError:
        pass
    minted = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(minted + "\n", encoding="utf-8")
        return minted
    except OSError:
        if _FALLBACK_PSEUDONYM is None:
            _FALLBACK_PSEUDONYM = minted
        return _FALLBACK_PSEUDONYM


def classify(blocker_category: str | None,
             reason_category: str | None) -> tuple[str, str] | None:
    """``(capability_class, reason_code)`` for one off-ramp, or ``None`` when
    this off-ramp is not a capability gap.

    The blocker category decides when there is one; ``reason_category`` is the
    fallback for the off-ramps that carry no blocker (`Orchestrator._fail`).
    An unrecognized value on either is ``None`` — never a guess, because a
    guessed capability class is exactly the overclaim issue #20's non-goals
    rule out.
    """
    key = (blocker_category or "").strip().upper()
    if key in _CATEGORY_MAP:
        return _CATEGORY_MAP[key]
    reason = (reason_category or "").strip().lower()
    return _REASON_MAP.get(reason)


def build_event(capability_class: str, reason_code: str, *,
                constraints: dict[str, str], instance: str,
                synthetic: bool, version: str) -> dict[str, Any]:
    """The complete wire event. Pure apart from the uuid and the clock, so a
    test can assert the whole shape in one comparison."""
    return {
        "schema": SCHEMA,
        "event_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "capability_class": capability_class,
        "reason_code": reason_code,
        "constraints": dict(constraints),
        "source": {"product": "no_human", "version": version,
                   "instance": instance},
        "synthetic": synthetic,
    }


def _validate(capability_class: Any, reason_code: Any,
              constraints: dict[str, Any]) -> None:
    if capability_class not in CAPABILITY_CLASSES:
        raise ValueError(
            f"capability_gap: unknown capability class {capability_class!r}")
    if reason_code not in REASON_CODES:
        raise ValueError(f"capability_gap: unknown reason code {reason_code!r}")
    unknown = set(constraints) - set(CONSTRAINT_VALUES)
    if unknown:
        raise ValueError(
            f"capability_gap: constraint key(s) {sorted(unknown)!r} are not in "
            "the closed set")
    for name, value in constraints.items():
        if value not in CONSTRAINT_VALUES[name]:
            raise ValueError(
                f"capability_gap: value {value!r} not allowed for constraint "
                f"{name!r}")


def record(capability_class: str, reason_code: str, *,
           constraints: dict[str, str] | None = None,
           config: dict[str, Any] | None = None) -> None:
    """Append one capability-gap event to the spool. No-op unless the channel
    is enabled AND a destination resolves.

    Raises ``ValueError`` for a class, reason code, constraint key or
    constraint value outside the closed sets — validated even when the channel
    is DISABLED, because an unlisted field is a bug either way and a channel
    nobody has switched on yet is exactly where that bug would hide. Every
    other failure is swallowed: this must never break a task run.
    """
    constraints = dict(constraints or {})
    _validate(capability_class, reason_code, constraints)
    try:
        section = _conf(config)
        dest = _destination(section)
        if not (bool(section.get("enabled")) and dest):
            return
        from . import __version__
        event = build_event(
            capability_class, reason_code, constraints=constraints,
            instance=instance_pseudonym(section),
            synthetic=is_synthetic(section), version=__version__)
        if not _sendable(event):  # the jsonl sink's reader gets the same shape
            return
        _append(event, section)
        if dest[0] == "http":
            _spawn_flush(section)
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 — fail-open
        # Debug, not warning: a disabled or misconfigured sink must not add a
        # line to every off-ramp's log, but an operator asking "why is my
        # spool empty" needs somewhere to look.
        log.debug("capability_gap: event dropped", exc_info=True)
        return


def _max_lines(section: dict[str, Any]) -> int:
    raw = section.get("max_lines", DEFAULT_MAX_LINES)
    if isinstance(raw, bool):
        return DEFAULT_MAX_LINES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_LINES
    return value if value > 0 else DEFAULT_MAX_LINES


def _append(event: dict[str, Any], section: dict[str, Any]) -> None:
    """Append one line, then compact only if the file has grown past
    `COMPACT_AT_BYTES`.

    Appending is O(1); rewriting the whole spool on every event (the shape
    `telemetry._append` uses, for a 500-line queue) would be O(n) against a
    file three orders of magnitude larger. The size trigger keeps the common
    path cheap and still bounds the file: past the threshold the newest
    ``capability_gap.max_lines`` lines are kept and the rest are dropped.
    """
    path = _spool_path(section)
    line = json.dumps(event, separators=(",", ":"))
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        _refuse_symlink(path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        try:
            oversize = path.stat().st_size > COMPACT_AT_BYTES
        except OSError:
            oversize = False
        if oversize:
            kept = [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
                    if ln.strip()][-_max_lines(section):]
            path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def _spawn_flush(section: dict[str, Any]) -> None:
    threading.Thread(
        target=flush, args=(section,), name="nh-capability-gap-flush",
        daemon=True,
    ).start()


def _sendable(event: Any) -> bool:
    """A spool line that still satisfies the closed contract.

    A line is re-read from disk before it ships, and a hand-edited or
    half-written one must not leave the machine just because it parses as
    JSON. This re-runs the SAME validation `record` ran, on the way out, and
    checks the SHAPE of every field that is not an enum: ``event_id`` is a
    canonical uuid, ``ts`` a UTC `isoformat` timestamp, ``source.product``
    the literal ``"no_human"``, ``source.version``/``source.instance`` match
    `_TOKEN_RE`. Never raises: any malformed line (a list where a string
    belongs, a nested object) is simply not sendable.
    """
    try:
        return _sendable_or_raise(event)
    except Exception:  # noqa: BLE001 — malformed means "drop", never "stall"
        return False


def _sendable_or_raise(event: Any) -> bool:
    if not isinstance(event, dict) or set(event) != _EVENT_KEYS:
        return False
    if event["schema"] != SCHEMA or not isinstance(event["synthetic"], bool):
        return False
    event_id, ts = event["event_id"], event["ts"]
    if not isinstance(event_id, str) or str(uuid.UUID(event_id)) != event_id:
        return False
    if not isinstance(ts, str):
        return False
    stamp = datetime.fromisoformat(ts)
    if stamp.utcoffset() != timezone.utc.utcoffset(None) or stamp.isoformat() != ts:
        return False
    source = event["source"]
    if not isinstance(source, dict) or set(source) != _SOURCE_KEYS:
        return False
    if source["product"] != "no_human":
        return False
    if not (_is_token(source["version"]) and _is_token(source["instance"])):
        return False
    constraints = event["constraints"]
    if not isinstance(constraints, dict):
        return False
    _validate(event["capability_class"], event["reason_code"], constraints)
    return True


def flush(section: dict[str, Any] | None = None,
          config: dict[str, Any] | None = None) -> int:
    """POST up to `FLUSH_BATCH` spooled events to the configured endpoint.

    Returns the number sent — 0 on any failure, with the spool left intact for
    a later attempt. Only the lines that were actually taken are removed, so
    an event recorded while the POST was in flight is never lost. Stdlib
    urllib only, 3s timeout, fail-open throughout. Returns 0 at once when
    another flush is already in flight (`_FLUSH_LOCK`).
    """
    if section is None:
        section = _conf(config)
    dest = _destination(section)
    if not (bool(section.get("enabled")) and dest and dest[0] == "http"):
        return 0
    if not _FLUSH_LOCK.acquire(blocking=False):
        return 0
    try:
        return _flush_locked(section, dest[1])
    finally:
        _FLUSH_LOCK.release()


def _flush_locked(section: dict[str, Any], endpoint: str) -> int:
    try:
        path = _spool_path(section)
        with _LOCK:
            _refuse_symlink(path)
            if not path.exists():
                return 0
            lines = [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
                     if ln.strip()]
            batch_lines = lines[:FLUSH_BATCH]
        if not batch_lines:
            return 0
        events = []
        for raw in batch_lines:
            try:
                event = json.loads(raw)
            except (ValueError, RecursionError):  # corrupt / absurdly deep
                continue  # a corrupt line is dropped, never re-sent forever
            if _sendable(event):
                events.append(event)
        if events:
            import urllib.request
            body = json.dumps({"schema": SCHEMA, "events": events}).encode()
            req = urllib.request.Request(
                endpoint, data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT):
                pass
        # Reached on both branches: a batch with nothing sendable in it is
        # DROPPED rather than left at the head of the spool, where it would
        # wedge every later flush behind lines that can never ship (the
        # failure mode `telemetry.flush` documents for its own queue).
        with _LOCK:
            current = []
            if path.exists():
                current = [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
                           if ln.strip()]
            sent = set(batch_lines)
            kept = [ln for ln in current if ln not in sent]
            path.write_text("\n".join(kept) + "\n" if kept else "",
                            encoding="utf-8")
        return len(events)
    except Exception:  # noqa: BLE001 — fail-open: events stay spooled
        log.debug("capability_gap: flush failed", exc_info=True)
        return 0


# --------------------------------------------------------------------------- #
# The built-in producer: the orchestrator's emit() stream
# --------------------------------------------------------------------------- #

#: emit() kinds where a BOUNDED ATTEMPT stopped, mapped onto `OUTCOMES`. This
#: is the whole trigger surface of the built-in producer — no other kind is a
#: candidate.
#:
#: "cancelled" and "cancelled_hard" are deliberately absent, and for the same
#: reason `USER_PAUSED` is absent from `_CATEGORY_MAP`: a human stopping a
#: task is not the machine lacking a capability.
OUTCOMES_BY_KIND: dict[str, str] = {
    "failed": "failed",
    "escalated": "escalated",
    "blocked": "parked",
    "paused_quota": "parked",
    "awaiting_input": "needs_answer",
}

#: Off-ramps whose emit NAMES its category by its kind rather than carrying a
#: ``blocker_category``: `Orchestrator._park_quota` emits
#: ``paused_quota`` with only ``status`` and ``auth_profile`` on it. Used only
#: when the emit carries no ``blocker_category`` of its own. Caveat, stated
#: rather than hidden: `_park_quota` also parks a dead SDK session that raised
#: ``QuotaExhausted(infra=True)`` on this same kind, and the emit does not say
#: which it was, so such a park is reported as QUOTA too.
_KIND_CATEGORY: dict[str, str] = {"paused_quota": "QUOTA"}


class _TaskState:
    """This channel's own per-run bookkeeping — never telemetry's
    ``_tel_attempts``, so neither channel's counts can be moved by an edit
    made for the other."""

    __slots__ = ("task_kind", "attempts")

    def __init__(self) -> None:
        self.task_kind = "unknown"
        self.attempts = 0


#: One `_TaskState` per emitting object, dropped with it.
_STATES: "weakref.WeakKeyDictionary[Any, _TaskState]" = weakref.WeakKeyDictionary()


def observe(owner: Any, kind: str, meta: dict[str, Any]) -> None:
    """Derive at most one event from one ``owner.emit(kind, **meta)``.

    *owner* is the emitting `Orchestrator`; its ``config`` and
    ``_attempt_backend`` are read HERE, inside the fail-open guard, so a
    partially built one (tests construct them via ``__new__``) cannot make
    ``emit`` raise. A missing ``config`` is treated as ``{}`` — disabled —
    never as "load the operator's config from disk".

    A bounded attempt that stopped because something the machine NEEDED was
    missing becomes one closed-vocabulary event; an ordinary failure of the
    change itself resolves to `None` in `classify` and emits nothing.

    Reads only ``task_kind``, ``blocker_category`` and ``reason_category`` off
    *meta* — never ``blocker``, which rides on the same emit carrying the
    agent's own prose. Fail-open by construction: this can never break a run.
    """
    try:
        state = _STATES.setdefault(owner, _TaskState())
        if kind == "kind":
            state.task_kind = normalize_task_kind(str(meta.get("task_kind") or "unknown"))
            state.attempts = 0
            return
        if kind == "attempt_start":
            state.attempts += 1
            return
        outcome = OUTCOMES_BY_KIND.get(kind)
        if outcome is None:
            return
        category = str(meta.get("blocker_category") or _KIND_CATEGORY.get(kind, ""))
        classified = classify(category, str(meta.get("reason_category") or ""))
        if classified is None:
            return
        constraints = {
            "outcome": outcome,
            "attempts_bucket": attempt_bucket(state.attempts),
            "task_kind": state.task_kind,
            "backend": normalize_backend(getattr(owner, "_attempt_backend", None)),
        }
        # Only ever a category `_CATEGORY_MAP` classifies, so the constraint's
        # value space stays exactly that table.
        normalized = category.strip().upper()
        if normalized in CONSTRAINT_VALUES["blocker_category"]:
            constraints["blocker_category"] = normalized
        record(classified[0], classified[1], constraints=constraints,
               config=getattr(owner, "config", None) or {})
    except Exception:  # noqa: BLE001 — fail-open
        log.debug("capability_gap: observe failed", exc_info=True)
