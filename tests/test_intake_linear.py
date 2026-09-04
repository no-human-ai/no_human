"""Linear intake: GraphQL search → normalize → dedupe, plus opt-in write-back.

Mirrors tests/test_jira_intake.py's shape. All HTTP is mocked; the API key is
read from the env and must never appear in a log line or an exception message.
"""

import logging

import httpx
import pytest

from no_human.core.task import Task, TaskStatus
from no_human.intake.linear import (
    API_URL,
    DEFAULT_STATE_TYPES,
    STATE_TYPES,
    LinearAdapter,
    LinearAuthError,
    LinearConfigError,
    LinearError,
    LinearRateLimited,
)
from no_human.intake.linear_poll import LinearPoller


def _cfg(**over):
    lin = {"enabled": True, "team_key": "ENG"}
    lin.update(over)
    return {"integrations": {"linear": lin}}


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _issue(identifier="ENG-1", **over):
    issue = {
        "id": "uuid-eng-1",
        "identifier": identifier,
        "title": "Add greet(name)",
        "description": "Do the thing.\n- [ ] returns 'hi, X'\n- [ ] has a test",
        "url": f"https://linear.app/acme/issue/{identifier}",
        "priority": 2,
        "priorityLabel": "High",
        "createdAt": "2026-07-01T00:00:00.000Z",
        "updatedAt": "2026-07-02T00:00:00.000Z",
        "state": {"id": "st-backlog", "name": "Backlog", "type": "backlog"},
        "assignee": {"id": "u1", "name": "Ada", "displayName": "ada"},
        "labels": {"nodes": [{"id": "l1", "name": "Bug"}]},
        "team": {"id": "t1", "key": "ENG", "name": "Engineering"},
    }
    issue.update(over)
    return issue


