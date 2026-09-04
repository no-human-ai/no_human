"""monday.com intake: board pull → label filter → normalize → dedupe, plus
opt-in write-back.

Mirrors tests/test_intake_linear.py's shape. All HTTP is mocked — no test in
this file touches the network — and the API token is read from the env and must
never appear in a log line or an exception message.

The payload shapes below are the ones a real account actually returns; they
were captured from a live account and de-identified, which is why the status
labels are a ragged operator-authored set (including an EMPTY one) rather than
a tidy enum. That raggedness is the point: monday has no typed workflow state,
so anything this code "knows" about a label's meaning has to come from config.
"""

import logging

import httpx
import pytest

from no_human.core.task import Task, TaskStatus
from no_human.intake.monday import (
    API_URL,
    API_VERSION,
    MAX_PAGE_SIZE,
    MAX_PAGES,
    STATE_MEANINGS,
    MondayAdapter,
    MondayAuthError,
    MondayConfigError,
    MondayError,
    MondayRateLimited,
)
from no_human.intake.monday_poll import MondayPoller

BOARD = "1234567890"
STATUS_COL = "bug_status"


def _cfg(**over):
    mon = {"enabled": True, "board_id": BOARD, "status_column": STATUS_COL,
           "todo_labels": ["Ready for Dev"]}
    mon.update(over)
    return {"integrations": {"monday": mon}}


class _Resp:
    def __init__(self, payload, status_code=200, *, raises=False):
        self._payload = payload
        self.status_code = status_code
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._payload


def _item(item_id="2000000001", name="Home page is slow to load",
          status="Ready for Dev", **over):
    item = {
        "id": item_id,
        "name": name,
        "url": f"https://example.monday.com/boards/{BOARD}/pulses/{item_id}",
        "created_at": "2026-08-05T07:47:36Z",
        "updated_at": "2026-08-05T07:48:14Z",
        "group": {"id": "topics", "title": "Incoming  Bugs"},
        "column_values": [
            {"id": "people1", "text": "",
             "column": {"title": "Reporter", "type": "people"}},
            {"id": STATUS_COL, "text": status,
             "column": {"title": "Status", "type": "status"}},
            {"id": "priority_1", "text": "Low",
             "column": {"title": "Priority", "type": "status"}},
            {"id": "long_text", "text": "Repro:\n- [ ] page loads under 2s\n- [ ] no 500s",
             "column": {"title": "Details", "type": "long_text"}},
            {"id": "item_id", "text": "BUG-002",
             "column": {"title": "Bug ID", "type": "item_id"}},
        ],
    }
    item.update(over)
    return item


def _items_payload(items, cursor=None):
    return {"data": {"boards": [
        {"id": BOARD, "name": "Bugs Queue",
         "items_page": {"cursor": cursor, "items": items}}]}}


#: A real status column's settings_str. `labels` is a JSON *string* holding an
#: {index: label} dict, the indices are sparse and non-contiguous, and one
#: label is the empty string — all three are real properties of the operator's
#: board, and each one breaks a plausible shortcut.
_SETTINGS_STR = (
    '{"done_colors":[1],'
    '"labels":{"0":"Fixing","1":"Fixed","2":"Missing Info","3":"Move to \'Sprints\'",'
    '"4":"Known Bug","5":"","7":"Pending Deploy","8":"Duplicated",'
    '"9":"Awaiting Review","14":"Ready for Dev"},'
    '"labels_positions_v2":{"9":0,"14":1,"0":2}}'
)


def _columns_payload():
    return {"data": {"boards": [{"id": BOARD, "name": "Bugs Queue", "columns": [
        {"id": "name", "title": "Name", "type": "name", "settings_str": "{}"},
        {"id": STATUS_COL, "title": "Status", "type": "status",
         "settings_str": _SETTINGS_STR},
        {"id": "priority_1", "title": "Priority", "type": "status",
         "settings_str": '{"labels":{"0":"Critical","1":"Low","7":"Medium"}}'},
        {"id": "long_text", "title": "Details", "type": "long_text",
         "settings_str": "{}"},
    ]}]}}


def _serving(items_post):
    """Answer the board's COLUMNS query, and delegate the items query.

    ``search()`` checks its config against the board before it filters, so a
    realistic board answers TWO distinct queries. Routing on the operation name
    keeps each test's item-query assertions (cursors, limits, call counts)
    about the item query alone.
    """
    def fake_post(url, headers=None, timeout=None, json=None):
        if "NoHumanColumns" in json["query"]:
            return _Resp(_columns_payload())
        return items_post(url, headers=headers, timeout=timeout, json=json)

    return fake_post


def _constant(payload):
    """An items responder that answers the same payload every time."""
    return lambda *a, **k: _Resp(payload)


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("MONDAY_API_TOKEN", "eyJhbGciOiJIUzI1NiJ9.SEKRET.sig")


# --------------------------------------------------------------------------- #
# Configuration + auth                                                         #
# --------------------------------------------------------------------------- #

def test_adapter_configured_needs_board_column_and_token(token, monkeypatch):
    assert MondayAdapter(_cfg()).configured is True
    assert MondayAdapter(_cfg(board_id="")).configured is False
    # A board without a status column is NOT configured: monday has no typed
    # state, so without the column there is nothing to filter intake on.
    assert MondayAdapter(_cfg(status_column="")).configured is False
    monkeypatch.delenv("MONDAY_API_TOKEN")
    assert MondayAdapter(_cfg()).configured is False


@pytest.mark.parametrize("missing,expect", [
    ({"board_id": ""}, "board_id"),
    ({"status_column": ""}, "status_column"),
])
def test_unconfigured_search_raises_a_named_error_instead_of_returning_nothing(
        token, monkeypatch, missing, expect):
    """THE failure this integration is most likely to have, and the one that
    hides best.

    An empty list from an unconfigured adapter is indistinguishable from an
    empty board: the poller creates nothing, logs nothing, and the operator
    concludes there is no work. So it must RAISE, and the message must name the
    exact config key so the fix does not need a code read.
    """
    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: pytest.fail("must not call the API"))
    adapter = MondayAdapter(_cfg(**missing))
    with pytest.raises(MondayConfigError) as exc:
        adapter.search()
    assert f"integrations.monday.{expect}" in str(exc.value)
    assert "columns" in str(exc.value)      # it says how to DISCOVER the value


