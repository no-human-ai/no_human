"""Jira intake: JQL search → normalize → dedupe, plus opt-in write-back.

Mirrors the (removed) TRACKER poller's shape. All HTTP is mocked; the API token is
read from the env and must never appear in a log line.
"""

import logging

import pytest

from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.intake.jira import JiraAdapter, _adf_text
from no_human.intake.jira_poll import JiraPoller
from no_human.profile import ProjectProfile


def _cfg(**over):
    j = {"enabled": True, "site": "https://acme.atlassian.net",
         "project_key": "PROJ", "email": "me@x.com", "jql": ""}
    j.update(over)
    return {"integrations": {"jira": j}}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _transitions_payload():
    """The three status categories a real workflow exposes: To Do (new),
    In Progress (indeterminate), Done (done) — ids are arbitrary/non-obvious
    on purpose so a test that hardcoded an id would fail."""
    return {"transitions": [
        {"id": "11", "name": "Back to To Do", "to": {"statusCategory": {"key": "new"}}},
        {"id": "21", "name": "Start Progress", "to": {"statusCategory": {"key": "indeterminate"}}},
        {"id": "31", "name": "Done", "to": {"statusCategory": {"key": "done"}}},
    ]}


def test_adapter_configured(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    assert JiraAdapter(_cfg()).configured is True
    assert JiraAdapter(_cfg(site="")).configured is False
    monkeypatch.delenv("JIRA_API_TOKEN")
    assert JiraAdapter(_cfg()).configured is False   # no token → not configured


def test_search_uses_successor_endpoint_and_basic_auth(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "SEKRET")
    captured = {}

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        captured.update(url=url, params=params, auth=auth)
        return _Resp({"issues": [{"key": "PROJ-1", "fields": {"summary": "Do X"}}]})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    issues = JiraAdapter(_cfg()).search()
    assert issues[0]["key"] == "PROJ-1"
    # r1: the deprecated /rest/api/3/search is replaced by /rest/api/3/search/jql
    assert "/rest/api/3/search/jql" in captured["url"]
    assert captured["auth"] == ("me@x.com", "SEKRET")     # Basic email:token


def test_jql_is_operator_authored_never_task_text(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    a = JiraAdapter(_cfg(jql="assignee = currentUser() AND statusCategory != Done"))
    assert a._search_jql() == "assignee = currentUser() AND statusCategory != Done"
    # default derives from the project key only — never from any task text
    assert 'project = "PROJ"' in JiraAdapter(_cfg(jql=""))._search_jql()


def test_normalize_maps_fields_and_criteria(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    issue = {"key": "PROJ-7", "fields": {
        "summary": "Add retry",
        "description": "Body\n- [ ] retries 3x\n- [x] logs\n",
        "status": {"name": "To Do"}, "labels": ["backend"],
        "issuetype": {"name": "Story"}}}
    task = JiraAdapter(_cfg()).normalize(issue)
    assert task.source == "jira"
    assert task.external_id == "PROJ-7"
    assert task.title == "Add retry"
    assert task.acceptance_criteria == ["retries 3x", "logs"]
    assert task.context["jira"]["url"].endswith("/browse/PROJ-7")
    assert task.context["jira"]["status"] == "To Do"
    assert task.context["jira"]["labels"] == ["backend"]


def test_normalize_extracts_plain_bullets_from_acceptance_criteria(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    task = JiraAdapter(_cfg()).normalize({"key": "PROJ-10", "fields": {
        "summary": "Plain criteria",
        "description": {"type": "doc", "content": [
            {"type": "heading", "attrs": {"level": 2},
             "content": [{"type": "text", "text": "Acceptance criteria"}]},
            {"type": "bulletList", "content": [
                {"type": "listItem", "content": [
                    {"type": "paragraph", "content": [
                        {"type": "text", "text": "retries 3x"}]}]},
                {"type": "listItem", "content": [
                    {"type": "paragraph", "content": [
                        {"type": "text", "text": "logs failures"}]}]},
            ]},
        ]}}})
    assert task.acceptance_criteria == ["retries 3x", "logs failures"]


def test_normalize_warns_for_an_empty_acceptance_criteria_heading(monkeypatch, caplog):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    with caplog.at_level(logging.WARNING, logger="no_human.intake.criteria"):
        task = JiraAdapter(_cfg()).normalize({"key": "PROJ-11", "fields": {
            "summary": "Missing criteria", "description": "## Acceptance criteria\nText only"}})
    assert task.acceptance_criteria == []
    assert "Jira issue PROJ-11" in caplog.text
    assert "--criteria" in caplog.text


def test_normalize_leaves_missing_criteria_silent(monkeypatch, caplog):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    with caplog.at_level(logging.WARNING, logger="no_human.intake.criteria"):
        task = JiraAdapter(_cfg()).normalize({"key": "PROJ-12", "fields": {
            "summary": "No criteria", "description": "Background only"}})
    assert task.acceptance_criteria == []
    assert not caplog.records


def test_normalize_handles_adf_description(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    adf = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "hello world"}]}]}
    task = JiraAdapter(_cfg()).normalize({"key": "PROJ-8", "fields": {"summary": "S", "description": adf}})
    assert "hello world" in task.description
    assert _adf_text("plain string") == "plain string"


