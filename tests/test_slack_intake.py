"""Slack @mention intake handler (SCRUM-61: 2/3 of SCRUM-27's split).

Mirrors test_jira_intake.py's style. The async ``_handle`` core is driven
directly (real tmp-sqlite Store, capturing ``reply`` fake) — the sync
``_dispatch``/``run_coroutine_threadsafe`` bridge carries no business logic
and is covered separately by the ``register()`` wiring test.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future

import pytest

from no_human.integrations.slack.intake import (
    AppMentionHandler,
    extract_repo_token,
    parse_message,
)
from no_human.integrations.slack.worker import SlackWorker
from no_human.project_model import Project

# Tests here reach ``config.load_env_var``, which reads the operator's real
# ``~/.no_human/.env`` BEFORE the process env. Requested by NAME through
# `usefixtures` — never an autouse marker; see tests/conftest.py.
pytestmark = pytest.mark.usefixtures("isolated_env_file")


class _FakeSocketModeClient:
    def __init__(self, app_token=None, web_client=None):
        self.socket_mode_request_listeners = []

    def connect(self):
        pass

    def close(self):
        pass


def _worker(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "BOT-TOK")
    monkeypatch.setenv("SLACK_APP_TOKEN", "APP-TOK")
    monkeypatch.setattr(
        "no_human.integrations.slack.worker.SocketModeClient", _FakeSocketModeClient)
    monkeypatch.setattr(
        "no_human.integrations.slack.worker.WebClient", lambda token=None: object())
    return SlackWorker()


class _Reply:
    """Capturing in-thread reply fake."""

    def __init__(self):
        self.calls = []

    def __call__(self, channel, thread_ts, text):
        self.calls.append((channel, thread_ts, text))


def _event(text, *, channel="C1", ts="1000.1", thread_ts=None):
    e = {"type": "app_mention", "channel": channel, "ts": ts, "text": text}
    if thread_ts:
        e["thread_ts"] = thread_ts
    return e


# ------------------------------ registration ---------------------------------


def test_handler_registers_via_on_event(monkeypatch, tmp_path):
    w = _worker(monkeypatch)
    calls = []
    monkeypatch.setattr(w, "on_event", lambda kind, fn: calls.append((kind, fn)))
    handler = AppMentionHandler(store=object(), reply=_Reply())
    handler.register(w)
    assert calls == [("app_mention", handler._dispatch)]


async def test_registered_handler_is_reachable_through_real_worker(monkeypatch, store):
    """End-to-end through SlackWorker.on_event/dispatch (not just a stub
    capture) — proves the registration API contract, not just the call."""
    w = _worker(monkeypatch)
    handler = AppMentionHandler(store=store, reply=_Reply())
    handler.register(w)
    assert w._handlers["app_mention"] == [handler._dispatch]


# ------------------------------ regex: repo token -----------------------------


@pytest.mark.parametrize("text,expected", [
    ("please fix repo:foo now", "foo"),
    ("please fix repo: foo now", "foo"),
    ("please fix this bug", None),                 # missing
    ("please fix repo:", None),                    # malformed: nothing after colon
    ("please fix repo:   ", None),                  # malformed: only whitespace after colon
    ("first repo:foo then repo:bar", "foo"),        # first match wins
])
def test_extract_repo_token(text, expected):
    assert extract_repo_token(text) == expected


# ------------------------------ message parsing -------------------------------


def test_parse_message_splits_title_and_description():
    text = "<@U123ABC> Fix the login bug\nSteps:\n1. click login\n2. crash"
    title, desc = parse_message(text)
    assert title == "Fix the login bug"
    assert desc == "Steps:\n1. click login\n2. crash"


def test_parse_message_title_only_no_description():
    title, desc = parse_message("<@U123ABC> Title only please")
    assert title == "Title only please"
    assert desc == ""


def test_parse_message_without_mention_prefix():
    title, desc = parse_message("No mention here\nDetails follow")
    assert title == "No mention here"
    assert desc == "Details follow"


def test_parse_message_empty_text_falls_back_to_default_title():
    title, desc = parse_message("")
    assert title == "Slack task"
    assert desc == ""


# ------------------------------ validation: missing repo ----------------------


async def test_missing_repo_replies_in_thread_and_creates_no_task(monkeypatch, store):
    await store.create_project(Project.new("foo", repo_paths=["/tmp/foo"]))
    reply = _Reply()
    handler = AppMentionHandler(store=store, reply=reply)

    await handler._handle(_event("<@U1> Fix the bug\nno repo mentioned here"))

    assert await store.list_tasks() == []
    assert len(reply.calls) == 1
    channel, thread_ts, text = reply.calls[0]
    assert channel == "C1"
    assert thread_ts == "1000.1"
    assert text.startswith(
        "Please specify a repository using format: repo:<name>. Available repos:")
    assert "foo" in text


# ------------------------------ validation: unknown repo ----------------------


async def test_unknown_repo_replies_in_thread_and_creates_no_task(monkeypatch, store):
    await store.create_project(Project.new("foo", repo_paths=["/tmp/foo"]))
    reply = _Reply()
    handler = AppMentionHandler(store=store, reply=reply)

    await handler._handle(_event("<@U1> Fix the bug\nrepo:nonexistent please"))

    assert await store.list_tasks() == []
    assert len(reply.calls) == 1
    channel, thread_ts, text = reply.calls[0]
    assert "nonexistent" in text
    assert "not recognized" in text
    assert "foo" in text


# ------------------------------ valid repo: task creation ---------------------


async def test_valid_repo_creates_task_with_source_and_external_id(monkeypatch, store):
    await store.create_project(Project.new("foo", repo_paths=["/tmp/foo"]))
    reply = _Reply()
    handler = AppMentionHandler(store=store, reply=reply)

    await handler._handle(_event(
        "<@U1> Fix the login bug\nrepo:foo\nSteps: 1. click 2. crash",
        channel="C42", ts="1700000000.001200"))

    tasks = await store.list_tasks()
    assert len(tasks) == 1
    task = tasks[0]
    assert task.source == "slack"
    assert task.external_id == "C42+1700000000.001200"
    assert task.title == "Fix the login bug"
    assert task.description == "repo:foo\nSteps: 1. click 2. crash"
    assert task.repo_path == "/tmp/foo"
    assert reply.calls == []


# ------------------------------ dedupe on re-delivery --------------------------


async def test_redelivery_same_channel_and_ts_does_not_duplicate(monkeypatch, store):
    await store.create_project(Project.new("foo", repo_paths=["/tmp/foo"]))
    reply = _Reply()
    handler = AppMentionHandler(store=store, reply=reply)
    event = _event("<@U1> Fix the bug\nrepo:foo", channel="C9", ts="1234.5678")

    await handler._handle(event)
    tasks_after_first = await store.list_tasks()
    assert len(tasks_after_first) == 1

    # Re-delivery: Slack's own retry mechanism re-sends the identical event.
    await handler._handle(event)
    tasks_after_second = await store.list_tasks()
    assert len(tasks_after_second) == 1
    assert tasks_after_second[0].id == tasks_after_first[0].id


async def test_concurrent_redelivery_creates_only_one_task(monkeypatch, store):
    """PR #54 review MAJOR 1: two deliveries of the SAME event racing each
    other on the loop (not sequential) must still dedupe to a single task —
    proves the check-then-insert is serialized, not just fast enough to
    usually not collide."""
    await store.create_project(Project.new("foo", repo_paths=["/tmp/foo"]))
    reply = _Reply()
    handler = AppMentionHandler(store=store, reply=reply)
    event = _event("<@U1> Fix the bug\nrepo:foo", channel="C9", ts="9999.0001")

    results = await asyncio.gather(
        handler._handle(event), handler._handle(event),
        return_exceptions=True,
    )
    assert not any(isinstance(r, Exception) for r in results)

    tasks = await store.list_tasks()
    assert len(tasks) == 1


# ------------------------------ task creation failure --------------------------


async def test_create_task_failure_replies_in_thread_and_does_not_raise(monkeypatch, store):
    await store.create_project(Project.new("foo", repo_paths=["/tmp/foo"]))
    reply = _Reply()
    handler = AppMentionHandler(store=store, reply=reply)

    async def _boom(task):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(store, "create_task", _boom)

    await handler._handle(_event("<@U1> Fix the bug\nrepo:foo"))  # must not raise

    assert await store.list_tasks() == []
    assert len(reply.calls) == 1
    assert "Failed to create the task" in reply.calls[0][2]


# ------------------------------ reply-failure resilience -----------------------


async def test_reply_failure_does_not_raise_or_suppress_event(monkeypatch, store, caplog):
    """PR #54 review MAJOR 2: a broken transport (reply() raises) must be
    logged, must not propagate out of _handle, and must not swallow the
    on_event telemetry that follows it."""
    await store.create_project(Project.new("foo", repo_paths=["/tmp/foo"]))

    def _broken_reply(channel, thread_ts, text):
        raise RuntimeError("chat_postMessage failed")

    events = []
    handler = AppMentionHandler(
        store=store, reply=_broken_reply,
        on_event=lambda kind, detail: events.append((kind, detail)),
    )

    with caplog.at_level("WARNING", logger="no_human.integrations.slack.intake"):
        await handler._handle(_event("<@U1> Fix the bug\nno repo mentioned here"))

    assert await store.list_tasks() == []
    assert events and events[0][0] == "slack_mention_missing_repo"
    assert any("slack reply failed" in r.message for r in caplog.records)


def test_dispatch_future_exception_is_logged(caplog):
    """The run_coroutine_threadsafe future is no longer discarded silently —
    a done-callback logs a handler failure instead of letting it vanish."""
    handler = AppMentionHandler(store=object(), reply=_Reply())
    future = Future()
    future.set_exception(RuntimeError("boom"))

    with caplog.at_level("ERROR", logger="no_human.integrations.slack.intake"):
        handler._log_handle_failure(future)

    assert any("app_mention handler failed" in r.message for r in caplog.records)