def test_unconfigured_is_not_swallowed_as_an_empty_result(token, monkeypatch):
    """Belt-and-braces on the same class of bug: whatever else changes, an
    unconfigured adapter must never produce a list."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(_items_payload([])))
    with pytest.raises(MondayConfigError):
        MondayAdapter(_cfg(board_id="", status_column="")).search()


def test_a_board_the_token_cannot_see_raises_rather_than_reading_as_empty(
        token, monkeypatch):
    """monday answers `boards: []` — not an error — for a board id that does
    not exist or that the token cannot see. Treating that as "no items" is the
    same silent lie as an unconfigured adapter."""
    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: _Resp({"data": {"boards": []}}))
    with pytest.raises(MondayConfigError, match="not found"):
        MondayAdapter(_cfg()).search()


def test_auth_header_is_the_raw_token_not_bearer(token, monkeypatch):
    """monday's documented header is `Authorization: <token>`. Sending
    `Bearer <token>` is a 401 that looks like a bad token."""
    seen = {}

    def fake_post(url, headers=None, timeout=None, json=None):
        seen.update(url=url, headers=headers, json=json)
        return _Resp(_items_payload([]))

    monkeypatch.setattr(httpx, "post", _serving(fake_post))
    MondayAdapter(_cfg()).search()
    assert seen["url"] == API_URL
    assert seen["headers"]["Authorization"] == "eyJhbGciOiJIUzI1NiJ9.SEKRET.sig"
    assert not seen["headers"]["Authorization"].startswith("Bearer")
    # The schema version is pinned, so the account's default moving cannot
    # change what a running install sends.
    assert seen["headers"]["API-Version"] == API_VERSION


def test_missing_token_raises_auth_error_before_any_request(monkeypatch):
    monkeypatch.delenv("MONDAY_API_TOKEN", raising=False)
    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: pytest.fail("must not call the API"))
    with pytest.raises(MondayAuthError, match="MONDAY_API_TOKEN"):
        MondayAdapter(_cfg()).search()


def test_the_token_never_appears_in_an_error(token, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(
        {"errors": [{"message": "Not authenticated",
                     "extensions": {"code": "NOT_AUTHENTICATED"}}]}, 401))
    with pytest.raises(MondayAuthError) as exc:
        MondayAdapter(_cfg()).search()
    assert "SEKRET" not in str(exc.value)


def test_the_token_never_appears_in_a_NON_auth_error(token, monkeypatch):
    """The leak guard above only walks the NOT_AUTHENTICATED path. Every other
    failure builds its MondayError in the same place, and a validation error is
    the one an operator is most likely to paste into an issue report."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(
        {"errors": [{"message": "Cannot query field \"nope\" on type \"Query\".",
                     "path": ["query"]}]}, 200))
    with pytest.raises(MondayError) as exc:
        MondayAdapter(_cfg()).search()
    err = exc.value
    assert type(err) is MondayError          # the base branch, not a subclass
    for rendered in (str(err), repr(err), str(err.args)):
        assert "SEKRET" not in rendered
        assert "eyJhbGciOi" not in rendered  # nor a truncated/prefixed form


# --------------------------------------------------------------------------- #
# Error classification                                                         #
# --------------------------------------------------------------------------- #

def test_throttling_is_classified_from_the_429_before_the_body_is_parsed(
        token, monkeypatch):
    """The single most likely integration bug, and it is the MIRROR of
    Linear's.

    monday answers a rate limit with HTTP 429 and an **HTML** error page — no
    JSON, no error code, nothing to branch on but the status. A client that
    parses the body first reports its most common transient failure as
    "non-JSON response", which reads as a broken API and is never retried as
    throttling.
    """
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(
        None, 429, raises=True))       # body does not parse — exactly like live
    with pytest.raises(MondayRateLimited) as exc:
        MondayAdapter(_cfg()).search()
    assert exc.value.status == 429
    assert "429" in str(exc.value)


def test_a_validation_error_with_no_extensions_key_does_not_crash_the_handler(
        token, monkeypatch):
    """Observed live: `Cannot query field "nope"` arrives at 200 with an
    `errors` array carrying `path` and NO `extensions` at all. A handler that
    assumed `extensions` was always present would raise a TypeError from inside
    the error path — turning a legible API error into a stack trace."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(
        {"errors": [{"message": "Cannot query field \"nope\" on type \"Query\".",
                     "path": ["query"]}]}, 200))
    with pytest.raises(MondayError) as exc:
        MondayAdapter(_cfg()).search()
    assert exc.value.code == ""                       # absent, not invented
    assert "Cannot query field" in str(exc.value)


def test_a_cursor_error_at_200_with_populated_data_is_still_an_error(
        token, monkeypatch):
    """Observed live: a bad cursor answers 200, carries an `errors` array AND a
    `data` object of nulls. Reading `data` first and only checking errors when
    it is missing would swallow this entirely."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(
        {"data": {"boards": [None]},
         "errors": [{"message": "Invalid or corrupted cursor provided.",
                     "extensions": {"code": "CursorException", "status_code": 200}}]}, 200))
    with pytest.raises(MondayError) as exc:
        MondayAdapter(_cfg()).search()
    assert exc.value.code == "CursorException"


def test_complexity_exhaustion_is_retryable_not_a_client_error(token, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(
        {"errors": [{"message": "Complexity budget exhausted",
                     "extensions": {"code": "COMPLEXITY_BUDGET_EXHAUSTED"}}]}, 200))
    with pytest.raises(MondayRateLimited):
        MondayAdapter(_cfg()).search()