def _states_payload():
    """A team's real workflow states. Ids are opaque and positions are
    deliberately out of order, so a test that hardcoded an id — or that took
    the FIRST match instead of the lowest position — would fail."""
    return {"data": {"workflowStates": {"nodes": [
        {"id": "st-triage", "name": "Triage", "type": "triage", "position": 0},
        {"id": "st-backlog", "name": "Backlog", "type": "backlog", "position": 1},
        {"id": "st-review", "name": "In Review", "type": "started", "position": 3},
        {"id": "st-progress", "name": "In Progress", "type": "started", "position": 2},
        {"id": "st-done", "name": "Done", "type": "completed", "position": 4},
        {"id": "st-cancelled", "name": "Cancelled", "type": "canceled", "position": 5},
    ], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_SEKRET")


# --------------------------------------------------------------------------- #
# Configuration + auth                                                         #
# --------------------------------------------------------------------------- #

def test_adapter_configured(key, monkeypatch):
    assert LinearAdapter(_cfg()).configured is True
    assert LinearAdapter(_cfg(team_key="")).configured is False
    monkeypatch.delenv("LINEAR_API_KEY")
    assert LinearAdapter(_cfg()).configured is False   # no key → not configured


def test_auth_header_is_the_raw_key_not_bearer(key, monkeypatch):
    """Linear's documented personal-key header is `Authorization: <key>`.
    Sending `Bearer <key>` is a 401 that looks like a bad key."""
    seen = {}

    def fake_post(url, headers=None, timeout=None, json=None):
        seen.update(url=url, headers=headers, json=json)
        return _Resp({"data": {"issues": {"nodes": [], "pageInfo": {}}}})

    monkeypatch.setattr(httpx, "post", fake_post)
    LinearAdapter(_cfg()).search()
    assert seen["url"] == API_URL
    assert seen["headers"]["Authorization"] == "lin_api_SEKRET"
    assert not seen["headers"]["Authorization"].startswith("Bearer")


def test_missing_key_raises_auth_error_before_any_request(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not call the API"))
    with pytest.raises(LinearAuthError, match="LINEAR_API_KEY"):
        LinearAdapter(_cfg()).search()


def test_the_api_key_never_appears_in_an_error(key, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(
        {"errors": [{"message": "Authentication required, not authenticated",
                     "extensions": {"code": "AUTHENTICATION_ERROR"}}]}, 401))
    with pytest.raises(LinearAuthError) as exc:
        LinearAdapter(_cfg()).search()
    assert "SEKRET" not in str(exc.value)


def test_the_api_key_never_appears_in_a_NON_auth_error(key, monkeypatch):
    """The leak guard above only walks the AUTHENTICATION_ERROR path.

    Every other GraphQL failure builds its LinearError in the same place, and a
    validation error is the one an operator is most likely to paste into an
    issue report — so the plain `LinearError` branch needs its own pin, not the
    subclass's.
    """
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(
        {"errors": [{"message": "Argument Validation Error",
                     "extensions": {"code": "INVALID_INPUT"}}]}, 200))
    with pytest.raises(LinearError) as exc:
        LinearAdapter(_cfg()).search()
    err = exc.value
    assert type(err) is LinearError          # the base branch, not a subclass
    assert err.code == "INVALID_INPUT"
    for rendered in (str(err), repr(err), str(err.args)):
        assert "SEKRET" not in rendered
        assert "lin_api" not in rendered     # nor a truncated/prefixed form


# --------------------------------------------------------------------------- #
# Error classification — status alone does not classify a Linear failure       #
# --------------------------------------------------------------------------- #

def test_rate_limiting_is_http_400_with_a_code_not_429(key, monkeypatch):
    """The single most likely integration bug: Linear reports throttling as
    HTTP 400 + extensions.code == RATELIMITED. Branching on 429 never fires,
    and treating 400 as a permanent client error gives up on a retryable one."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(
        {"errors": [{"message": "rate limited",
                     "extensions": {"code": "RATELIMITED"}}]}, 400))
    with pytest.raises(LinearRateLimited) as exc:
        LinearAdapter(_cfg()).search()
    assert exc.value.code == "RATELIMITED"
    assert exc.value.status == 400


def test_field_errors_at_http_200_are_still_errors(key, monkeypatch):
    """GraphQL partial failure: 200 with an errors array. Returning the partial
    data as if it were a clean result would silently drop issues."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(
        {"data": {"issues": None},
         "errors": [{"message": "Field error", "extensions": {"code": "INVALID_INPUT"}}]}, 200))
    with pytest.raises(LinearError) as exc:
        LinearAdapter(_cfg()).search()
    assert exc.value.code == "INVALID_INPUT"


def test_non_2xx_with_a_well_formed_body_still_raises(key, monkeypatch):
    """The status is checked INDEPENDENTLY of whether `data` parsed.

    A body that happens to deserialize (a gateway/proxy error page rendered as
    JSON, a partial response) must not be accepted just because `data` is a
    dict — otherwise a 5xx would return an empty issue list and the poll would
    silently create nothing while reporting success.
    """
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(
        {"data": {"issues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}, 503))
    with pytest.raises(LinearError, match="503"):
        LinearAdapter(_cfg()).search()


def test_non_2xx_with_a_null_data_also_raises(key, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp({"data": None}, 503))
    with pytest.raises(LinearError):
        LinearAdapter(_cfg()).search()


def test_non_json_body_raises_rather_than_returning_empty(key, monkeypatch):
    class _Bad:
        status_code = 502

        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Bad())
    with pytest.raises(LinearError, match="non-JSON"):
        LinearAdapter(_cfg()).search()


# --------------------------------------------------------------------------- #
# Search                                                                       #
# --------------------------------------------------------------------------- #

def test_search_filter_is_operator_authored_and_scoped_to_the_team(key, monkeypatch):
    seen = {}
    monkeypatch.setattr(httpx, "post", lambda url, headers=None, timeout=None, json=None:
                        seen.update(json=json) or
                        _Resp({"data": {"issues": {"nodes": [], "pageInfo": {}}}}))
    LinearAdapter(_cfg(label="Bug")).search()
    flt = seen["json"]["variables"]["filter"]
    assert flt["team"] == {"key": {"eq": "ENG"}}
    assert flt["state"] == {"type": {"in": list(DEFAULT_STATE_TYPES)}}
    assert flt["labels"] == {"name": {"in": ["Bug"]}}
    assert "updatedAt" not in flt


def test_default_state_types_take_only_unstarted_work(key):
    assert set(DEFAULT_STATE_TYPES) == {"triage", "backlog", "unstarted"}
    assert set(DEFAULT_STATE_TYPES) <= set(STATE_TYPES)


def test_state_types_are_the_seven_documented_values_including_duplicate():
    # WorkflowState.type is a plain String in the schema, so nothing validates
    # a literal at query time. Missing "duplicate" from an exhaustive mapping
    # throws on real data.
    assert STATE_TYPES == ("triage", "backlog", "unstarted", "started",
                           "completed", "canceled", "duplicate")


def test_search_follows_the_cursor_so_a_second_page_is_not_dropped(key, monkeypatch):
    pages = [
        _Resp({"data": {"issues": {
            "nodes": [_issue("ENG-1")],
            "pageInfo": {"hasNextPage": True, "endCursor": "cur1"}}}}),
        _Resp({"data": {"issues": {
            "nodes": [_issue("ENG-2")],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}}),
    ]
    afters = []

    def fake_post(url, headers=None, timeout=None, json=None):
        # search() also runs the intake-config check (teams/labels). Answer
        # those with a body that carries no such connection, which is the
        # fail-open path, so this test measures ISSUE paging only.
        if "NoHumanIssues" not in json["query"]:
            return _Resp({"data": {}})
        afters.append(json["variables"]["after"])
        return pages.pop(0)

    monkeypatch.setattr(httpx, "post", fake_post)
    issues = LinearAdapter(_cfg()).search()
    assert [i["identifier"] for i in issues] == ["ENG-1", "ENG-2"]
    assert afters == [None, "cur1"]      # the cursor was actually passed through


def test_search_stops_paging_when_the_cursor_is_missing(key, monkeypatch):
    calls = []

    def fake_post(url, headers=None, timeout=None, json=None):
        if "NoHumanIssues" not in json["query"]:
            return _Resp({"data": {}})     # config check: fail open, not counted
        calls.append(1)
        return _Resp({"data": {"issues": {
            "nodes": [_issue()],
            "pageInfo": {"hasNextPage": True, "endCursor": None}}}})

    monkeypatch.setattr(httpx, "post", fake_post)
    LinearAdapter(_cfg()).search()
    assert len(calls) == 1      # no infinite loop on a malformed pageInfo


# --------------------------------------------------------------------------- #
# normalize                                                                    #
# --------------------------------------------------------------------------- #

def test_normalize_maps_the_issue_onto_a_task(key):
    task = LinearAdapter(_cfg()).normalize(_issue())
    assert task.source == "linear"
    assert task.external_id == "ENG-1"          # human identifier, for dedupe
    assert task.title == "Add greet(name)"
    assert task.acceptance_criteria == ["returns 'hi, X'", "has a test"]
    lin = task.context["linear"]
    assert lin["url"] == "https://linear.app/acme/issue/ENG-1"
    assert lin["state"] == "Backlog" and lin["state_type"] == "backlog"
    assert lin["labels"] == ["Bug"]
    assert lin["team"] == "ENG"


def test_normalize_keeps_the_node_uuid_because_mutations_need_it(key):
    """external_id is "ENG-1" but every mutation takes the UUID. Losing it
    would make write-back impossible (or, worse, guess)."""
    task = LinearAdapter(_cfg()).normalize(_issue())
    assert task.context["linear"]["id"] == "uuid-eng-1"
    assert task.context["linear"]["id"] != task.external_id


def test_normalize_survives_a_sparse_issue(key):
    task = LinearAdapter(_cfg()).normalize({"identifier": "ENG-9"})
    assert task.title == "ENG-9"
    assert task.description == ""
    assert task.acceptance_criteria == []
    assert task.context["linear"]["labels"] == []


# --------------------------------------------------------------------------- #
# Write-back: comments + type-matched state moves                              #
# --------------------------------------------------------------------------- #

def test_comment_is_a_noop_when_write_back_is_off(key, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not write"))
    assert LinearAdapter(_cfg()).comment("uuid-eng-1", "hi") is False


def test_comment_posts_the_mutation_when_write_back_is_on(key, monkeypatch):
    seen = {}
    monkeypatch.setattr(httpx, "post", lambda url, headers=None, timeout=None, json=None:
                        seen.update(json=json) or
                        _Resp({"data": {"commentCreate": {"success": True}}}))
    assert LinearAdapter(_cfg(write_back=True)).comment("uuid-eng-1", "note") is True
    assert "commentCreate" in seen["json"]["query"]
    assert seen["json"]["variables"] == {"issueId": "uuid-eng-1", "body": "note"}


def test_comment_reports_false_when_linear_says_success_false(key, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(
        {"data": {"commentCreate": {"success": False}}}))
    assert LinearAdapter(_cfg(write_back=True)).comment("uuid-eng-1", "note") is False


def test_transition_resolves_the_teams_own_state_never_a_hardcoded_id(key, monkeypatch):
    seen = []

    def fake_post(url, headers=None, timeout=None, json=None):
        seen.append(json)
        if "workflowStates" in json["query"]:
            return _Resp(_states_payload())
        return _Resp({"data": {"issueUpdate": {"success": True}}})

    monkeypatch.setattr(httpx, "post", fake_post)
    assert LinearAdapter(_cfg(write_back=True)).transition("uuid-eng-1", "started") is True
    # Lowest position among the two `started` states — In Progress (2), not
    # In Review (3), and not "the first one in the list".
    assert seen[-1]["variables"] == {"id": "uuid-eng-1", "stateId": "st-progress"}


def test_transition_to_completed_picks_the_completed_state(key, monkeypatch):
    def fake_post(url, headers=None, timeout=None, json=None):
        if "workflowStates" in json["query"]:
            return _Resp(_states_payload())
        assert json["variables"]["stateId"] == "st-done"
        return _Resp({"data": {"issueUpdate": {"success": True}}})

    monkeypatch.setattr(httpx, "post", fake_post)
    assert LinearAdapter(_cfg(write_back=True)).transition("uuid-eng-1", "completed") is True


@pytest.mark.parametrize("bad", ["canceled", "duplicate"])
def test_the_agent_refuses_to_close_an_issue(key, monkeypatch, bad):
    """Constraint #2: closing/cancelling is a human action. The team HAS a
    `canceled` state, so this must be refused by policy, not by absence."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not write"))
    with pytest.raises(ValueError, match="human action"):
        LinearAdapter(_cfg(write_back=True)).transition("uuid-eng-1", bad)


def test_an_unknown_state_type_is_refused_not_silently_dropped(key, monkeypatch):
    # state.type is an unvalidated String — a typo would otherwise be a no-op.
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not write"))
    with pytest.raises(ValueError, match="not a Linear workflow state type"):
        LinearAdapter(_cfg(write_back=True)).transition("uuid-eng-1", "in_progress")


def test_transition_is_a_noop_when_the_team_has_no_such_state(key, monkeypatch):
    def fake_post(url, headers=None, timeout=None, json=None):
        assert "workflowStates" in json["query"], "must not attempt the update"
        return _Resp({"data": {"workflowStates": {"nodes": [
            {"id": "st-backlog", "name": "Backlog", "type": "backlog", "position": 1}]}}})

    monkeypatch.setattr(httpx, "post", fake_post)
    assert LinearAdapter(_cfg(write_back=True)).transition("uuid-eng-1", "started") is False


def test_transition_is_a_noop_when_write_back_is_off(key, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not write"))
    assert LinearAdapter(_cfg()).transition("uuid-eng-1", "started") is False


def test_workflow_states_are_cached_per_adapter(key, monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", lambda url, headers=None, timeout=None, json=None:
                        calls.append(1) or _Resp(_states_payload()))
    a = LinearAdapter(_cfg())
    a.workflow_states()
    a.workflow_states()
    assert len(calls) == 1
    a.workflow_states(refresh=True)
    assert len(calls) == 2


def test_state_type_reads_the_current_state(key, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(
        {"data": {"issue": {"id": "uuid-eng-1", "identifier": "ENG-1",
                            "state": {"id": "st-done", "name": "Done", "type": "Completed"}}}}))
    assert LinearAdapter(_cfg()).state_type("uuid-eng-1") == "completed"   # lowercased


# --------------------------------------------------------------------------- #
# Config that matches nothing is REFUSED, not answered with an empty list.      #
#                                                                              #
# Measured against a real workspace before this was fixed: the correct config   #
# returned 4 issues, and an unset team key, a typo'd team key, a typo'd         #
# state_types and a label nothing carries each returned 0 — silently, with      #
# nothing anywhere reporting a problem. Linear applies all four filters         #
# server-side and answers a filter that matches nothing with an empty page and  #
# no error, so an empty page is the same thing a team with no open work looks   #
# like. Every test below pins one half of the distinction: a WRONG config       #
# raises, and a RIGHT config that happens to match nothing stays quiet.         #
# --------------------------------------------------------------------------- #

_WORKSPACE_TEAMS = ["NO", "ENG"]
_WORKSPACE_LABELS = ["Bug", "Feature"]


def _fake_workspace(issues=None, *, teams=_WORKSPACE_TEAMS, labels=_WORKSPACE_LABELS,
                    calls=None, teams_pages_forever=False, teams_error=False):
    """A scripted Linear workspace: answers the issues/teams/labels queries the
    way the real API does, including "a filter that matches nothing comes back
    as an empty page, not an error"."""
    nodes = [_issue("ENG-1")] if issues is None else issues

    def fake_post(url, headers=None, timeout=None, json=None):
        query = json["query"]
        if calls is not None:
            calls.append(query.strip().splitlines()[0])
        if "NoHumanTeams" in query:
            if teams_error:
                return _Resp({"errors": [{"message": "boom"}]}, status_code=400)
            return _Resp({"data": {"teams": {
                "nodes": [{"id": f"t-{k}", "key": k, "name": k} for k in teams],
                "pageInfo": {"hasNextPage": teams_pages_forever,
                             "endCursor": "cur" if teams_pages_forever else None}}}})
        if "NoHumanLabels" in query:
            return _Resp({"data": {"issueLabels": {
                "nodes": [{"id": f"l-{x}", "name": x} for x in labels],
                "pageInfo": {"hasNextPage": False, "endCursor": None}}}})
        # The issues search: apply the operator's filter the way Linear does.
        flt = json["variables"]["filter"]
        team = flt["team"]["key"]["eq"]
        types = flt["state"]["type"]["in"]
        wanted_labels = (flt.get("labels") or {}).get("name", {}).get("in")
        out = []
        for issue in nodes:
            if issue["team"]["key"] != team or issue["state"]["type"] not in types:
                continue
            if wanted_labels is not None:
                have = [n["name"] for n in issue["labels"]["nodes"]]
                if not any(x in have for x in wanted_labels):
                    continue
            out.append(issue)
        return _Resp({"data": {"issues": {
            "nodes": out, "pageInfo": {"hasNextPage": False, "endCursor": None}}}})

    return fake_post


def test_the_control_a_correct_config_still_returns_its_issues(key, monkeypatch):
    """The measurement every case below is read against. Without it, "0 issues"
    proves nothing: a fake that returns nothing for everything would make the
    whole section pass while the adapter did no work at all."""
    monkeypatch.setattr(httpx, "post", _fake_workspace())
    issues = LinearAdapter(_cfg()).search()
    assert [i["identifier"] for i in issues] == ["ENG-1"]


def test_an_unset_team_key_raises_instead_of_returning_nothing(key, monkeypatch):
    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: pytest.fail("must not reach the API"))
    with pytest.raises(LinearConfigError) as exc:
        LinearAdapter(_cfg(team_key="")).search()
    assert "integrations.linear.team_key" in str(exc.value)


def test_a_typod_team_key_is_refused_and_the_real_ones_are_named(key, monkeypatch):
    """The fault nothing caught: 'N0' for 'NO' is a real team key's shape, so
    the filter is well-formed and Linear answers it with an empty page."""
    monkeypatch.setattr(httpx, "post", _fake_workspace())
    with pytest.raises(LinearConfigError) as exc:
        LinearAdapter(_cfg(team_key="N0")).search()
    message = str(exc.value)
    assert "integrations.linear.team_key" in message and "'N0'" in message
    assert "'NO'" in message and "'ENG'" in message      # what it could have been


def test_a_team_key_that_differs_only_in_case_gets_the_did_you_mean(key, monkeypatch):
    monkeypatch.setattr(httpx, "post", _fake_workspace())
    with pytest.raises(LinearConfigError, match="Did you mean 'NO'"):
        LinearAdapter(_cfg(team_key="no")).search()


@pytest.mark.parametrize("bad", [["Backlog"], ["todo"], ["backlog", "in_progress"], [7]])
def test_a_state_type_outside_the_seven_is_refused_offline(key, monkeypatch, bad):
    """WorkflowState.type is a plain String, so Linear validates nothing: a
    typo is an empty result set, never a query error. This check costs no
    request at all — the seven values are a fixed documented list."""
    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: pytest.fail("must not reach the API"))
    with pytest.raises(LinearConfigError) as exc:
        LinearAdapter(_cfg(state_types=bad)).search()
    assert "integrations.linear.state_types" in str(exc.value)


def test_the_state_type_error_names_the_lowercase_value_it_meant(key, monkeypatch):
    monkeypatch.setattr(httpx, "post", _fake_workspace())
    with pytest.raises(LinearConfigError, match="did you mean 'backlog'"):
        LinearAdapter(_cfg(state_types=["Backlog"])).search()


def test_a_label_the_workspace_does_not_have_is_refused(key, monkeypatch):
    monkeypatch.setattr(httpx, "post", _fake_workspace())
    with pytest.raises(LinearConfigError) as exc:
        LinearAdapter(_cfg(label="nonexistent")).search()
    assert "integrations.linear.label" in str(exc.value)
    assert "'Bug'" in str(exc.value)


def test_state_type_problems_accepts_every_documented_value():
    from no_human.intake.linear import state_type_problems

    assert state_type_problems(list(STATE_TYPES)) == []
    assert state_type_problems(list(DEFAULT_STATE_TYPES)) == []
    assert state_type_problems("backlog")          # a bare string is not a list
    assert state_type_problems(["nope"])


# --- the other half: a RIGHT config that matches nothing must stay quiet ---- #

def test_a_correct_config_matching_zero_issues_does_not_raise(key, monkeypatch):
    """The legitimate empty case. A team whose backlog is empty right now is
    not a misconfiguration, and turning it into one would be a worse defect
    than the silence this change removes."""
    monkeypatch.setattr(httpx, "post", _fake_workspace(issues=[]))
    assert LinearAdapter(_cfg(team_key="NO")).search() == []


def test_a_real_label_that_nothing_currently_carries_does_not_raise(key, monkeypatch):
    monkeypatch.setattr(httpx, "post", _fake_workspace(
        issues=[_issue("ENG-1")], labels=["Bug", "Feature", "chore"]))
    # "chore" exists in the workspace; no issue carries it today.
    assert LinearAdapter(_cfg(label="chore")).search() == []


def test_a_state_type_the_team_happens_not_to_use_does_not_raise(key, monkeypatch):
    """'duplicate' is one of the seven and is therefore legal config, even on a
    team with no issue in it. Validity is about the CONFIG naming real things,
    never about the result being non-empty."""
    monkeypatch.setattr(httpx, "post", _fake_workspace())
    assert LinearAdapter(_cfg(state_types=["duplicate"])).search() == []


# --- and it must not become a new way for intake to break ------------------ #

def test_the_online_check_costs_one_pass_per_adapter_not_one_per_poll(key, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(httpx, "post", _fake_workspace(calls=calls))
    adapter = LinearAdapter(_cfg())
    adapter.search()
    adapter.search()
    adapter.search()
    assert sum("NoHumanTeams" in c for c in calls) == 1
    assert sum("NoHumanIssues" in c for c in calls) == 3


def test_a_verdict_of_misconfigured_is_remembered_without_re_asking(key, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(httpx, "post", _fake_workspace(calls=calls))
    adapter = LinearAdapter(_cfg(team_key="N0"))
    for _ in range(3):
        with pytest.raises(LinearConfigError):
            adapter.search()
    assert sum("NoHumanTeams" in c for c in calls) == 1
    assert not any("NoHumanIssues" in c for c in calls)


def test_a_failing_validation_query_never_becomes_a_config_error(key, monkeypatch):
    """Fail OPEN. If the check itself errors — a throttle, an outage, a query
    shape this workspace does not support — intake carries on. A validation
    that turned an unrelated API failure into "your config is wrong" would be
    worse than the silence it replaces."""
    monkeypatch.setattr(httpx, "post", _fake_workspace(teams_error=True))
    assert [i["identifier"] for i in LinearAdapter(_cfg()).search()] == ["ENG-1"]


def test_an_unknown_response_shape_is_not_evidence_about_the_config(key, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda url, headers=None, timeout=None, json=None:
                        _Resp({"data": {"issues": {"nodes": [_issue("ENG-1")],
                                                   "pageInfo": {}}}}))
    # Every query gets an issues-shaped body; the teams check cannot conclude
    # anything from it and must not try.
    assert len(LinearAdapter(_cfg()).search()) == 1


def test_a_team_list_that_never_ends_is_not_used_to_accuse_the_config(key, monkeypatch):
    """A membership test on an INCOMPLETE list would fail a config that is
    right, so the walk stopping at its bound skips the check."""
    monkeypatch.setattr(httpx, "post", _fake_workspace(
        teams=["OTHER"], teams_pages_forever=True))
    assert [i["identifier"] for i in LinearAdapter(_cfg()).search()] == ["ENG-1"]


def test_an_empty_team_list_is_not_used_to_accuse_the_config(key, monkeypatch):
    monkeypatch.setattr(httpx, "post", _fake_workspace(teams=[]))
    assert [i["identifier"] for i in LinearAdapter(_cfg()).search()] == ["ENG-1"]


def test_write_back_state_lookup_refuses_an_unset_team_key(key, monkeypatch):
    """The same silence through the other door: with no team key this query
    returns zero states, so every transition became a no-op that reported
    nothing."""
    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: pytest.fail("must not reach the API"))
    with pytest.raises(LinearConfigError, match="team_key"):
        LinearAdapter(_cfg(team_key="", write_back=True)).transition("uuid-1", "started")


# --------------------------------------------------------------------------- #
# Poller                                                                       #
# --------------------------------------------------------------------------- #

class _FakeAdapter:
    default_repo = None

    def __init__(self, issues=None, *, raises=None):
        self.issues = issues or []
        self.raises = raises
        self.comments = []
        self.transitions = []
        self.state_types = {}

    def search(self):
        if self.raises:
            raise self.raises
        return self.issues

    def normalize(self, issue):
        return LinearAdapter(_cfg()).normalize(issue)

    def comment(self, issue_id, body):
        self.comments.append((issue_id, body))
        return True

    def transition(self, issue_id, target):
        self.transitions.append((issue_id, target))
        return True

    def state_type(self, issue_id):
        return self.state_types.get(issue_id, "backlog")


@pytest.mark.asyncio
async def test_poll_creates_one_task_per_new_issue_and_dedupes(store):
    adapter = _FakeAdapter([_issue("ENG-1"), _issue("ENG-2", id="uuid-eng-2")])
    poller = LinearPoller(adapter, store, config=_cfg())
    r1 = await poller.poll_once()
    assert (r1.created, r1.skipped, r1.seen) == (2, 0, 2)
    assert sorted(r1.numbers) == ["ENG-1", "ENG-2"]
    r2 = await poller.poll_once()
    assert (r2.created, r2.skipped) == (0, 2)   # deduped by (source, external_id)


@pytest.mark.asyncio
async def test_a_transport_error_is_reported_and_creates_nothing(store, caplog):
    events = []
    poller = LinearPoller(_FakeAdapter(raises=LinearError("boom")), store,
                          config=_cfg(), on_event=lambda k, t: events.append((k, t)))
    with caplog.at_level(logging.WARNING, logger="no_human.intake.linear_poll"):
        result = await poller.poll_once()
    assert result.created == 0
    assert events and events[0][0] == "linear_poll_error"
    assert not await store.list_tasks()          # never a half-created task


@pytest.mark.asyncio
async def test_throttling_is_named_as_such_not_reported_as_a_bad_request(store, caplog):
    """A log line saying only "HTTP 400" would read as a permanent client
    error; this one has to say it is retryable throttling."""
    events = []
    poller = LinearPoller(_FakeAdapter(raises=LinearRateLimited("slow down", code="RATELIMITED",
                                                               status=400)),
                          store, config=_cfg(), on_event=lambda k, t: events.append((k, t)))
    with caplog.at_level(logging.WARNING, logger="no_human.intake.linear_poll"):
        await poller.poll_once()
    assert "rate limited" in events[0][1]
    assert "not 429" in events[0][1]


@pytest.mark.asyncio
async def test_a_config_error_gets_its_own_event_kind_not_the_transport_one(store, caplog):
    """"The network flaked" and "your team key names no team" need different
    words: no number of ticks fixes the second. Same split monday's poller
    already makes."""
    events = []
    poller = LinearPoller(
        _FakeAdapter(raises=LinearConfigError(
            "integrations.linear.team_key is 'N0', which is not a team")),
        store, config=_cfg(), on_event=lambda k, t: events.append((k, t)))
    with caplog.at_level(logging.WARNING, logger="no_human.intake.linear_poll"):
        result = await poller.poll_once()
    assert result.created == 0
    assert events[0][0] == "linear_config_error"
    assert "not configured" in events[0][1]
    assert "integrations.linear.team_key" in events[0][1]
    assert not await store.list_tasks()


@pytest.mark.asyncio
async def test_a_config_error_never_crashes_the_poll_pool(store):
    """The poller must survive it exactly as it survives a transport error —
    a raise here would take `nh serve`'s whole tick down."""
    adapter = _FakeAdapter(raises=LinearConfigError("bad team key"))
    poller = LinearPoller(adapter, store, config=_cfg())
    assert (await poller.poll_once()).created == 0
    assert (await poller.tick()).created == 0        # and the next tick still runs
    # ...and it recovers by itself once the operator fixes the setting.
    adapter.raises = None
    adapter.issues = [_issue("ENG-1")]
    assert (await poller.tick()).created == 1


@pytest.mark.asyncio
async def test_a_transport_error_still_reports_as_a_transport_error(store):
    """The control for the two tests above: adding a config kind must not have
    reclassified everything else into it."""
    events = []
    poller = LinearPoller(_FakeAdapter(raises=LinearError("connection reset")), store,
                          config=_cfg(), on_event=lambda k, t: events.append((k, t)))
    assert (await poller.poll_once()).created == 0
    assert events[0][0] == "linear_poll_error"
    assert "not configured" not in events[0][1]


@pytest.mark.asyncio
async def test_write_back_is_off_by_default(store):
    adapter = _FakeAdapter([_issue()])
    poller = LinearPoller(adapter, store, config=_cfg())
    await poller.poll_once()
    # The task must be in a status that WOULD produce a note and a transition,
    # otherwise "nothing was written" proves nothing about the opt-in gate.
    (task,) = await store.list_tasks()
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)
    assert await poller.sync_statuses() == 0
    assert adapter.comments == [] and adapter.transitions == []


@pytest.mark.asyncio
async def test_write_back_comments_and_transitions_once_per_change(store):
    adapter = _FakeAdapter([_issue()])
    cfg = _cfg(write_back=True)
    poller = LinearPoller(adapter, store, config=cfg)
    await poller.poll_once()
    (task,) = await store.list_tasks()

    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)
    assert await poller.sync_statuses() == 1
    assert adapter.transitions == [("uuid-eng-1", "started")]
    assert len(adapter.comments) == 1

    # Same status again: no duplicate comment, no duplicate transition.
    assert await poller.sync_statuses() == 0
    assert adapter.transitions == [("uuid-eng-1", "started")]
    assert len(adapter.comments) == 1


@pytest.mark.asyncio
async def test_write_back_uses_the_uuid_not_the_human_identifier(store):
    adapter = _FakeAdapter([_issue()])
    poller = LinearPoller(adapter, store, config=_cfg(write_back=True))
    await poller.poll_once()
    (task,) = await store.list_tasks()
    await store.set_status(task, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})
    await poller.sync_statuses()
    assert adapter.transitions == [("uuid-eng-1", "completed")]
    assert adapter.comments[0][0] == "uuid-eng-1"
    assert all(issue_id != "ENG-1" for issue_id, _ in adapter.comments)


@pytest.mark.asyncio
async def test_a_task_with_no_node_id_is_skipped_loudly_not_guessed(store, caplog):
    adapter = _FakeAdapter()
    poller = LinearPoller(adapter, store, config=_cfg(write_back=True))
    task = Task.new("orphan", source="linear", external_id="ENG-77")
    task.context = {"linear": {"url": "x"}}       # no "id"
    task.status = TaskStatus.IMPLEMENTING
    await store.create_task(task)
    with caplog.at_level(logging.WARNING, logger="no_human.intake.linear_poll"):
        assert await poller.sync_statuses() == 0
    assert adapter.comments == [] and adapter.transitions == []
    assert "ENG-77" in caplog.text and "cannot write back" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [TaskStatus.BLOCKED, TaskStatus.ESCALATED,
                                    TaskStatus.FAILED, TaskStatus.AWAITING_INPUT])