def test_issue_brief_truncates_but_issue_detail_carries_full_description(monkeypatch):
    """SCRUM-9: issue_brief (browse list) truncates to 2000 chars — that's
    correct for a small list payload. issue_detail (single picked issue) must
    carry the FULL text so the created task doesn't lose the tail."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    long_desc = "x" * 2000 + "TAIL-AFTER-TRUNCATION-POINT"
    issue = {"key": "PROJ-9", "fields": {"summary": "Fix", "description": long_desc}}
    a = JiraAdapter(_cfg())
    brief = a.issue_brief(issue)
    assert len(brief["description"]) == 2000
    assert "TAIL-AFTER-TRUNCATION-POINT" not in brief["description"]

    detail = a.issue_detail(issue)
    assert detail["description"] == long_desc
    assert detail["description"].endswith("TAIL-AFTER-TRUNCATION-POINT")
    # Same shape otherwise.
    assert detail["key"] == brief["key"] == "PROJ-9"
    assert detail["summary"] == brief["summary"]


def test_search_text_nonempty_query_stays_scoped_to_open_tickets(monkeypatch):
    """SCRUM-5 bug 1: typing into the picker must FILTER the open-tickets set,
    never expand it — a non-empty query has to keep the same
    ``statusCategory != Done`` scope the empty-query browse uses."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    captured = {}

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        captured.update(params=params)
        return _Resp({"issues": []})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    JiraAdapter(_cfg()).search_text("thing")
    jql = captured["params"]["jql"]
    assert 'project = "PROJ"' in jql, jql
    assert "statusCategory != Done" in jql, jql
    assert 'text ~ "thing"' in jql, jql
    assert "ORDER BY updated DESC" in jql, jql