def test_non_2xx_with_a_well_formed_body_still_raises(token, monkeypatch):
    """The status is checked INDEPENDENTLY of whether `data` parsed. A body
    that happens to deserialize (a gateway error page rendered as JSON) must
    not be accepted just because `data` is a dict — otherwise a 5xx returns an
    empty item list and the poll silently creates nothing while reporting
    success."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(_items_payload([]), 503))
    with pytest.raises(MondayError, match="503"):
        MondayAdapter(_cfg()).search()


def test_non_json_body_at_a_non_429_status_raises_rather_than_returning_empty(
        token, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(None, 502, raises=True))
    with pytest.raises(MondayError, match="non-JSON"):
        MondayAdapter(_cfg()).search()


# --------------------------------------------------------------------------- #
# Search: paging and the label filter                                          #
# --------------------------------------------------------------------------- #

def test_search_follows_the_cursor_so_a_second_page_is_not_dropped(token, monkeypatch):
    pages = [
        _Resp(_items_payload([_item("1", "first")], cursor="CUR1")),
        _Resp(_items_payload([_item("2", "second")], cursor=None)),
    ]
    cursors = []

    def fake_post(url, headers=None, timeout=None, json=None):
        cursors.append(json["variables"]["cursor"])
        return pages.pop(0)

    monkeypatch.setattr(httpx, "post", _serving(fake_post))
    items = MondayAdapter(_cfg()).search()
    assert [i["id"] for i in items] == ["1", "2"]
    assert cursors == [None, "CUR1"]      # the cursor was actually passed through


def test_search_stops_when_the_cursor_is_null(token, monkeypatch):
    """monday signals "last page" with a null cursor, NOT with an empty item
    list — a full final page still has items. Paging until the page is empty
    would spend an extra request every tick; ignoring the null would loop."""
    calls = []

    def fake_post(url, headers=None, timeout=None, json=None):
        calls.append(1)
        return _Resp(_items_payload([_item()], cursor=None))

    monkeypatch.setattr(httpx, "post", _serving(fake_post))
    assert len(MondayAdapter(_cfg()).search()) == 1
    assert len(calls) == 1      # ITEM queries only — the columns query is routed away


def test_page_size_is_clamped_to_mondays_own_cap(token, monkeypatch):
    seen = {}
    monkeypatch.setattr(httpx, "post", _serving(
        lambda url, headers=None, timeout=None, json=None:
        seen.update(json=json) or _Resp(_items_payload([]))))
    MondayAdapter(_cfg(page_size=100_000)).search()
    assert seen["json"]["variables"]["limit"] == MAX_PAGE_SIZE == 500


def test_todo_labels_filter_the_board_down_to_the_operators_own_labels(
        token, monkeypatch):
    """THE design difference, tested. monday has no typed state, so intake
    scope is exactly the labels the operator named — nothing is inferred from
    the label's text, colour or `done_colors`.
    """
    board = [
        _item("1", "ready", status="Ready for Dev"),
        _item("2", "in flight", status="Fixing"),
        _item("3", "shipped", status="Fixed"),
        _item("4", "triage", status="Awaiting Review"),
        _item("5", "blank", status=""),        # the real board has an EMPTY label
    ]
    monkeypatch.setattr(httpx, "post", _serving(_constant(_items_payload(board))))
    got = MondayAdapter(_cfg(todo_labels=["Ready for Dev", "Awaiting Review"])).search()
    assert [i["id"] for i in got] == ["1", "4"]


def test_the_label_filter_is_exact_not_a_substring_match(token, monkeypatch):
    """The board's EMPTY label is the sharpest probe there is: `"" in "Fixed"`
    is True, so a substring filter configured with it would take the whole
    board rather than the one item that actually carries it. It is also a REAL
    label on this board, so it survives the config check and reaches the
    filter — a made-up near-miss like "Fix" is now refused before it gets
    there, which is the defect above, not this one.
    """
    board = [_item("1", "a", status="Fixed"), _item("2", "b", status="Fixing"),
             _item("3", "c", status="")]
    monkeypatch.setattr(httpx, "post", _serving(_constant(_items_payload(board))))
    got = MondayAdapter(_cfg(todo_labels=[""])).search()
    assert [i["id"] for i in got] == ["3"]
    assert len(got) == 1


def test_an_empty_todo_labels_list_takes_the_whole_board_and_says_so(
        token, monkeypatch, caplog):
    board = [_item("1", "a", status="Fixed"), _item("2", "b", status="Ready for Dev")]
    monkeypatch.setattr(httpx, "post", _serving(_constant(_items_payload(board))))
    with caplog.at_level(logging.INFO, logger="no_human.intake.monday"):
        got = MondayAdapter(_cfg(todo_labels=[])).search()
    assert [i["id"] for i in got] == ["1", "2"]
    assert "todo_labels is empty" in caplog.text     # loud, not a silent default


def test_updated_since_drops_items_that_have_not_moved(token, monkeypatch):
    board = [
        _item("1", "old", updated_at="2026-08-01T00:00:00Z"),
        _item("2", "new", updated_at="2026-08-05T07:48:14Z"),
        _item("3", "undated", updated_at=None),
    ]
    monkeypatch.setattr(httpx, "post", _serving(_constant(_items_payload(board))))
    got = MondayAdapter(_cfg()).search(updated_since="2026-08-02T00:00:00Z")
    # The undated item is KEPT: dropping work because a field was absent is the
    # worse of the two failures.
    assert [i["id"] for i in got] == ["2", "3"]


# --------------------------------------------------------------------------- #
# Search: config that is WRONG, not merely absent                              #
# --------------------------------------------------------------------------- #
#
# ABSENT config was always loud — it cannot even reach the API. WRONG config
# used to sail straight through: it matched nothing, returned [], logged
# nothing and raised nothing, so a board full of work read as an empty one.
# The write path checked the column, but write-back is opt-in and off by
# default, so on the SHIPPED default the column was never validated at all.


def test_a_column_title_where_an_id_belongs_fails_loudly_and_hands_back_the_id(
        token, monkeypatch):
    """The mistake operators actually make, and the one that hid best.

    `status_column: Status` is the column's TITLE. It matches no column id, so
    every item's status read as "" and the filter dropped the entire board —
    no exception, no log line. A tracker integration that silently shows zero
    tickets is worse than one that refuses to start.
    """
    monkeypatch.setattr(httpx, "post", _serving(_constant(_items_payload([_item()]))))
    with pytest.raises(MondayConfigError) as exc:
        MondayAdapter(_cfg(status_column="Status")).search()
    msg = str(exc.value)
    assert "integrations.monday.status_column" in msg
    assert "TITLE" in msg                     # it names the actual mistake ...
    assert f"'{STATUS_COL}'" in msg           # ... and hands back the id to use
    assert "long_text" in msg                 # ... and lists the real ids


def test_a_status_column_that_matches_nothing_at_all_lists_the_real_ids(
        token, monkeypatch):
    """No column and no title matches, so there is no id to hand back — but
    the real ids still have to be listed, and no title hint may be invented."""
    monkeypatch.setattr(httpx, "post", _serving(_constant(_items_payload([_item()]))))
    with pytest.raises(MondayConfigError) as exc:
        MondayAdapter(_cfg(status_column="statuses")).search()
    msg = str(exc.value)
    assert "'statuses'" in msg
    assert "TITLE" not in msg                 # nothing matched: no false hint
    assert STATUS_COL in msg and "long_text" in msg


def test_a_non_status_column_is_refused_on_the_READ_path_too(token, monkeypatch):
    """`status_labels` already refused a non-status column — but its only
    callers were `status_labels` and `resolve_state`, i.e. the WRITE path. On
    the shipped default (write_back off) this check was never reached."""
    monkeypatch.setattr(httpx, "post", _serving(_constant(_items_payload([_item()]))))
    with pytest.raises(MondayConfigError, match="not 'status'"):
        MondayAdapter(_cfg(status_column="long_text")).search()


def test_a_todo_label_the_board_does_not_have_fails_instead_of_matching_nothing(
        token, monkeypatch):
    """"Fix" is not a label on this board — "Fixing" and "Fixed" are. Matching
    nothing and returning [] is indistinguishable from an empty board, so the
    error has to name the bad label AND list what the board really has."""
    monkeypatch.setattr(httpx, "post", _serving(_constant(_items_payload([_item()]))))
    with pytest.raises(MondayConfigError) as exc:
        MondayAdapter(_cfg(todo_labels=["Fix"])).search()
    msg = str(exc.value)
    assert "integrations.monday.todo_labels" in msg
    assert "'Fix'" in msg                        # the bad label, named
    assert "'Fixing'" in msg and "'Ready for Dev'" in msg   # what IS on the board


def test_one_bad_label_among_valid_ones_fails_the_whole_call(token, monkeypatch):
    """The decision, tested: a partial config is a STOP, not a footnote.

    Continuing on the valid labels would narrow intake to a scope the operator
    never chose, and the work that stops arriving is indistinguishable from
    work nobody filed — the exact confusion this adapter refuses everywhere
    else. Only the BAD label is blamed, so the fix needs no diffing.
    """
    board = [_item("1", "a", status="Ready for Dev")]
    monkeypatch.setattr(httpx, "post", _serving(_constant(_items_payload(board))))
    adapter = MondayAdapter(_cfg(todo_labels=["Ready for Dev", "Redy for Dev"]))
    with pytest.raises(MondayConfigError) as exc:
        adapter.search()
    blamed = str(exc.value).split("which column")[0]
    assert "'Redy for Dev'" in blamed
    assert "'Ready for Dev'" not in blamed       # the valid one is not blamed


def test_an_empty_todo_labels_list_still_validates_the_status_column(
        token, monkeypatch):
    """Two independent checks. Skipping the column check when there are no
    labels to check would leave whole-board mode — the mode that pulls
    EVERYTHING — running on a column that may not exist."""
    monkeypatch.setattr(httpx, "post", _serving(_constant(_items_payload([_item()]))))
    with pytest.raises(MondayConfigError, match="TITLE"):
        MondayAdapter(_cfg(status_column="Status", todo_labels=[])).search()


def test_the_config_check_costs_one_cached_request_not_one_per_page_or_item(
        token, monkeypatch):
    """It reuses `board_columns()`'s cache: ONE request per adapter, for any
    number of searches, pages or items. A per-item round-trip would make the
    fix cost more than the bug it closes."""
    kinds = []
    pages = [_Resp(_items_payload([_item("1"), _item("2")], cursor="C1")),
             _Resp(_items_payload([_item("3")], cursor=None)),
             _Resp(_items_payload([_item("4")], cursor=None))]

    def fake_post(url, headers=None, timeout=None, json=None):
        if "NoHumanColumns" in json["query"]:
            kinds.append("columns")
            return _Resp(_columns_payload())
        kinds.append("items")
        return pages.pop(0)

    monkeypatch.setattr(httpx, "post", fake_post)
    adapter = MondayAdapter(_cfg())
    assert len(adapter.search()) == 3        # 3 items over 2 pages
    assert len(adapter.search()) == 1        # a second search, same adapter
    assert kinds == ["columns", "items", "items", "items"]
    assert kinds.count("columns") == 1


# --------------------------------------------------------------------------- #
# Search: the page bound is a partial answer, and says so                      #
# --------------------------------------------------------------------------- #

def test_stopping_at_the_page_bound_reports_the_result_as_partial(
        token, monkeypatch, caplog):
    """The same failure class at the other end: a board that always hands back
    a cursor stops at the bound, and the short list used to be returned as if
    it were the whole board — 20 pages, 2000 items, zero log records."""
    calls = []

    def items_post(url, headers=None, timeout=None, json=None):
        calls.append(1)
        return _Resp(_items_payload([_item(str(len(calls)))], cursor="MORE"))

    monkeypatch.setattr(httpx, "post", _serving(items_post))
    adapter = MondayAdapter(_cfg())
    with caplog.at_level(logging.WARNING, logger="no_human.intake.monday"):
        got = adapter.search()
    assert len(calls) == MAX_PAGES == 20      # the bound itself still holds
    assert len(got) == MAX_PAGES
    assert adapter.last_search_truncated is True
    assert "PARTIAL" in caplog.text
    assert str(MAX_PAGES) in caplog.text      # it says WHERE it stopped


def test_a_complete_pull_is_not_reported_as_partial(token, monkeypatch, caplog):
    """The flag has to be cleared by a complete pull, or every later poll on a
    board that fits in one page would cry truncation for ever."""
    monkeypatch.setattr(httpx, "post", _serving(_constant(_items_payload([_item()]))))
    adapter = MondayAdapter(_cfg())
    adapter.last_search_truncated = True      # as if an earlier pull was short
    with caplog.at_level(logging.WARNING, logger="no_human.intake.monday"):
        assert len(adapter.search()) == 1
    assert adapter.last_search_truncated is False
    assert "PARTIAL" not in caplog.text


# --------------------------------------------------------------------------- #
# normalize                                                                    #
# --------------------------------------------------------------------------- #

def test_normalize_maps_the_item_onto_a_task(token):
    task = MondayAdapter(_cfg()).normalize(_item())
    assert task.source == "monday"
    assert task.external_id == "2000000001"
    assert task.title == "Home page is slow to load"
    mon = task.context["monday"]
    assert mon["id"] == "2000000001"
    assert mon["board_id"] == BOARD
    assert mon["status"] == "Ready for Dev"
    assert mon["status_column"] == STATUS_COL
    assert mon["group"] == "Incoming  Bugs"
    assert mon["url"].endswith("/pulses/2000000001")


def test_normalize_keys_on_the_numeric_id_not_a_board_specific_key_column(token):
    """The operator's board renders a readable "BUG-002" from an `item_id`
    column configured on THAT board. Keying dedupe on it would work on one
    board and break on the next; the numeric id is the only identifier monday
    itself guarantees."""
    task = MondayAdapter(_cfg()).normalize(_item())
    assert task.external_id == "2000000001"
    assert task.external_id != "BUG-002"
    assert task.context["monday"]["columns"]["Bug ID"] == "BUG-002"   # kept, not keyed on


def test_normalize_builds_the_description_from_the_columns_because_monday_has_none(
        token):
    """monday items have no description field — the free text lives in the
    board's own columns. So the description is the item's non-empty column
    values, by TITLE, carrying a long-text column through verbatim. Empty
    columns are dropped rather than rendered as blank noise."""
    task = MondayAdapter(_cfg()).normalize(_item())
    assert "Details: Repro:" in task.description
    assert "page loads under 2s" in task.description
    assert "Status: Ready for Dev" in task.description
    assert "Reporter" not in task.description        # empty column, dropped
    # Checklist items in a long-text column become acceptance criteria, the
    # same extraction Jira and Linear run.
    assert task.acceptance_criteria == ["page loads under 2s", "no 500s"]


def test_normalize_survives_a_sparse_item(token):
    task = MondayAdapter(_cfg()).normalize({"id": "9"})
    assert task.external_id == "9"
    assert task.title == "9"
    assert task.description == ""
    assert task.acceptance_criteria == []
    assert task.context["monday"]["status"] == ""


# --------------------------------------------------------------------------- #
# Columns and label discovery                                                  #
# --------------------------------------------------------------------------- #

def test_status_labels_are_parsed_out_of_the_settings_json_string(token, monkeypatch):
    """`settings_str` is a JSON *string* on the column, and `labels` inside it
    is an {index: label} dict with SPARSE, non-contiguous indices. Treating it
    as a list, or as an already-parsed object, gets nothing."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(_columns_payload()))
    labels = MondayAdapter(_cfg()).status_labels()
    assert labels["14"] == "Ready for Dev"
    assert labels["9"] == "Awaiting Review"
    assert labels["5"] == ""                 # the real board has an empty label
    assert "6" not in labels                 # indices are sparse, not a range