async def test_off_ramp_statuses_comment_but_never_transition(store, status):
    adapter = _FakeAdapter([_issue()])
    poller = LinearPoller(adapter, store, config=_cfg(write_back=True))
    await poller.poll_once()
    (task,) = await store.list_tasks()
    await store.set_status(task, status, validate=False)
    await poller.sync_statuses()
    assert adapter.transitions == []          # state must never lie
    assert len(adapter.comments) == 1


@pytest.mark.asyncio
async def test_no_needs_a_human_note_when_a_human_already_closed_the_issue(store):
    adapter = _FakeAdapter([_issue()])
    adapter.state_types["uuid-eng-1"] = "completed"
    poller = LinearPoller(adapter, store, config=_cfg(write_back=True))
    await poller.poll_once()
    (task,) = await store.list_tasks()
    await store.set_status(task, TaskStatus.ESCALATED, validate=False)
    await poller.sync_statuses()
    assert adapter.comments == []             # the note would have lied


@pytest.mark.asyncio
@pytest.mark.parametrize("state_type,posts_note", [
    # The three CLOSED types. `canceled` and `duplicate` close an issue exactly
    # as dead as `completed` does, so a "no_human is blocked / escalated" note
    # on one of them lies in the same way. The obvious bug here is an
    # assumed-six-value mapping that drops `duplicate` (WorkflowState.type is a
    # plain String, not an enum, so nothing else catches it).
    ("completed", False),
    ("canceled", False),
    ("duplicate", False),
    # The four OPEN types. The issue is still live, so the note must be posted.
    ("triage", True),
    ("backlog", True),
    ("unstarted", True),
    ("started", True),
])
async def test_the_closed_state_set_is_exactly_completed_canceled_duplicate(
        store, state_type, posts_note):
    adapter = _FakeAdapter([_issue()])
    adapter.state_types["uuid-eng-1"] = state_type
    poller = LinearPoller(adapter, store, config=_cfg(write_back=True))
    await poller.poll_once()
    (task,) = await store.list_tasks()
    await store.set_status(task, TaskStatus.BLOCKED, validate=False)
    await poller.sync_statuses()
    assert (len(adapter.comments) == 1) is posts_note
    assert set(STATE_TYPES) == {"completed", "canceled", "duplicate",
                                "triage", "backlog", "unstarted", "started"}


