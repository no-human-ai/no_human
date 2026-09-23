"""monday.com item intake — GraphQL board pull, normalize → Task.

Deliberately shaped like ``intake/linear.py``, because it plays the SAME role: a
server-side POLLED issue source, not an argument to ``nh task add``. Config
lives in ``integrations.monday``; the token is ``MONDAY_API_TOKEN`` in
``~/.no_human/.env`` (never config, never logged).

API FACTS THIS MODULE DEPENDS ON (verified LIVE against a real account,
2026-08-05 — every claim below was observed, not read)
--------------------------------------------------------------------------
* **Endpoint** ``https://api.monday.com/v2``. POST + JSON only.
* **Auth header is the RAW token** — ``Authorization: <token>``, *not*
  ``Bearer <token>``, exactly like Linear. Getting this wrong is a 401 that
  looks like a bad token.
* **``API-Version`` is pinned** (:data:`API_VERSION`). monday versions its
  schema by date and rotates the account default; an unpinned client silently
  changes behaviour when the account's default moves. ``2025-07`` was verified
  to answer 200 for the exact queries below.
* **Throttling answers HTTP 429 with an HTML body, not JSON.** This is the
  inverse of Linear's trap and it matters more: there is NO parseable error
  code to branch on, so 429 must be classified by STATUS ALONE, *before* the
  body is parsed. A client that parses first and classifies second reports
  monday's rate limit as "non-JSON response" and never retries it.
* **Status does not classify the rest of the failure.** A GraphQL validation
  error arrives at 200 with an ``errors`` array and NO ``extensions`` at all
  (observed: ``Cannot query field "nope" on type "Query"``); a bad cursor
  arrives at 200 *with* ``extensions.code == "CursorException"`` **and**
  ``data`` populated with nulls; auth failure arrives at 401 with
  ``extensions.code == "NOT_AUTHENTICATED"``; a malformed request arrives at
  400 with ``INVALID_GRAPHQL_REQUEST``. So the ``errors`` array is parsed on
  *every* response, and a missing ``extensions`` must not throw.
* **Paging is a cursor on ``items_page`` itself** —
  ``boards(ids:[...]) { items_page(limit:N, cursor:C) { cursor items {...} } }``
  — and ``cursor`` is ``null`` exactly when the page is the last one. ``limit``
  is capped at 500.
* **Mutations** (signatures read from the live schema, not from docs):
  ``create_update(item_id: ID, body: String!)`` and
  ``change_simple_column_value(board_id: ID!, item_id: ID, column_id: String!,
  value: String, create_labels_if_missing: Boolean)``. For a ``status`` column
  the simple value is just the label string.
* **A status column's labels live in ``settings_str``**, a JSON *string* on the
  column: ``json.loads(settings_str)["labels"]`` is a ``{index: label}`` dict.

THE ONE REAL DESIGN DIFFERENCE FROM JIRA AND LINEAR
---------------------------------------------------
Jira and Linear expose a **typed** workflow state — Linear's
``WorkflowState.type`` is one of seven documented strings, so a poller can say
"pull everything that is ``backlog``" and mean it on any workspace.

**monday has no such concept.** A status column is a bag of user-defined
labels, different on every board. A real bug board observed while building this
carried ``Ready for Dev``, ``Fixing``, ``Awaiting Review``, ``Known Bug``, an
empty string, and five more. Nothing in the API says which of those means
"not started". Inferring one — by label text, by colour, by ``done_colors`` —
would be a guess dressed as a mapping, and it would be wrong on the next board.

So the mapping is **explicit operator config** and nothing else:
``todo_labels`` says what to pull, ``in_progress_label``/``done_label`` say
where to move it. When ``board_id``/``status_column`` are unset the adapter
RAISES :class:`MondayConfigError` — it never returns an empty list, because a
silent empty result is indistinguishable from "the board is empty" and would
park a misconfigured integration in permanent quiet.

**Config that is WRONG is refused on the same terms as config that is absent.**
Absent config cannot reach the API at all, so it was always loud; wrong config
— ``status_column`` holding a column's *title* instead of its id, or
``todo_labels`` naming a label the board does not have — sails through, matches
nothing, and returns ``[]``. That is the same lie with a longer fuse, and the
id/title mix-up is the one operators actually make. So :meth:`search` validates
both against the board's real columns before it filters
(:meth:`MondayAdapter._validate_intake_config`), reusing the cached
``board_columns()`` — one request per adapter, not one per item. The other end
of the same failure is a pull that stops at the :data:`MAX_PAGES` bound: it is
flagged and logged as PARTIAL rather than returned as if it were the board.

Write-back is opt-in (``integrations.monday.write_back``, default false) and
mirrors the Linear adapter: updates (comments) plus label moves resolved at
runtime against the board's OWN labels, never a label this code invented.
``create_labels_if_missing`` is never sent, so a typo'd config label fails
loudly instead of silently vandalising the operator's board with a new status.
The agent still never merges (constraint #2).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from ..core.task import Task
from .criteria import extract_acceptance_criteria

log = logging.getLogger("no_human.intake.monday")

API_URL = "https://api.monday.com/v2"

#: Pinned schema version. monday rotates the per-account default; pinning is
#: what stops the schema moving under a running install. Verified live.
API_VERSION = "2025-07"

#: monday's own cap on ``items_page(limit:)``.
MAX_PAGE_SIZE = 500

#: How many pages one :meth:`MondayAdapter.search` will follow before it stops.
#: A bound is needed so a board that always hands back a cursor cannot spin the
#: poll tick forever — but hitting it means the result is PARTIAL, which is
#: exactly the kind of quiet half-answer this adapter refuses to give, so
#: reaching it is logged and flagged on the adapter rather than passed off as a
#: complete board.
MAX_PAGES = 20

#: The three meanings the poller may ask for. Deliberately NOT monday labels —
#: labels are per-board and live in config. This tuple is the whole vocabulary
#: between the poller and the adapter, and :meth:`MondayAdapter.transition`
#: refuses anything outside it so a typo cannot become a silent no-op.
STATE_MEANINGS = ("todo", "in_progress", "done")

_ITEM_FIELDS = """
        id
        name
        url
        created_at
        updated_at
        group { id title }
        column_values { id text column { title type } }