def test_status_labels_refuses_a_column_that_is_not_a_status_column(token, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(_columns_payload()))
    with pytest.raises(MondayConfigError, match="not 'status'"):
        MondayAdapter(_cfg(status_column="long_text")).status_labels()


def test_an_unknown_status_column_names_the_columns_that_do_exist(token, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(_columns_payload()))
    with pytest.raises(MondayConfigError) as exc:
        MondayAdapter(_cfg(status_column="Status")).status_labels()
    # A title was passed where an id belongs — the most likely mistake, so the
    # error lists the real ids rather than only saying "not found".
    assert STATUS_COL in str(exc.value)


def test_board_columns_are_cached_per_adapter(token, monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", lambda url, headers=None, timeout=None, json=None:
                        calls.append(1) or _Resp(_columns_payload()))
    a = MondayAdapter(_cfg())
    a.board_columns()
    a.board_columns()
    assert len(calls) == 1
    a.board_columns(refresh=True)
    assert len(calls) == 2


# --------------------------------------------------------------------------- #
# Write-back: updates + status-label moves                                     #
# --------------------------------------------------------------------------- #

def test_comment_is_a_noop_when_write_back_is_off(token, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not write"))
    assert MondayAdapter(_cfg()).comment("2000000001", "hi") is False


def test_comment_posts_create_update_when_write_back_is_on(token, monkeypatch):
    seen = {}
    monkeypatch.setattr(httpx, "post", lambda url, headers=None, timeout=None, json=None:
                        seen.update(json=json) or
                        _Resp({"data": {"create_update": {"id": "u1"}}}))
    assert MondayAdapter(_cfg(write_back=True)).comment("2000000001", "note") is True
    assert "create_update" in seen["json"]["query"]
    assert seen["json"]["variables"] == {"itemId": "2000000001", "body": "note"}


def test_comment_reports_false_when_monday_returns_no_update_id(token, monkeypatch):
    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: _Resp({"data": {"create_update": None}}))
    assert MondayAdapter(_cfg(write_back=True)).comment("2000000001", "note") is False