def test_search_text_empty_query_browses_open_tickets(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    captured = {}

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        captured.update(params=params)
        return _Resp({"issues": []})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    JiraAdapter(_cfg()).search_text("")
    jql = captured["params"]["jql"]
    assert "statusCategory != Done" in jql, jql
    assert "text ~" not in jql, jql


def test_search_text_escapes_quotes_and_backslashes(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    captured = {}

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        captured.update(params=params)
        return _Resp({"issues": []})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    JiraAdapter(_cfg()).search_text('a"b\\c')
    jql = captured["params"]["jql"]
    assert 'a\\"b\\\\c' in jql, jql
    assert "statusCategory != Done" in jql, jql


def test_get_issue_fetches_by_key_with_basic_auth(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "SEKRET")
    captured = {}

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        captured.update(url=url, params=params, auth=auth)
        return _Resp({"key": "PROJ-9", "fields": {"summary": "Fix the thing"}})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    issue = JiraAdapter(_cfg()).get_issue("PROJ-9")
    assert issue["key"] == "PROJ-9"
    assert "/rest/api/3/issue/PROJ-9" in captured["url"]
    assert captured["auth"] == ("me@x.com", "SEKRET")


def test_status_category_fetches_status_field_only(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "SEKRET")
    captured = {}

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        captured.update(url=url, params=params)
        return _Resp({"fields": {"status": {"statusCategory": {"key": "Done"}}}})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    cat = JiraAdapter(_cfg()).status_category("PROJ-9")

    assert cat == "done"
    assert "/rest/api/3/issue/PROJ-9" in captured["url"]
    assert captured["params"] == {"fields": "status"}


def test_auth_token_never_logged(monkeypatch, caplog):
    monkeypatch.setenv("JIRA_API_TOKEN", "SUPERSECRET")
    monkeypatch.setattr("no_human.intake.jira.httpx.get",
                        lambda *a, **k: _Resp({"issues": []}))
    with caplog.at_level(logging.DEBUG):
        JiraAdapter(_cfg()).search()
    assert "SUPERSECRET" not in caplog.text


def test_comment_write_back_is_opt_in(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    posted = {}

    def fake_post(url, auth=None, json=None, timeout=None, headers=None):
        posted.update(url=url)
        return _Resp({})

    monkeypatch.setattr("no_human.intake.jira.httpx.post", fake_post)
    assert JiraAdapter(_cfg(write_back=False)).comment("PROJ-1", "hi") is False
    assert posted == {}                                    # never posts when off
    assert JiraAdapter(_cfg(write_back=True)).comment("PROJ-1", "hi") is True
    assert "/issue/PROJ-1/comment" in posted["url"]


@pytest.mark.asyncio
async def test_poller_creates_then_dedupes(monkeypatch, tmp_path):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        issues = [{"key": "PROJ-1", "fields": {"summary": "A"}},
                  {"key": "PROJ-2", "fields": {"summary": "B"}}]
        a = JiraAdapter(_cfg())
        monkeypatch.setattr(a, "search", lambda: issues)
        poller = JiraPoller(a, store, config=_cfg(default_repo="/tmp/repo"))
        r1 = await poller.poll_once()
        assert (r1.created, r1.seen) == (2, 2)
        r2 = await poller.poll_once()          # same issues → all deduped
        assert (r2.created, r2.skipped) == (0, 2)
        tasks = await store.list_tasks()
        assert {t.external_id for t in tasks} == {"PROJ-1", "PROJ-2"}
        assert all(t.repo_path == "/tmp/repo" for t in tasks)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_poller_skips_issue_already_on_board_as_jira_task(monkeypatch, tmp_path):
    """SCRUM-32 regression: an issue whose key already exists as a board task
    (source="jira" + external_id, e.g. created via the web import picker)
    must not be re-created by JiraPoller.tick."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        seeded = Task.new("A", source="jira", external_id="PROJ-1")
        await store.create_task(seeded)

        a = JiraAdapter(_cfg())
        monkeypatch.setattr(a, "search", lambda: [{"key": "PROJ-1", "fields": {"summary": "A"}}])
        monkeypatch.setattr(a, "transition", lambda *a, **k: False)
        monkeypatch.setattr(a, "comment", lambda *a, **k: False)

        poller = JiraPoller(a, store, config=_cfg())
        result = await poller.tick()

        assert result.created == 0
        assert result.skipped == 1
        matching = [t for t in await store.list_tasks() if t.external_id == "PROJ-1"]
        assert len(matching) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_poller_survives_a_search_error(monkeypatch, tmp_path):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        a = JiraAdapter(_cfg())

        def boom():
            raise RuntimeError("429 rate limited")

        monkeypatch.setattr(a, "search", boom)
        r = await JiraPoller(a, store, config=_cfg()).poll_once()
        assert r.created == 0                   # error logged, not raised
    finally:
        await store.close()


# --------------------- SCRUM-26: repo profile default budgets ---------------


@pytest.mark.asyncio
async def test_poller_copies_profile_defaults_into_task_config(monkeypatch, tmp_path):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        await store.upsert_profile(ProjectProfile(
            repo_path="/tmp/repo", default_attempt_tokens=6_000_000,
            default_lifetime_tokens=16_000_000,
        ))
        issues = [{"key": "PROJ-1", "fields": {"summary": "A"}}]
        a = JiraAdapter(_cfg())
        monkeypatch.setattr(a, "search", lambda: issues)
        poller = JiraPoller(a, store, config=_cfg(default_repo="/tmp/repo"))
        r = await poller.poll_once()
        assert r.created == 1
        tasks = await store.list_tasks()
        assert tasks[0].config["attempt_tokens"] == 6_000_000
        assert tasks[0].config["lifetime_tokens"] == 16_000_000
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_poller_no_profile_defaults_config_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        issues = [{"key": "PROJ-1", "fields": {"summary": "A"}}]
        a = JiraAdapter(_cfg())
        monkeypatch.setattr(a, "search", lambda: issues)
        poller = JiraPoller(a, store, config=_cfg(default_repo="/tmp/repo"))
        r = await poller.poll_once()
        assert r.created == 1
        tasks = await store.list_tasks()
        assert "attempt_tokens" not in tasks[0].config
        assert "lifetime_tokens" not in tasks[0].config
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_poller_explicit_config_overrides_profile_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        await store.upsert_profile(ProjectProfile(
            repo_path="/tmp/repo", default_attempt_tokens=6_000_000,
            default_lifetime_tokens=16_000_000,
        ))
        issues = [{"key": "PROJ-1", "fields": {"summary": "A"}}]
        a = JiraAdapter(_cfg())
        monkeypatch.setattr(a, "search", lambda: issues)
        orig_normalize = a.normalize

        def _normalize_with_explicit_override(issue):
            t = orig_normalize(issue)
            t.config["attempt_tokens"] = 999
            return t

        monkeypatch.setattr(a, "normalize", _normalize_with_explicit_override)
        poller = JiraPoller(a, store, config=_cfg(default_repo="/tmp/repo"))
        r = await poller.poll_once()
        assert r.created == 1
        tasks = await store.list_tasks()
        assert tasks[0].config["attempt_tokens"] == 999          # explicit wins
        assert tasks[0].config["lifetime_tokens"] == 16_000_000  # untouched key still defaulted
    finally:
        await store.close()


# --------------------------- SCRUM-21: transitions ---------------------------


def test_transition_matches_target_category_never_hardcoded_id(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    monkeypatch.setattr("no_human.intake.jira.httpx.get",
                        lambda *a, **k: _Resp(_transitions_payload()))
    posted = {}

    def fake_post(url, auth=None, json=None, timeout=None, headers=None):
        posted["url"] = url
        posted["body"] = json
        return _Resp({})

    monkeypatch.setattr("no_human.intake.jira.httpx.post", fake_post)
    a = JiraAdapter(_cfg(write_back=True))

    assert a.transition("PROJ-1", "indeterminate") is True
    assert posted["body"] == {"transition": {"id": "21"}}
    assert "/issue/PROJ-1/transitions" in posted["url"]

    posted.clear()
    assert a.transition("PROJ-1", "done") is True
    assert posted["body"] == {"transition": {"id": "31"}}


def test_transition_first_when_multiple_match(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    payload = {"transitions": [
        {"id": "21", "to": {"statusCategory": {"key": "indeterminate"}}},
        {"id": "22", "to": {"statusCategory": {"key": "indeterminate"}}},
    ]}
    monkeypatch.setattr("no_human.intake.jira.httpx.get", lambda *a, **k: _Resp(payload))
    posted = {}

    def fake_post(url, auth=None, json=None, timeout=None, headers=None):
        posted["body"] = json
        return _Resp({})

    monkeypatch.setattr("no_human.intake.jira.httpx.post", fake_post)
    a = JiraAdapter(_cfg(write_back=True))
    assert a.transition("PROJ-1", "indeterminate") is True
    assert posted["body"] == {"transition": {"id": "21"}}


def test_transition_opt_in_and_no_match_is_noop(monkeypatch, caplog):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    post_calls = []
    monkeypatch.setattr("no_human.intake.jira.httpx.post",
                        lambda *a, **k: post_calls.append(1) or _Resp({}))

    # write_back=False -> zero HTTP at all (transitions() never even called)
    get_calls = []
    monkeypatch.setattr("no_human.intake.jira.httpx.get",
                        lambda *a, **k: get_calls.append(1) or _Resp(_transitions_payload()))
    assert JiraAdapter(_cfg(write_back=False)).transition("PROJ-1", "indeterminate") is False
    assert get_calls == []
    assert post_calls == []

    # write_back=True but no transition targets the requested category
    no_match = {"transitions": [{"id": "11", "to": {"statusCategory": {"key": "new"}}}]}
    monkeypatch.setattr("no_human.intake.jira.httpx.get", lambda *a, **k: _Resp(no_match))
    with caplog.at_level(logging.DEBUG):
        assert JiraAdapter(_cfg(write_back=True)).transition("PROJ-1", "done") is False
    assert post_calls == []
    assert "no transition" in caplog.text.lower()


async def _fake_pr_url(_task):
    return "https://gh/pr/9"


def _seeded_jira_task(status: TaskStatus, **context_jira) -> Task:
    task = Task.new("T", source="jira", external_id="PROJ-1")
    task.status = status
    if context_jira:
        task.context = {"jira": context_jira}
    return task


@pytest.mark.asyncio
async def test_poller_claim_transitions_in_progress_once(monkeypatch, tmp_path):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        task = _seeded_jira_task(TaskStatus.IMPLEMENTING)
        await store.create_task(task)

        a = JiraAdapter(_cfg(write_back=True))
        transition_calls = []
        events = []
        monkeypatch.setattr(a, "transition",
                            lambda key, cat: transition_calls.append((key, cat)) or True)
        monkeypatch.setattr(a, "comment", lambda *a_, **k_: True)
        poller = JiraPoller(a, store, config=_cfg(write_back=True),
                            on_event=lambda kind, text: events.append((kind, text)))

        await poller.sync_statuses()
        await poller.sync_statuses()          # second tick: no re-trigger

        assert transition_calls == [("PROJ-1", "indeterminate")]
        saved = await store.get_task(task.id)
        assert saved.context["jira"]["nh_jira_transitions"] == ["indeterminate"]

        transitioned = [text for kind, text in events if kind == "jira_transitioned"]
        assert len(transitioned) == 1
        assert "In Progress" in transitioned[0]
        assert "indeterminate" not in transitioned[0]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_poller_done_transitions_and_comments_with_pr(monkeypatch, tmp_path):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        task = _seeded_jira_task(TaskStatus.DONE, nh_jira_transitions=["indeterminate"])
        await store.create_task(task)

        a = JiraAdapter(_cfg(write_back=True))
        transition_calls = []
        comments = []
        monkeypatch.setattr(a, "transition",
                            lambda key, cat: transition_calls.append((key, cat)) or True)
        monkeypatch.setattr(a, "comment",
                            lambda key, body: comments.append(body) or True)
        poller = JiraPoller(a, store, config=_cfg(write_back=True))
        monkeypatch.setattr(poller, "_pr_url_for", _fake_pr_url)

        await poller.sync_statuses()

        assert transition_calls == [("PROJ-1", "done")]
        assert len(comments) == 1
        assert "PR: https://gh/pr/9" in comments[0]
        saved = await store.get_task(task.id)
        assert saved.context["jira"]["nh_jira_transitions"] == ["indeterminate", "done"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_poller_escalated_comments_without_transition(monkeypatch, tmp_path):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        get_calls = []
        post_calls = []
        monkeypatch.setattr("no_human.intake.jira.httpx.get",
                            lambda *a, **k: get_calls.append(1) or _Resp({"transitions": []}))
        monkeypatch.setattr("no_human.intake.jira.httpx.post",
                            lambda *a, **k: post_calls.append(1) or _Resp({}))

        a = JiraAdapter(_cfg(write_back=True))
        transition_calls = []
        comments = []
        monkeypatch.setattr(a, "transition", lambda *args: transition_calls.append(args))
        monkeypatch.setattr(a, "comment",
                            lambda key, body: comments.append((key, body)) or True)

        for status in (TaskStatus.ESCALATED, TaskStatus.FAILED):
            t = Task.new(f"T-{status.value}", source="jira", external_id=f"PROJ-{status.value}")
            t.status = status
            await store.create_task(t)

        poller = JiraPoller(a, store, config=_cfg(write_back=True))
        await poller.sync_statuses()

        assert transition_calls == []
        assert len(comments) == 2
        # One status_category GET per off-ramp task (SCRUM-53 done-check);
        # {"transitions": []} has no "fields", so status_category reads ""
        # (not done) and the comment still posts.
        assert get_calls == [1, 1]
        assert post_calls == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_write_back_false_and_non_jira_never_write(monkeypatch, tmp_path):
    monkeypatch.setenv("JIRA_API_TOKEN", "t")

    # write_back=False: zero writes even for a DONE jira task.
    store1 = await Store(tmp_path / "a.db").connect()
    try:
        task = _seeded_jira_task(TaskStatus.DONE)
        await store1.create_task(task)
        a = JiraAdapter(_cfg(write_back=False))
        calls = []
        monkeypatch.setattr(a, "transition", lambda *args: calls.append(("transition", args)))
        monkeypatch.setattr(a, "comment", lambda *args: calls.append(("comment", args)))
        written = await JiraPoller(a, store1, config=_cfg(write_back=False)).sync_statuses()
        assert written == 0
        assert calls == []
    finally:
        await store1.close()

    # write_back=True but a board-typed (non-jira) task is never touched.
    store2 = await Store(tmp_path / "b.db").connect()
    try:
        board_task = Task.new("B", source="board")
        board_task.status = TaskStatus.DONE
        await store2.create_task(board_task)
        a2 = JiraAdapter(_cfg(write_back=True))
        calls2 = []
        monkeypatch.setattr(a2, "transition", lambda *args: calls2.append(("transition", args)))
        monkeypatch.setattr(a2, "comment", lambda *args: calls2.append(("comment", args)))
        written2 = await JiraPoller(a2, store2, config=_cfg(write_back=True)).sync_statuses()
        assert written2 == 0
        assert calls2 == []
    finally:
        await store2.close()


@pytest.mark.asyncio
async def test_sync_statuses_only_newest_task_per_external_id(monkeypatch, tmp_path):
    """SCRUM-31: a stale/cancelled duplicate task sharing external_id must
    never be synced — only the newest (by created_at) task per external_id
    is eligible for transition/comment."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        older = _seeded_jira_task(TaskStatus.FAILED)
        older.created_at = "2026-07-01T00:00:00"
        await store.create_task(older)

        newer = _seeded_jira_task(TaskStatus.IMPLEMENTING)
        newer.created_at = "2026-07-24T00:00:00"
        await store.create_task(newer)

        a = JiraAdapter(_cfg(write_back=True))
        transition_calls = []
        comments = []
        monkeypatch.setattr(a, "transition",
                            lambda key, cat: transition_calls.append((key, cat)) or True)
        monkeypatch.setattr(a, "comment",
                            lambda key, body: comments.append((key, body)) or True)
        poller = JiraPoller(a, store, config=_cfg(write_back=True))

        written = await poller.sync_statuses()

        assert written == 1
        assert transition_calls == [("PROJ-1", "indeterminate")]
        assert len(comments) == 1
        assert "failed" not in comments[0][1].lower()   # the older FAILED note never posted

        saved_older = await store.get_task(older.id)
        assert "jira" not in saved_older.context or "nh_synced_status" not in saved_older.context.get("jira", {})
        assert "jira" not in saved_older.context or "nh_jira_transitions" not in saved_older.context.get("jira", {})

        saved_newer = await store.get_task(newer.id)
        assert saved_newer.context["jira"]["nh_synced_status"] == "implementing"
        assert saved_newer.context["jira"]["nh_jira_transitions"] == ["indeterminate"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sync_statuses_identical_created_at_ties_break_on_id(monkeypatch, tmp_path):
    """When created_at is identical, the higher task id (deterministic
    tie-breaker) wins — not insertion/iteration order."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        a_task = _seeded_jira_task(TaskStatus.FAILED)
        a_task.created_at = "2026-07-24T00:00:00"
        b_task = _seeded_jira_task(TaskStatus.IMPLEMENTING)
        b_task.created_at = "2026-07-24T00:00:00"
        # Force a deterministic id ordering regardless of uuid4 randomness:
        # the LOWER id gets FAILED (loses), the HIGHER id gets IMPLEMENTING (wins).
        lo, hi = sorted([a_task.id, b_task.id])
        a_task.id, a_task.status = lo, TaskStatus.FAILED
        b_task.id, b_task.status = hi, TaskStatus.IMPLEMENTING

        await store.create_task(a_task)
        await store.create_task(b_task)

        a = JiraAdapter(_cfg(write_back=True))
        transition_calls = []
        comments = []
        monkeypatch.setattr(a, "transition",
                            lambda key, cat: transition_calls.append((key, cat)) or True)
        monkeypatch.setattr(a, "comment",
                            lambda key, body: comments.append((key, body)) or True)
        poller = JiraPoller(a, store, config=_cfg(write_back=True))

        written = await poller.sync_statuses()

        assert written == 1
        assert transition_calls == [("PROJ-1", "indeterminate")]  # the higher-id (IMPLEMENTING) task synced
        saved_b = await store.get_task(b_task.id)
        assert saved_b.context["jira"]["nh_jira_transitions"] == ["indeterminate"]
        saved_a = await store.get_task(a_task.id)
        assert "jira" not in saved_a.context or "nh_jira_transitions" not in saved_a.context.get("jira", {})
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_transition_error_never_breaks_pipeline_or_leaks(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("JIRA_API_TOKEN", "SEKRET")
    store = await Store(tmp_path / "t.db").connect()
    try:
        task = _seeded_jira_task(TaskStatus.IMPLEMENTING)
        await store.create_task(task)

        a = JiraAdapter(_cfg(write_back=True))

        def boom(key, cat):
            raise RuntimeError(
                "https://acme.atlassian.net/rest/api/3/issue/PROJ-1/transitions "
                "Authorization: Basic dG9rZW46U0VLUkVU")

        monkeypatch.setattr(a, "transition", boom)
        monkeypatch.setattr(a, "comment", lambda *a_, **k_: True)
        poller = JiraPoller(a, store, config=_cfg(write_back=True))

        with caplog.at_level(logging.WARNING):
            await poller.sync_statuses()          # must not raise

        saved = await store.get_task(task.id)
        assert "nh_jira_transitions" not in saved.context.get("jira", {})   # unset -> retry next tick
        assert "SEKRET" not in caplog.text
        assert "acme.atlassian.net" not in caplog.text
        assert "Authorization" not in caplog.text
        assert "RuntimeError" in caplog.text
    finally:
        await store.close()


def test_issue_urls_quote_the_key(monkeypatch):
    """Review 2026-07-25 residue: an odd key ('%', '#', '?', space) must be
    percent-quoted into the URL path — a malformed URL surfaces as an opaque
    502 instead of Jira's clean 404."""
    monkeypatch.setenv("JIRA_API_TOKEN", "SEKRET")
    seen = []

    def fake_get(url, params=None, auth=None, timeout=None, headers=None):
        seen.append(url)
        return _Resp({"key": "X", "fields": {}, "transitions": []})

    monkeypatch.setattr("no_human.intake.jira.httpx.get", fake_get)
    adapter = JiraAdapter(_cfg())
    adapter.get_issue("AB#1 %?")
    adapter.transitions("AB#1 %?")

    assert len(seen) == 2
    for url in seen:
        assert "AB%231%20%25%3F" in url, url
        assert "#" not in url and " " not in url, url


@pytest.mark.asyncio
async def test_poller_skips_needs_human_note_when_issue_done_in_jira(monkeypatch, tmp_path):
    """SCRUM-53 AC1: a done-in-Jira issue skips the 'needs a human' note and
    sets the marker so the next tick never re-checks it."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        task = _seeded_jira_task(TaskStatus.FAILED)
        await store.create_task(task)

        a = JiraAdapter(_cfg(write_back=True))
        status_calls = []
        comments = []
        monkeypatch.setattr(a, "status_category",
                            lambda key: status_calls.append(key) or "done")
        monkeypatch.setattr(a, "comment",
                            lambda key, body: comments.append((key, body)) or True)
        monkeypatch.setattr(a, "transition", lambda *args: True)
        poller = JiraPoller(a, store, config=_cfg(write_back=True))

        await poller.sync_statuses()
        await poller.sync_statuses()          # second tick: marker short-circuits

        assert comments == []
        assert status_calls == ["PROJ-1"]     # called exactly once
        saved = await store.get_task(task.id)
        assert saved.context["jira"]["nh_synced_status"] == "failed"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_poller_comments_when_issue_not_done(monkeypatch, tmp_path):
    """SCRUM-53 AC4: a non-done issue still gets the 'needs a human' note."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        task = _seeded_jira_task(TaskStatus.ESCALATED)
        await store.create_task(task)

        a = JiraAdapter(_cfg(write_back=True))
        comments = []
        monkeypatch.setattr(a, "status_category", lambda key: "indeterminate")
        monkeypatch.setattr(a, "comment",
                            lambda key, body: comments.append((key, body)) or True)
        monkeypatch.setattr(a, "transition", lambda *args: True)
        poller = JiraPoller(a, store, config=_cfg(write_back=True))

        await poller.sync_statuses()

        assert len(comments) == 1
        saved = await store.get_task(task.id)
        assert saved.context["jira"]["nh_synced_status"] == "escalated"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_poller_comments_when_status_fetch_errors(monkeypatch, tmp_path):
    """SCRUM-53 AC3: a Jira status fetch failure degrades to today's
    behavior (comment posted), never a crash of the sync loop."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        task = _seeded_jira_task(TaskStatus.FAILED)
        await store.create_task(task)

        a = JiraAdapter(_cfg(write_back=True))
        comments = []

        def boom(key):
            raise RuntimeError("boom")

        monkeypatch.setattr(a, "status_category", boom)
        monkeypatch.setattr(a, "comment",
                            lambda key, body: comments.append((key, body)) or True)
        monkeypatch.setattr(a, "transition", lambda *args: True)
        poller = JiraPoller(a, store, config=_cfg(write_back=True))

        await poller.sync_statuses()          # must not raise

        assert len(comments) == 1
        saved = await store.get_task(task.id)
        assert saved.context["jira"]["nh_synced_status"] == "failed"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_poller_done_note_does_not_status_check(monkeypatch, tmp_path):
    """SCRUM-53 AC2: completion notes are unchanged — the done-check guard
    only applies to off-ramp (blocked/awaiting_input/escalated/failed)
    statuses, never the completion path."""
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    store = await Store(tmp_path / "t.db").connect()
    try:
        task = _seeded_jira_task(TaskStatus.DONE, nh_jira_transitions=["indeterminate"])
        await store.create_task(task)

        a = JiraAdapter(_cfg(write_back=True))
        comments = []

        def must_not_be_called(key):
            raise AssertionError("status_category must not be called for DONE")

        monkeypatch.setattr(a, "status_category", must_not_be_called)
        monkeypatch.setattr(a, "comment",
                            lambda key, body: comments.append((key, body)) or True)
        monkeypatch.setattr(a, "transition", lambda *args: True)
        poller = JiraPoller(a, store, config=_cfg(write_back=True))
        monkeypatch.setattr(poller, "_pr_url_for", _fake_pr_url)

        await poller.sync_statuses()

        assert len(comments) == 1
        assert "PR: https://gh/pr/9" in comments[0][1]
        saved = await store.get_task(task.id)
        assert saved.context["jira"]["nh_synced_status"] == "done"
    finally:
        await store.close()


def test_normalize_reads_an_acceptance_heading_that_follows_a_paragraph(monkeypatch):
    """The ADF flattener emits `## ` and `- ` as PREFIXES, so the line-based
    parser only works if each block already starts a line. A heading as the
    document's first node cannot show that; one that follows a paragraph can.
    """
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    task = JiraAdapter(_cfg()).normalize({"key": "PROJ-13", "fields": {
        "summary": "Heading after prose",
        "description": {"type": "doc", "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": "Some background first."}]},
            {"type": "heading", "attrs": {"level": 2},
             "content": [{"type": "text", "text": "Acceptance criteria"}]},
            {"type": "bulletList", "content": [
                {"type": "listItem", "content": [
                    {"type": "paragraph", "content": [
                        {"type": "text", "text": "retries 3x"}]}]},
            ]},
        ]}}})
    assert task.acceptance_criteria == ["retries 3x"]