@pytest.mark.asyncio
async def test_a_failed_state_check_still_posts_the_needs_a_human_note(store, caplog):
    """Fail OPEN, deliberately.

    The closed-state check exists to suppress a note that would lie. If the
    check itself cannot answer, suppressing is the worse error of the two: an
    escalation is dropped permanently (the marker is written either way on the
    suppress path), and nothing ever says a human was needed. So a raising
    state_type must degrade to "post it".
    """
    class _BadStateType(_FakeAdapter):
        def state_type(self, issue_id):
            raise LinearError("state read blew up")

    adapter = _BadStateType([_issue()])
    poller = LinearPoller(adapter, store, config=_cfg(write_back=True))
    await poller.poll_once()
    (task,) = await store.list_tasks()
    await store.set_status(task, TaskStatus.ESCALATED, validate=False)
    with caplog.at_level(logging.WARNING, logger="no_human.intake.linear_poll"):
        await poller.sync_statuses()
    assert len(adapter.comments) == 1
    assert "escalated" in adapter.comments[0][1]
    assert "state check" in caplog.text        # the failure is surfaced, not hidden


@pytest.mark.asyncio
async def test_a_failed_comment_leaves_the_marker_unset_so_the_next_tick_retries(store):
    """A raised comment must not be recorded as a synced status.

    Marking it synced would drop the note forever — the status never changes
    again, so the only chance to say "a human is needed" is gone. The
    transition half is independent and keeps its own marker.
    """
    class _FlakyComment(_FakeAdapter):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.fail_next = True

        def comment(self, issue_id, body):
            if self.fail_next:
                self.fail_next = False
                raise LinearError("comment blew up")
            return super().comment(issue_id, body)

    adapter = _FlakyComment([_issue()])
    poller = LinearPoller(adapter, store, config=_cfg(write_back=True))
    await poller.poll_once()
    (task,) = await store.list_tasks()
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)

    await poller.sync_statuses()
    assert adapter.comments == []              # the note did not land
    (task,) = await store.list_tasks()
    linear = task.context["linear"]
    assert "nh_synced_status" not in linear     # ... so it is NOT marked synced
    assert linear["nh_linear_transitions"] == ["started"]   # the other half stands

    await poller.sync_statuses()               # next tick
    assert len(adapter.comments) == 1          # retried
    assert "started work" in adapter.comments[0][1]
    assert adapter.transitions == [("uuid-eng-1", "started")]   # not re-transitioned
    (task,) = await store.list_tasks()
    assert task.context["linear"]["nh_synced_status"] == TaskStatus.IMPLEMENTING.value