def test_transition_is_a_noop_when_write_back_is_off(token, monkeypatch):
    # `in_progress_label` is set DELIBERATELY: with it blank, the move is a
    # no-op for the unrelated reason that no label is configured, and removing
    # the write_back gate entirely would still make no request — so the test
    # would pass while proving nothing. (Caught by mutation M12.)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not write"))
    adapter = MondayAdapter(_cfg(in_progress_label="Fixing"))
    assert adapter.transition("2000000001", "in_progress") is False


def test_transition_sends_the_operators_own_label_as_a_simple_value(token, monkeypatch):
    seen = []

    def fake_post(url, headers=None, timeout=None, json=None):
        seen.append(json)
        if "columns" in json["query"]:
            return _Resp(_columns_payload())
        return _Resp({"data": {"change_simple_column_value": {"id": "2000000001"}}})

    monkeypatch.setattr(httpx, "post", fake_post)
    adapter = MondayAdapter(_cfg(write_back=True, in_progress_label="Fixing"))
    assert adapter.transition("2000000001", "in_progress") is True
    assert seen[-1]["variables"] == {
        "boardId": BOARD, "itemId": "2000000001",
        "columnId": STATUS_COL, "value": "Fixing",
    }


def test_transition_never_offers_to_create_a_missing_label(token, monkeypatch):
    """`create_labels_if_missing: true` would let a typo'd config silently add
    a new status to the operator's board. The mutation must not carry it."""
    def fake_post(url, headers=None, timeout=None, json=None):
        if "columns" in json["query"]:
            return _Resp(_columns_payload())
        assert "create_labels_if_missing" not in json["query"]
        return _Resp({"data": {"change_simple_column_value": {"id": "x"}}})

    monkeypatch.setattr(httpx, "post", fake_post)
    MondayAdapter(_cfg(write_back=True, done_label="Fixed")).transition("1", "done")


