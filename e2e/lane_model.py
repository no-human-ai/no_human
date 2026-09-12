"""Single source of truth for board-lane expectations, browser-free.

``e2e/board_e2e.py`` must never hardcode a lane name, a lane's status list,
or a demo card count — every one of those facts already lives in two places
that are kept in sync on purpose (``web/src/boardLanes.js`` for the UI,
``testdata/lane_conformance.json`` for the shared routing fixture used by
both ``tests/test_lane_conformance.py`` and
``web/src/laneConformance.test.mjs``). This module reads both of those files
directly — no Playwright, no server, importable from a plain pytest process —
so the e2e script (and its pytest companion, test_board_e2e_lane_model.py)
derive their expectations instead of re-asserting a stale guess.

``boardLanes.js`` is parsed with a small regex rather than a JS engine: the
``LANES`` array in that file is a list of single-line object literals with no
nested ``{...}`` (nested structures there use ``[...]``), which makes a
regex-per-entry safe. If that file's ``LANES`` array ever grows a
multi-line or nested-object entry, ``parse_lanes()`` will raise rather than
silently mis-parse (see the assertion in ``parse_lanes``).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_LANES_JS = REPO_ROOT / "web" / "src" / "boardLanes.js"
LANE_CONFORMANCE_JSON = REPO_ROOT / "testdata" / "lane_conformance.json"


@dataclass(frozen=True)
class Lane:
    key: str
    label: str
    outcome: bool


def _extract_lanes_block(js_text: str) -> str:
    m = re.search(r"export const LANES\s*=\s*\[(.*?)\n\];", js_text, re.DOTALL)
    if not m:
        raise ValueError("could not find `export const LANES = [...]` in boardLanes.js")
    return m.group(1)


def parse_lanes(js_text: str | None = None) -> tuple[Lane, ...]:
    """Parse the ``LANES`` array out of ``boardLanes.js``.

    Each array entry is expected to be a single-line ``{ ... }`` object
    literal (true today — see module docstring). Raises ValueError if an
    entry doesn't look like that, rather than silently dropping it.
    """
    text = js_text if js_text is not None else BOARD_LANES_JS.read_text(encoding="utf-8")
    block = _extract_lanes_block(text)
    entries = [ln.strip() for ln in block.splitlines() if ln.strip()]
    lanes: list[Lane] = []
    for entry in entries:
        entry = entry.rstrip(",")
        if not (entry.startswith("{") and entry.endswith("}")):
            raise ValueError(f"unexpected LANES entry shape (not a single-line object): {entry!r}")
        key_m = re.search(r'key:\s*"([^"]+)"', entry)
        label_m = re.search(r'label:\s*"([^"]+)"', entry)
        if not key_m or not label_m:
            raise ValueError(f"LANES entry missing key/label: {entry!r}")
        outcome_m = re.search(r"outcome:\s*(true|false)", entry)
        outcome = bool(outcome_m and outcome_m.group(1) == "true")
        lanes.append(Lane(key=key_m.group(1), label=label_m.group(1), outcome=outcome))
    if not lanes:
        raise ValueError("parsed zero lanes out of boardLanes.js — regex is out of date")
    return tuple(lanes)


LANES: tuple[Lane, ...] = parse_lanes()
BOARD_LANES: tuple[Lane, ...] = tuple(l for l in LANES if not l.outcome)
OUTCOME_LANES: tuple[Lane, ...] = tuple(l for l in LANES if l.outcome)

BOARD_LANE_KEYS: tuple[str, ...] = tuple(l.key for l in BOARD_LANES)
OUTCOME_LANE_KEYS: tuple[str, ...] = tuple(l.key for l in OUTCOME_LANES)


def label_for(key: str) -> str:
    for lane in LANES:
        if lane.key == key:
            return lane.label
    raise KeyError(f"no lane with key {key!r}")


def _load_fixture_cases() -> list[dict]:
    return json.loads(LANE_CONFORMANCE_JSON.read_text(encoding="utf-8"))["cases"]


FIXTURE_CASES: tuple[dict, ...] = tuple(_load_fixture_cases())

# (status, bool(wake_condition)) -> lane key. First pin wins: the fixture has
# several cases sharing the same (status, bool(wake)) key (e.g. `blocked`
# with wake=None and `blocked` with wake="" both mean "no wake"), and they
# must agree — a later, differently-worded case must never silently override
# an earlier pin to a DIFFERENT lane, which would hide a real fixture
# conflict instead of surfacing it.
_STATUS_WAKE_LANE: dict[tuple[str | None, bool], str] = {}
for _case in FIXTURE_CASES:
    _task = _case["task"]
    _status = _task.get("status")
    _wake = bool(_task.get("blocker_wake_condition"))
    _key = (_status, _wake)
    _lane = _case["lane"]
    _existing = _STATUS_WAKE_LANE.setdefault(_key, _lane)
    if _existing != _lane:
        raise ValueError(
            f"lane_conformance.json disagrees with itself for status={_status!r} "
            f"wake={_wake!r}: {_existing!r} vs {_lane!r}"
        )


def fixture_lane(status: str, wake: object = None) -> str:
    """Look up the expected lane for ``status``/``wake`` in the shared fixture.

    Raises KeyError if that exact (status, bool(wake)) combination was never
    pinned by testdata/lane_conformance.json — callers must not guess at an
    unpinned combination.
    """
    key = (status, bool(wake))
    if key not in _STATUS_WAKE_LANE:
        raise KeyError(
            f"status={status!r} wake={wake!r} is not pinned by "
            f"testdata/lane_conformance.json — add a case there first"
        )
    return _STATUS_WAKE_LANE[key]


def lane_title_for_status(status: str, wake: object = None) -> str | None:
    """The board-lane label a task with this status/wake renders under, or
    None if it routes to an outcome lane (Done/Failed) that isn't on the
    board at all."""
    key = fixture_lane(status, wake)
    if key in OUTCOME_LANE_KEYS:
        return None
    return label_for(key)



# Rendered (CSS-uppercased) DOM text — `.lane-title` is upper-cased purely via
# stylesheet (styles.css), so Playwright's `inner_text()` returns the UPPER
# form while `label_for()`/`Lane.label` stay in normal case for readability
# and for the `.so-*`/prose call sites that want the natural-case string.
BOARD_LANE_TITLES: tuple[str, ...] = tuple(l.label.upper() for l in BOARD_LANES)
OUTCOME_LANE_TITLES: tuple[str, ...] = tuple(l.label.upper() for l in OUTCOME_LANES)