@pytest.mark.asyncio
async def test_a_non_linear_task_in_the_same_store_is_never_written_through_linear(
        store, caplog):
    """One store holds every source. Write-back must select on `source`, not
    just "has an external id" — a Jira task otherwise gets a work note posted
    through the Linear API, onto whatever issue its id happens to hit."""
    adapter = _FakeAdapter([_issue()])
    poller = LinearPoller(adapter, store, config=_cfg(write_back=True))
    await poller.poll_once()

    jira = Task.new("a jira task", source="jira", external_id="PROJ-1")
    jira.context = {"jira": {"id": "10001"}}
    jira.status = TaskStatus.ESCALATED          # would produce a note if considered
    await store.create_task(jira)

    (linear_task,) = [t for t in await store.list_tasks() if t.source == "linear"]
    await store.set_status(linear_task, TaskStatus.IMPLEMENTING, validate=False)

    with caplog.at_level(logging.WARNING, logger="no_human.intake.linear_poll"):
        assert await poller.sync_statuses() == 1        # the linear task only
    assert [issue_id for issue_id, _ in adapter.comments] == ["uuid-eng-1"]
    assert adapter.transitions == [("uuid-eng-1", "started")]
    # Not even looked at: the poller never reaches the "cannot write back"
    # branch for it, which is what a source-blind loop would log.
    assert "PROJ-1" not in caplog.text
    (jira_after,) = [t for t in await store.list_tasks() if t.source == "jira"]
    assert jira_after.context == {"jira": {"id": "10001"}}   # untouched