def test_a_configured_label_the_board_does_not_have_is_refused_loudly(token, monkeypatch):
    """The monday analogue of Linear's "never a hardcoded state id": the label
    comes from config, but it is checked against the board's REAL labels before
    it is written. Otherwise the write fails at the API with a message about
    column values, three layers from the config typo that caused it."""
    def fake_post(url, headers=None, timeout=None, json=None):
        assert "columns" in json["query"], "must not attempt the write"
        return _Resp(_columns_payload())

    monkeypatch.setattr(httpx, "post", fake_post)
    adapter = MondayAdapter(_cfg(write_back=True, done_label="Shipped"))
    with pytest.raises(MondayConfigError) as exc:
        adapter.transition("2000000001", "done")
    assert "Shipped" in str(exc.value)
    assert "Fixed" in str(exc.value)          # it lists what IS available


def test_transition_is_a_noop_when_that_label_is_simply_unset(token, monkeypatch):
    """An operator who left `done_label` blank has opted out of that move. That
    is not a failure and must not raise — but it must also not write."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not write"))
    assert MondayAdapter(_cfg(write_back=True)).transition("2000000001", "done") is False


@pytest.mark.parametrize("bad", ["started", "completed", "in progress", ""])
def test_an_unknown_state_meaning_is_refused_not_silently_dropped(token, monkeypatch, bad):
    """The poller speaks in MEANINGS, not labels. Linear's vocabulary
    ("started"/"completed") is the likeliest wrong guess here, and passing it
    must fail rather than no-op."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not write"))
    with pytest.raises(ValueError, match="not a monday state meaning"):
        MondayAdapter(_cfg(write_back=True)).transition("2000000001", bad)


def test_the_state_meanings_are_exactly_the_three_config_keys(token):
    assert STATE_MEANINGS == ("todo", "in_progress", "done")


def test_item_status_reads_the_current_label(token, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp({"data": {"items": [
        {"id": "2000000001", "name": "x",
         "column_values": [{"id": STATUS_COL, "text": "Fixed"}]}]}}))
    assert MondayAdapter(_cfg()).item_status("2000000001") == "Fixed"


# --------------------------------------------------------------------------- #
# Poller                                                                       #
# --------------------------------------------------------------------------- #

class _FakeAdapter:
    default_repo = None
    done_label = "Fixed"

    def __init__(self, items=None, *, raises=None, cfg=None):
        self.items = items or []
        self.raises = raises
        self.comments = []
        self.transitions = []
        self.statuses = {}
        self._cfg = cfg or _cfg()

    def search(self):
        if self.raises:
            raise self.raises
        return self.items

    def normalize(self, item):
        return MondayAdapter(self._cfg).normalize(item)

    def comment(self, item_id, body):
        self.comments.append((item_id, body))
        return True

    def transition(self, item_id, meaning):
        self.transitions.append((item_id, meaning))
        return True

    def item_status(self, item_id):
        return self.statuses.get(item_id, "Ready for Dev")


@pytest.mark.asyncio
async def test_poll_creates_one_task_per_new_item_and_dedupes(store):
    adapter = _FakeAdapter([_item("1"), _item("2")])
    poller = MondayPoller(adapter, store, config=_cfg())
    r1 = await poller.poll_once()
    assert (r1.created, r1.skipped, r1.seen) == (2, 0, 2)
    assert sorted(r1.numbers) == ["1", "2"]
    r2 = await poller.poll_once()
    assert (r2.created, r2.skipped) == (0, 2)   # deduped by (source, external_id)


@pytest.mark.asyncio
async def test_a_truncated_pull_is_reported_on_the_operators_own_channel(store):
    """The adapter's log line alone is not enough: the poll otherwise SUCCEEDS
    — tasks are created, nothing errors, the event stream says "created N" —
    so a partial board reads as a complete one everywhere an operator looks."""
    events = []
    adapter = _FakeAdapter([_item()])
    adapter.last_search_truncated = True
    poller = MondayPoller(adapter, store, config=_cfg(),
                          on_event=lambda k, t: events.append((k, t)))
    await poller.poll_once()
    assert "monday_poll_truncated" in [k for k, _ in events]
    (text,) = [t for k, t in events if k == "monday_poll_truncated"]
    assert "PARTIAL" in text


@pytest.mark.asyncio
async def test_a_complete_pull_reports_no_truncation(store):
    events = []
    poller = MondayPoller(_FakeAdapter([_item()]), store, config=_cfg(),
                          on_event=lambda k, t: events.append((k, t)))
    await poller.poll_once()
    assert "monday_poll_truncated" not in [k for k, _ in events]
    assert events                              # ... but the poll DID report


@pytest.mark.asyncio
async def test_a_transport_error_is_reported_and_creates_nothing(store, caplog):
    events = []
    poller = MondayPoller(_FakeAdapter(raises=MondayError("boom")), store,
                          config=_cfg(), on_event=lambda k, t: events.append((k, t)))
    with caplog.at_level(logging.WARNING, logger="no_human.intake.monday_poll"):
        result = await poller.poll_once()
    assert result.created == 0
    assert events and events[0][0] == "monday_poll_error"
    assert not await store.list_tasks()          # never a half-created task


@pytest.mark.asyncio
async def test_throttling_is_named_as_such_not_reported_as_a_broken_api(store, caplog):
    """A log line saying only "non-JSON response" would read as a broken API;
    this one has to say it is retryable throttling, and why the body is HTML."""
    events = []
    poller = MondayPoller(
        _FakeAdapter(raises=MondayRateLimited("throttled", code="RATE_LIMIT_EXCEEDED",
                                              status=429)),
        store, config=_cfg(), on_event=lambda k, t: events.append((k, t)))
    with caplog.at_level(logging.WARNING, logger="no_human.intake.monday_poll"):
        await poller.poll_once()
    assert "rate limited" in events[0][1]
    assert "429" in events[0][1]