"""

_ITEMS_QUERY = """
query NoHumanItems($boardId: ID!, $limit: Int!, $cursor: String) {
  boards(ids: [$boardId]) {
    id
    name
    items_page(limit: $limit, cursor: $cursor) {
      cursor
      items {%s
      }
    }
  }
}
""" % _ITEM_FIELDS

_COLUMNS_QUERY = """
query NoHumanColumns($boardId: ID!) {
  boards(ids: [$boardId]) {
    id
    name
    columns { id title type settings_str }
  }
}
"""

_ITEM_STATUS_QUERY = """
query NoHumanItemStatus($itemId: ID!) {
  items(ids: [$itemId]) {
    id
    name
    column_values { id text }
  }
}
"""

_UPDATE_MUTATION = """
mutation NoHumanUpdate($itemId: ID!, $body: String!) {
  create_update(item_id: $itemId, body: $body) { id }
}
"""

# `create_labels_if_missing` is deliberately absent: the agent must never add a
# status label to the operator's board. An unknown label is a config error and
# has to fail, not self-heal.
_SET_STATUS_MUTATION = """
mutation NoHumanSetStatus($boardId: ID!, $itemId: ID!, $columnId: String!, $value: String!) {
  change_simple_column_value(
    board_id: $boardId, item_id: $itemId, column_id: $columnId, value: $value
  ) { id }
}
"""


class MondayError(RuntimeError):
    """A monday API failure. Carries the GraphQL ``extensions.code`` when the
    response had one. Never carries the token or the request headers."""

    def __init__(self, message: str, *, code: str = "", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class MondayRateLimited(MondayError):
    """HTTP 429. The body is an **HTML** error page, not JSON, so this is
    raised from the status before any parse is attempted."""


class MondayAuthError(MondayError):
    """``extensions.code == "NOT_AUTHENTICATED"`` — HTTP 401."""


class MondayConfigError(MondayError):
    """Required operator config is missing.

    Its own class because it is the one failure that is NOT the API's fault and
    NOT retryable: no amount of ticking fixes an unset ``board_id``. Raised
    instead of returning an empty list, so a misconfigured integration is loud.
    """


#: Error codes that mean "retry later", not "you did something wrong".
_RETRYABLE_CODES = frozenset({
    "COMPLEXITY_BUDGET_EXHAUSTED", "ComplexityException",
    "RATE_LIMIT_EXCEEDED", "RateLimitExceeded",
})


def _raise_for_graphql_errors(payload: Any, status: int) -> None:
    """Raise the right :class:`MondayError` subclass for a GraphQL error body.

    Called for EVERY response, whatever the status: monday returns validation
    errors at 200 (sometimes with no ``extensions`` key at all), cursor errors
    at 200 *with* populated ``data``, auth failure at 401 and malformed
    requests at 400 — every one of them carries the same ``errors`` array. Only
    the message text and the error code are surfaced — never the request, which
    holds the token.
    """
    if not isinstance(payload, dict):
        raise MondayError(f"malformed monday response (HTTP {status})", status=status)
    errors = payload.get("errors")
    if not errors:
        return
    first = errors[0] if isinstance(errors, list) and errors else {}
    if not isinstance(first, dict):
        first = {}
    # A validation error carries NO `extensions` at all — observed live. This
    # must degrade to an empty code, not raise a TypeError inside the handler.
    ext = first.get("extensions") or {}
    code = str(ext.get("code") or "") if isinstance(ext, dict) else ""
    message = str(first.get("message") or "monday API error")
    if code in _RETRYABLE_CODES:
        raise MondayRateLimited(message, code=code, status=status)
    if code == "NOT_AUTHENTICATED" or status == 401:
        raise MondayAuthError(message, code=code, status=status)
    raise MondayError(message, code=code, status=status)


def _status_labels_from_settings(settings_str: str | None) -> dict[str, str]:
    """``{index: label}`` out of a status column's ``settings_str``.

    ``settings_str`` is a JSON **string** on the column, not an object, and on
    a non-status column it is ``"{}"``. A malformed value degrades to ``{}``
    rather than throwing, because one bad column must not make the whole board
    unreadable.
    """
    try:
        settings = json.loads(settings_str or "{}")
    except (TypeError, ValueError):
        return {}
    labels = settings.get("labels") if isinstance(settings, dict) else None
    if not isinstance(labels, dict):
        return {}
    return {str(k): str(v) for k, v in labels.items()}


class MondayAdapter:
    kind = "monday"

    def __init__(self, config: dict | None = None):
        cfg = ((config or {}).get("integrations") or {}).get("monday") or {}
        self.board_id = str(cfg.get("board_id") or "").strip()
        self.status_column = (cfg.get("status_column") or "").strip()
        # The explicit label→meaning mapping. monday has no typed state, so
        # this config IS the mapping; nothing here is inferred.
        self.todo_labels = [str(x) for x in (cfg.get("todo_labels") or [])]
        self.in_progress_label = (cfg.get("in_progress_label") or "").strip()
        self.done_label = (cfg.get("done_label") or "").strip()
        self.default_repo = cfg.get("default_repo") or None
        self.write_back = bool(cfg.get("write_back", False))
        self.page_size = min(int(cfg.get("page_size", 100) or 100), MAX_PAGE_SIZE)
        # Token from the process env only (loaded from ~/.no_human/.env at the
        # CLI/API boundary). Never read from config, never logged.
        self.api_token = os.environ.get("MONDAY_API_TOKEN")
        # Per-instance cache of the board's columns. They are stable (an
        # operator edits them rarely) and every write-back would otherwise
        # spend a request re-fetching them. The read path's config check reuses
        # this same cache, so validating intake costs one request per adapter,
        # never one per page and never one per item.
        self._columns_cache: list[dict[str, Any]] | None = None
        #: True when the last :meth:`search` stopped at :data:`MAX_PAGES` with
        #: monday still offering a cursor — i.e. the list it returned is a
        #: PARTIAL view of the board. Read by the poller so the operator is
        #: told, instead of a truncated pull reading as a complete one.
        self.last_search_truncated = False

    @property
    def configured(self) -> bool:
        return bool(self.board_id and self.status_column and self.api_token)

    def _headers(self) -> dict[str, str]:
        # RAW token, not "Bearer <token>" — see module docstring.
        return {
            "Authorization": self.api_token or "",
            "Content-Type": "application/json",
            "API-Version": API_VERSION,
        }

    def _require_config(self) -> None:
        """Fail loudly on missing operator config, naming the exact keys.

        The alternative — returning nothing — is indistinguishable from an
        empty board, so a typo'd install would look like a working one that
        simply has no work. Every entry point that would otherwise degrade to
        silence calls this first.
        """
        missing = [
            name for name, value in (
                ("integrations.monday.board_id", self.board_id),
                ("integrations.monday.status_column", self.status_column),
            ) if not value
        ]
        if missing:
            raise MondayConfigError(
                "monday intake is not configured: set "
                + " and ".join(missing)
                + " in ~/.no_human/config.yaml. Discover them with the board's "
                "columns query — `boards { id name columns { id title type } }` "
                "— and use the column *id* (e.g. \"bug_status\"), not its title."
            )

    def _post(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """One GraphQL round-trip. Returns the ``data`` object.

        Raises :class:`MondayError` (or a subclass) on any failure, regardless
        of HTTP status. Never raises anything carrying the token.
        """
        if not self.api_token:
            raise MondayAuthError("MONDAY_API_TOKEN not set in ~/.no_human/.env")
        resp = httpx.post(
            API_URL, headers=self._headers(), timeout=30.0,
            json={"query": query, "variables": variables or {}},
        )
        # Throttling FIRST, before any parse: monday answers 429 with an HTML
        # error page. Parsing first would classify the single most common
        # transient failure as "non-JSON response" — a permanent-looking error
        # the poller would never retry as throttling.
        if resp.status_code == 429:
            raise MondayRateLimited(
                "rate limited (monday answers HTTP 429 with an HTML body, not JSON)",
                code="RATE_LIMIT_EXCEEDED", status=429,
            )
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001 — a non-JSON body is still a failure
            raise MondayError(
                f"non-JSON monday response (HTTP {resp.status_code})",
                status=resp.status_code,
            ) from None
        _raise_for_graphql_errors(payload, resp.status_code)
        if not 200 <= resp.status_code < 300:
            # No errors array but a non-2xx status: still a failure, never a
            # silently-empty success.
            raise MondayError(f"monday HTTP {resp.status_code}", status=resp.status_code)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MondayError(
                f"monday response had no data (HTTP {resp.status_code})",
                status=resp.status_code,
            )
        return data

    def _board(self, data: dict[str, Any]) -> dict[str, Any]:
        """The single requested board out of a ``boards(ids:[...])`` result.

        ``boards`` answers an empty list — not an error — for a board id that
        does not exist or that the token cannot see. Returning "no items" there
        would be the same silent lie :meth:`_require_config` exists to prevent,
        so it raises instead.
        """
        boards = data.get("boards") or []
        if not boards or not isinstance(boards[0], dict):
            raise MondayConfigError(
                f"monday board {self.board_id!r} not found, or this token cannot "
                "see it — check integrations.monday.board_id and the token's scopes"
            )
        return boards[0]

    # ------------------------------- read ---------------------------------- #

    def search(self, updated_since: str | None = None) -> list[dict[str, Any]]:
        """Items on the configured board that are in a configured todo label.

        Pages via monday's own cursor (``items_page.cursor``, ``null`` on the
        last page) so a board bigger than one page is not silently truncated.
        Bounded at 20 pages so a misconfigured board cannot spin the poll tick
        forever.

        Filtering is CLIENT-SIDE and deliberately so. monday's server-side
        ``query_params`` matches status columns by label *index*, and the index
        of a label is an implementation detail of the board that changes when
        an operator reorders their statuses — a filter keyed on it would
        silently start pulling the wrong work. The operator's config names
        labels by TEXT, so the filter matches on text.

        With ``todo_labels`` empty, every item on the board is in scope. That
        is a real choice an operator can make (a board that IS the queue), so
        it is honoured rather than treated as "match nothing" — but it is
        logged, because it is also what an unfinished config looks like.

        The config is checked against the BOARD before any filtering
        (:meth:`_validate_intake_config`): absent config was already loud, but
        *wrong* config used to be silent, and a silently-empty tracker is worse
        than one that refuses to start.
        """
        self._require_config()
        self._validate_intake_config()
        if not self.todo_labels:
            log.info(
                "monday board %s: integrations.monday.todo_labels is empty — "
                "every item on the board is in intake scope", self.board_id)
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        fetched = 0
        truncated = True
        for _ in range(MAX_PAGES):
            data = self._post(_ITEMS_QUERY, {
                "boardId": self.board_id,
                "limit": self.page_size,
                "cursor": cursor,
            })
            page = self._board(data).get("items_page") or {}
            for item in page.get("items") or []:
                fetched += 1
                if not self._in_scope(item, updated_since):
                    continue
                out.append(item)
            cursor = page.get("cursor")
            if not cursor:
                truncated = False
                break
        self.last_search_truncated = truncated
        if truncated:
            # Same failure class as a wrong-config empty result, at the other
            # end: a short answer that looks like a whole one. Say it is short.
            log.warning(
                "monday board %s: stopped at the %d-page bound with monday still "
                "offering a cursor — THIS RESULT IS PARTIAL (%d item(s) read, %d "
                "in scope); items past the bound were never fetched. Narrow the "
                "pull with integrations.monday.todo_labels, or raise "
                "integrations.monday.page_size (max %d).",
                self.board_id, MAX_PAGES, fetched, len(out), MAX_PAGE_SIZE)
        return out

    def _validate_intake_config(self) -> None:
        """Check ``status_column`` and ``todo_labels`` against the board ITSELF.

        THE read path's config check, and the hole it closes:
        :meth:`_require_config` catches config that is ABSENT. Config that is
        WRONG — a column *title* where an id belongs, or a label this board does
        not have — used to match nothing and return an empty list, which is
        indistinguishable from an empty board. That is the exact silence the
        absent-config check exists to prevent, arriving through the other door,
        and it is the failure an operator is *most* likely to hit: the id/title
        mix-up is the one the docs warn about twice.

        Costs at most ONE request per adapter — :meth:`board_columns` caches,
        and every later search reads the cache. Never per page, never per item.

        **ONE bad label fails the WHOLE call**, deliberately. Warn-and-continue
        would silently narrow intake to a subset the operator never chose, and
        the work that stops arriving looks exactly like work nobody filed —
        this integration has no way to tell those apart, which is the whole
        reason the rest of the module refuses to answer quietly. A label typo
        is also permanent: no number of poll ticks fixes it, so degrading it to
        a warning only buries it in tick noise.
        """
        labels = self.status_labels()      # raises on an unknown/non-status column
        if not self.todo_labels:
            return
        known = set(labels.values())
        unknown = [lbl for lbl in self.todo_labels if lbl not in known]
        if unknown:
            raise MondayConfigError(
                f"monday board {self.board_id}: integrations.monday.todo_labels "
                f"names {', '.join(repr(u) for u in unknown)}, which column "
                f"{self.status_column!r} does not have. Labels on this column: "
                f"{', '.join(repr(x) for x in sorted(known)) or '(none)'}. "
                "Every item would be filtered out and the board would read as "
                "empty, so intake refuses to run instead of returning nothing."
            )

    def _in_scope(self, item: dict[str, Any], updated_since: str | None) -> bool:
        """Client-side intake filter: configured todo label, and recency."""
        if self.todo_labels and self.status_of(item) not in self.todo_labels:
            return False
        if updated_since:
            # monday's updated_at is a UTC ISO-8601 string ("2026-08-05T07:48:14Z"),
            # so lexical comparison is chronological comparison. An item with
            # no timestamp is kept — dropping work because a field was absent
            # is the worse failure.
            updated_at = item.get("updated_at")
            if updated_at and str(updated_at) <= updated_since:
                return False
        return True

    def status_of(self, item: dict[str, Any]) -> str:
        """The item's text in the configured status column, or ``""``.

        Pure — reads only the fetched item, so the poller never spends a
        request to learn what it already fetched.
        """
        for cv in item.get("column_values") or []:
            if isinstance(cv, dict) and cv.get("id") == self.status_column:
                return str(cv.get("text") or "")
        return ""

    def board_columns(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """The configured board's columns (cached per adapter)."""
        self._require_config()
        if self._columns_cache is None or refresh:
            data = self._post(_COLUMNS_QUERY, {"boardId": self.board_id})
            self._columns_cache = self._board(data).get("columns") or []
        return self._columns_cache

    def status_labels(self, column_id: str | None = None,
                      *, refresh: bool = False) -> dict[str, str]:
        """``{index: label}`` for a status column on the board.

        Defaults to the configured ``status_column``. Raises when the column is
        absent or is not of type ``status``: both are config errors that would
        otherwise surface much later as a mystifying failed write.
        """
        wanted = column_id or self.status_column
        columns = self.board_columns(refresh=refresh)
        for col in columns:
            if col.get("id") != wanted:
                continue
            if col.get("type") != "status":
                raise MondayConfigError(
                    f"monday column {wanted!r} on board {self.board_id} is of type "
                    f"{col.get('type')!r}, not 'status' — "
                    "integrations.monday.status_column must name a status column"
                )
            return _status_labels_from_settings(col.get("settings_str"))
        # The id/title mix-up is the mistake people actually make — `docs/
        # adapters.md` warns about it twice — so when the value IS a real
        # column's title, the error hands back the id instead of leaving the
        # operator to diff two lists.
        by_title: dict[str, str] = {}
        for col in columns:
            by_title.setdefault(str(col.get("title") or "").casefold(),
                                str(col.get("id") or ""))
        actual_id = by_title.get(wanted.casefold())
        hint = (
            f" {wanted!r} is the TITLE of the column whose id is {actual_id!r} — "
            f"set integrations.monday.status_column to {actual_id!r}."
            if actual_id else ""
        )
        known = ", ".join(sorted(str(c.get("id")) for c in columns)) or "(none)"
        raise MondayConfigError(
            f"monday board {self.board_id} has no column with the id {wanted!r}, "
            "and integrations.monday.status_column must be a column ID, not a "
            f"title.{hint} Column ids on this board: {known}"
        )

    def item_status(self, item_id: str) -> str:
        """The item's CURRENT status label, fetched fresh.

        The analogue of the Linear adapter's ``state_type``: used by write-back
        so a "needs a human" note is not posted onto an item a human already
        moved to the done label.
        """
        self._require_config()
        data = self._post(_ITEM_STATUS_QUERY, {"itemId": str(item_id)})
        items = data.get("items") or []
        if not items or not isinstance(items[0], dict):
            return ""
        return self.status_of(items[0])

    # ---------------------------- normalize -------------------------------- #

    def normalize(self, item: dict[str, Any]) -> Task:
        """Pure: map a fetched monday item onto an internal Task.

        ``external_id`` is the item's numeric id as a string. Unlike Jira's
        ``PROJ-1`` or Linear's ``ENG-1``, monday has no account-wide human key
        — a readable ``BUG-002`` comes from an ``item_id`` column configured on
        one board alone, so keying on it would work on that board and break on
        the next. The numeric id is the only identifier the API guarantees.

        monday items have **no description field**; the free text lives in the
        board's own columns. So the description is the item's non-empty column
        values rendered as ``Title: value`` lines — real fetched data, in the
        operator's own vocabulary, and it carries a long-text column's contents
        through verbatim. Nothing here is invented.
        """
        item_id = str(item.get("id") or "")
        title = item.get("name") or item_id or "monday item"
        columns = [
            (str((cv.get("column") or {}).get("title") or cv.get("id") or ""),
             str(cv.get("text") or ""))
            for cv in (item.get("column_values") or [])
            if isinstance(cv, dict)
        ]
        description = "\n".join(f"{k}: {v}" for k, v in columns if v)
        task = Task.new(title, source="monday", external_id=item_id,
                        description=description)
        task.acceptance_criteria = extract_acceptance_criteria(
            description, f"monday item {task.external_id}")
        task.context = {
            "monday": {
                "id": item_id,
                "board_id": self.board_id,
                "url": item.get("url") or "",
                "status": self.status_of(item),
                "status_column": self.status_column,
                "group": (item.get("group") or {}).get("title"),
                "columns": {k: v for k, v in columns if v},
            }
        }
        return task

    # ------------------------------- write --------------------------------- #

    def comment(self, item_id: str, body: str) -> bool:
        """Post a work-note update. Opt-in (``write_back``).

        Returns False when write-back is disabled (a no-op, not an error), and
        False when monday returns no update id — never True for a write that
        did not happen.
        """
        if not self.write_back:
            return False
        self._require_config()
        data = self._post(_UPDATE_MUTATION, {"itemId": str(item_id), "body": body})
        return bool((data.get("create_update") or {}).get("id"))

    def resolve_state(self, meaning: str) -> str | None:
        """The operator's own label for ``meaning``, validated against the board.

        The monday analogue of Linear's ``resolve_state``, and the place the
        one design difference lands: there is no typed state to match, so the
        label comes from config — but it is checked against the board's REAL
        labels before it is ever written. A configured label the board does not
        have raises, because ``change_simple_column_value`` without
        ``create_labels_if_missing`` would fail anyway, and with it would
        create a status column entry on the operator's board. Neither is
        acceptable silently.

        Returns None when the operator simply left that label unset — an
        optional feature that is off, not an error.
        """
        meaning = (meaning or "").lower()
        if meaning not in STATE_MEANINGS:
            raise ValueError(
                f"not a monday state meaning: {meaning!r} "
                f"(expected one of {', '.join(STATE_MEANINGS)})"
            )
        if meaning == "todo":
            label = self.todo_labels[0] if self.todo_labels else ""
        elif meaning == "in_progress":
            label = self.in_progress_label
        else:
            label = self.done_label
        if not label:
            return None
        known = set(self.status_labels().values())
        if label not in known:
            raise MondayConfigError(
                f"monday status label {label!r} is not on column "
                f"{self.status_column!r} of board {self.board_id}. "
                f"Labels on this column: {', '.join(sorted(known)) or '(none)'}"
            )
        return label

    def transition(self, item_id: str, meaning: str) -> bool:
        """Opt-in (``write_back``). Move the item to the operator's own label
        for ``meaning``.

        Refuses any value outside :data:`STATE_MEANINGS`, since the value is an
        unvalidated string and a typo would otherwise become a silent no-op.
        No label configured for that meaning -> debug log + no-op (``False``):
        an operator who left ``done_label`` blank has opted out of that move,
        which is not a failure.
        """
        if not self.write_back:
            return False
        self._require_config()
        label = self.resolve_state(meaning)
        if label is None:
            log.debug("monday %s: no label configured for %s", item_id, meaning)
            return False
        data = self._post(_SET_STATUS_MUTATION, {
            "boardId": self.board_id,
            "itemId": str(item_id),
            "columnId": self.status_column,
            "value": label,
        })
        return bool((data.get("change_simple_column_value") or {}).get("id"))