@pytest.mark.asyncio
async def test_a_failed_transition_does_not_suppress_the_comment(store):
    class _BadTransition(_FakeAdapter):
        def transition(self, issue_id, target):
            raise LinearError("nope")

    adapter = _BadTransition([_issue()])
    poller = LinearPoller(adapter, store, config=_cfg(write_back=True))
    await poller.poll_once()
    (task,) = await store.list_tasks()
    await store.set_status(task, TaskStatus.IMPLEMENTING, validate=False)
    await poller.sync_statuses()
    assert len(adapter.comments) == 1         # independent idempotency markers


@pytest.mark.asyncio
async def test_default_repo_is_applied_to_polled_tasks(store, tmp_path):
    adapter = _FakeAdapter([_issue()])
    cfg = _cfg(default_repo=str(tmp_path))
    await LinearPoller(adapter, store, config=cfg).poll_once()
    (task,) = await store.list_tasks()
    assert task.repo_path == str(tmp_path)


@pytest.mark.asyncio
async def test_tick_runs_write_back_even_though_it_is_a_separate_half(store):
    adapter = _FakeAdapter([_issue()])
    poller = LinearPoller(adapter, store, config=_cfg(write_back=True))
    await poller.tick()
    (task,) = await store.list_tasks()
    await store.set_status(task, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})
    await poller.tick()
    assert adapter.transitions == [("uuid-eng-1", "completed")]