@pytest.mark.asyncio
async def test_a_config_error_gets_its_own_event_kind_not_a_transport_one(store, caplog):
    """Ticking again never fixes an unset board_id. Reporting it through the
    same channel as a network blip buries a permanent misconfiguration in
    retry noise."""
    events = []
    poller = MondayPoller(
        _FakeAdapter(raises=MondayConfigError("set integrations.monday.board_id")),
        store, config=_cfg(), on_event=lambda k, t: events.append((k, t)))
    with caplog.at_level(logging.WARNING, logger="no_human.intake.monday_poll"):
        await poller.poll_once()
    assert events[0][0] == "monday_config_error"
    assert "board_id" in events[0][1]


@pytest.mark.asyncio
async def test_write_back_is_off_by_default(store):
    """write_back=False must post NOTHING — no update, no status move."""
    adapter = _FakeAdapter([_item()])
    poller = MondayPoller(adapter, store, config=_cfg())
    await poller.poll_once()
    # The task must be in a status that WOULD produce a note and a transition,
    # otherwise "nothing was written" proves nothing about the opt-in gate.
    (task,) = await store.list_tasks()
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)
    assert await poller.sync_statuses() == 0
    assert adapter.comments == [] and adapter.transitions == []


@pytest.mark.asyncio
async def test_write_back_comments_and_transitions_once_per_change(store):
    adapter = _FakeAdapter([_item()])
    poller = MondayPoller(adapter, store, config=_cfg(write_back=True))
    await poller.poll_once()
    (task,) = await store.list_tasks()

    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)
    assert await poller.sync_statuses() == 1
    assert adapter.transitions == [("2000000001", "in_progress")]
    assert len(adapter.comments) == 1

    # Same status again: no duplicate comment, no duplicate transition.
    assert await poller.sync_statuses() == 0
    assert adapter.transitions == [("2000000001", "in_progress")]
    assert len(adapter.comments) == 1


@pytest.mark.asyncio
async def test_the_idempotency_marker_survives_a_reload_from_the_store(store):
    """The marker has to live on the persisted task, not in poller memory — a
    fresh poller (every `nh serve` restart is one) must not re-post the note."""
    adapter = _FakeAdapter([_item()])
    cfg = _cfg(write_back=True)
    await MondayPoller(adapter, store, config=cfg).poll_once()
    (task,) = await store.list_tasks()
    await store.set_status(task, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})
    await MondayPoller(adapter, store, config=cfg).sync_statuses()
    assert len(adapter.comments) == 1

    (task,) = await store.list_tasks()
    assert task.context["monday"]["nh_synced_status"] == TaskStatus.DONE.value
    # A brand-new poller over the same store: still exactly one.
    await MondayPoller(adapter, store, config=cfg).sync_statuses()
    assert len(adapter.comments) == 1
    assert adapter.transitions == [("2000000001", "done")]


@pytest.mark.asyncio
async def test_a_task_with_no_item_id_is_skipped_loudly_not_guessed(store, caplog):
    adapter = _FakeAdapter()
    poller = MondayPoller(adapter, store, config=_cfg(write_back=True))
    task = Task.new("orphan", source="monday", external_id="777")
    task.context = {"monday": {"url": "x"}}       # no "id"
    task.status = TaskStatus.IMPLEMENTING
    await store.create_task(task)
    with caplog.at_level(logging.WARNING, logger="no_human.intake.monday_poll"):
        assert await poller.sync_statuses() == 0
    assert adapter.comments == [] and adapter.transitions == []
    assert "777" in caplog.text and "cannot write back" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [TaskStatus.BLOCKED, TaskStatus.ESCALATED,
                                    TaskStatus.FAILED, TaskStatus.AWAITING_INPUT])
async def test_off_ramp_statuses_comment_but_never_transition(store, status):
    adapter = _FakeAdapter([_item()])
    poller = MondayPoller(adapter, store, config=_cfg(write_back=True))
    await poller.poll_once()
    (task,) = await store.list_tasks()
    await store.set_status(task, status, validate=False)
    await poller.sync_statuses()
    assert adapter.transitions == []          # the board must never lie
    assert len(adapter.comments) == 1


@pytest.mark.asyncio
async def test_no_needs_a_human_note_when_a_human_already_moved_it_to_done(store):
    adapter = _FakeAdapter([_item()])
    adapter.statuses["2000000001"] = "Fixed"    # == the configured done_label
    poller = MondayPoller(adapter, store, config=_cfg(write_back=True))
    await poller.poll_once()
    (task,) = await store.list_tasks()
    await store.set_status(task, TaskStatus.ESCALATED, validate=False)
    await poller.sync_statuses()
    assert adapter.comments == []             # the note would have lied


@pytest.mark.asyncio
async def test_with_no_done_label_configured_the_note_is_posted_without_a_probe(store):
    """"Done" on a monday board is whatever label the operator named. With
    none named there IS no done, so the check must answer False — and must not
    spend a request asking."""
    class _NoProbe(_FakeAdapter):
        done_label = ""

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.probes = 0

        def item_status(self, item_id):
            # RECORDED, not raised. Raising here proves nothing: the poller
            # catches every exception from the probe and fails open, so a
            # missing guard would still post the note and the test would pass.
            # (Caught by mutation P7.)
            self.probes += 1
            return "Fixed"

    adapter = _NoProbe([_item()])
    poller = MondayPoller(adapter, store, config=_cfg(write_back=True))
    await poller.poll_once()
    (task,) = await store.list_tasks()
    await store.set_status(task, TaskStatus.ESCALATED, validate=False)
    await poller.sync_statuses()
    assert adapter.probes == 0                 # no request spent on a non-question
    assert len(adapter.comments) == 1


@pytest.mark.asyncio
async def test_a_failed_status_check_still_posts_the_needs_a_human_note(store, caplog):
    """Fail OPEN, deliberately. If the check cannot answer, suppressing is the
    worse error: the escalation is dropped permanently (the marker is written
    either way on the suppress path) and nothing ever says a human was needed."""
    class _BadStatus(_FakeAdapter):
        def item_status(self, item_id):
            raise MondayError("status read blew up")

    adapter = _BadStatus([_item()])
    poller = MondayPoller(adapter, store, config=_cfg(write_back=True))
    await poller.poll_once()
    (task,) = await store.list_tasks()
    await store.set_status(task, TaskStatus.ESCALATED, validate=False)
    with caplog.at_level(logging.WARNING, logger="no_human.intake.monday_poll"):
        await poller.sync_statuses()
    assert len(adapter.comments) == 1
    assert "escalated" in adapter.comments[0][1]
    assert "status check" in caplog.text       # the failure is surfaced, not hidden


@pytest.mark.asyncio
async def test_a_failed_comment_leaves_the_marker_unset_so_the_next_tick_retries(store):
    """A raised comment must not be recorded as synced: the status never
    changes again, so the only chance to say "a human is needed" is gone. The
    transition half is independent and keeps its own marker."""
    class _FlakyComment(_FakeAdapter):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.fail_next = True

        def comment(self, item_id, body):
            if self.fail_next:
                self.fail_next = False
                raise MondayError("comment blew up")
            return super().comment(item_id, body)

    adapter = _FlakyComment([_item()])
    poller = MondayPoller(adapter, store, config=_cfg(write_back=True))
    await poller.poll_once()
    (task,) = await store.list_tasks()
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)

    await poller.sync_statuses()
    assert adapter.comments == []              # the note did not land
    (task,) = await store.list_tasks()
    monday = task.context["monday"]
    assert "nh_synced_status" not in monday     # ... so it is NOT marked synced
    assert monday["nh_monday_transitions"] == ["in_progress"]   # the other half stands

    await poller.sync_statuses()               # next tick
    assert len(adapter.comments) == 1          # retried
    assert "started work" in adapter.comments[0][1]
    assert adapter.transitions == [("2000000001", "in_progress")]   # not re-transitioned
    (task,) = await store.list_tasks()
    assert task.context["monday"]["nh_synced_status"] == TaskStatus.IMPLEMENTING.value


@pytest.mark.asyncio
async def test_a_failed_transition_does_not_suppress_the_comment(store):
    class _BadTransition(_FakeAdapter):
        def transition(self, item_id, meaning):
            raise MondayError("nope")

    adapter = _BadTransition([_item()])
    poller = MondayPoller(adapter, store, config=_cfg(write_back=True))
    await poller.poll_once()
    (task,) = await store.list_tasks()
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)
    await poller.sync_statuses()
    assert len(adapter.comments) == 1         # independent idempotency markers


@pytest.mark.asyncio
async def test_a_non_monday_task_in_the_same_store_is_never_written_through_monday(
        store, caplog):
    """One store holds every source. Write-back must select on `source`, not
    just "has an external id" — a Linear task otherwise gets a work note posted
    through the monday API, onto whatever item its id happens to hit."""
    adapter = _FakeAdapter([_item()])
    poller = MondayPoller(adapter, store, config=_cfg(write_back=True))
    await poller.poll_once()

    linear = Task.new("a linear task", source="linear", external_id="ENG-1")
    linear.context = {"linear": {"id": "uuid-eng-1"}}
    linear.status = TaskStatus.ESCALATED       # would produce a note if considered
    await store.create_task(linear)

    (monday_task,) = [t for t in await store.list_tasks() if t.source == "monday"]
    await store.set_status(monday_task, TaskStatus.IMPLEMENTING, validate=False)

    with caplog.at_level(logging.WARNING, logger="no_human.intake.monday_poll"):
        assert await poller.sync_statuses() == 1        # the monday task only
    assert [item_id for item_id, _ in adapter.comments] == ["2000000001"]
    assert "ENG-1" not in caplog.text
    (linear_after,) = [t for t in await store.list_tasks() if t.source == "linear"]
    assert linear_after.context == {"linear": {"id": "uuid-eng-1"}}   # untouched


@pytest.mark.asyncio
async def test_default_repo_is_applied_to_polled_tasks(store, tmp_path):
    adapter = _FakeAdapter([_item()])
    await MondayPoller(adapter, store, config=_cfg(default_repo=str(tmp_path))).poll_once()
    (task,) = await store.list_tasks()
    assert task.repo_path == str(tmp_path)


@pytest.mark.asyncio
async def test_tick_runs_write_back_even_though_it_is_a_separate_half(store):
    adapter = _FakeAdapter([_item()])
    poller = MondayPoller(adapter, store, config=_cfg(write_back=True))
    await poller.tick()
    (task,) = await store.list_tasks()
    await store.set_status(task, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})
    await poller.tick()
    assert adapter.transitions == [("2000000001", "done")]


# --------------------------------------------------------------------------- #
# Config + registry wiring                                                     #
# --------------------------------------------------------------------------- #

def test_the_shipped_defaults_are_off_and_carry_no_credential():
    from no_human.config import DEFAULT_CONFIG

    mon = (DEFAULT_CONFIG["integrations"])["monday"]
    assert mon["enabled"] is False
    assert mon["write_back"] is False          # writes are opt-in, always
    assert mon["board_id"] == "" and mon["status_column"] == ""
    assert mon["todo_labels"] == []
    # The token lives in ~/.no_human/.env and must never gain a config key.
    assert not any("token" in k or "key" in k for k in mon)


def test_monday_is_registered_as_an_issue_tracker():
    from no_human.integrations import KIND_BY_NAME, SETUP_SECRET_ENV, list_integrations

    assert KIND_BY_NAME["monday"] == "issue_tracker"
    assert SETUP_SECRET_ENV["monday"] == ("MONDAY_API_TOKEN",)
    assert "monday" in {s.name for s in list_integrations({})}


def test_a_board_without_a_status_column_does_not_report_itself_configured():
    """Green next to an integration that cannot run is worse than red: monday
    intake RAISES without a status column, so 'configured' would be a lie."""
    from no_human.integrations import _monday_status

    half = {"integrations": {"monday": {"enabled": True, "board_id": BOARD}}}
    st = _monday_status(half)
    assert st.configured is False
    assert "status_column" in st.detail
    full = _monday_status(_cfg())
    assert full.configured is True and BOARD in full.detail
