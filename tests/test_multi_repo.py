"""Tests for multi-repo task support (Phase D — WS-E)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from no_human.core.task import Task, TaskStatus
from no_human.core.multi_repo import (
    RepoResult,
    MultiRepoOutcome,
    all_repo_paths,
    cross_repo_context,
    linked_repos_block,
    validate_linked_repos,
    store_multi_repo_outcome,
)


# --------------------------------------------------------------------------- #
# all_repo_paths                                                               #
# --------------------------------------------------------------------------- #


def test_all_repo_paths_single():
    t = Task.new("t", repo_path="/repos/a")
    assert all_repo_paths(t) == ["/repos/a"]


def test_all_repo_paths_multi():
    t = Task.new("t", repo_path="/repos/a")
    t.linked_repos = ["/repos/b", "/repos/c"]
    assert all_repo_paths(t) == ["/repos/a", "/repos/b", "/repos/c"]


def test_all_repo_paths_dedup():
    t = Task.new("t", repo_path="/repos/a")
    t.linked_repos = ["/repos/a", "/repos/b"]  # dup of primary
    assert all_repo_paths(t) == ["/repos/a", "/repos/b"]


def test_all_repo_paths_no_primary():
    t = Task.new("t")
    t.linked_repos = ["/repos/x"]
    assert all_repo_paths(t) == ["/repos/x"]


# --------------------------------------------------------------------------- #
# cross_repo_context                                                           #
# --------------------------------------------------------------------------- #


def test_cross_repo_context_single_repo():
    t = Task.new("t", repo_path="/repos/a")
    assert cross_repo_context(t, "/repos/a") == ""


def test_cross_repo_context_multi_repo():
    t = Task.new("t", repo_path="/repos/a")
    t.linked_repos = ["/repos/b"]
    ctx = cross_repo_context(t, "/repos/a")
    assert "MULTI-REPO" in ctx
    assert "/repos/a" in ctx
    assert "/repos/b" in ctx
    assert "(current)" in ctx


def test_cross_repo_context_linked_repo_current():
    t = Task.new("t", repo_path="/repos/a")
    t.linked_repos = ["/repos/b"]
    ctx = cross_repo_context(t, "/repos/b")
    assert "LINKED repo" in ctx
    assert "/repos/a" in ctx


# --------------------------------------------------------------------------- #
# linked_repos_block (D19 — the planner's path map)                            #
# --------------------------------------------------------------------------- #


def test_linked_repos_block_empty_for_single_repo():
    """Single-repo prompts must stay byte-identical (prompt-cache prefix)."""
    assert linked_repos_block(Task.new("t", repo_path="/repos/a")) == ""


def test_linked_repos_block_lists_linked_not_primary():
    t = Task.new("t", repo_path="/repos/a")
    t.linked_repos = ["/repos/metrics-core-service"]
    block = linked_repos_block(t)
    assert "/repos/metrics-core-service" in block
    # The primary repo is already named elsewhere in the planner prompt.
    assert "/repos/a" not in block
    # The instruction that answers D19's "not on disk" assumption.
    assert "Never assume a linked repo is absent" in block


def test_linked_repos_block_ignores_primary_listed_as_linked():
    t = Task.new("t", repo_path="/repos/a")
    t.linked_repos = ["/repos/a", "/repos/b"]
    block = linked_repos_block(t)
    assert "/repos/b" in block
    assert block.count("/repos/a") == 0


# --------------------------------------------------------------------------- #
# validate_linked_repos                                                        #
# --------------------------------------------------------------------------- #


def test_validate_linked_repos_all_exist(tmp_path):
    repo_a = tmp_path / "a"
    repo_a.mkdir()
    (repo_a / ".git").mkdir()
    repo_b = tmp_path / "b"
    repo_b.mkdir()
    (repo_b / ".git").mkdir()

    t = Task.new("t", repo_path=str(repo_a))
    t.linked_repos = [str(repo_b)]
    assert validate_linked_repos(t) == []


def test_validate_linked_repos_missing(tmp_path):
    t = Task.new("t", repo_path=str(tmp_path / "nonexistent"))
    errors = validate_linked_repos(t)
    assert len(errors) == 1
    assert "not found" in errors[0]


# --------------------------------------------------------------------------- #
# MultiRepoOutcome                                                             #
# --------------------------------------------------------------------------- #


def test_multi_repo_outcome_all_succeeded():
    t = Task.new("t", repo_path="/repos/a")
    outcome = MultiRepoOutcome(
        task=t,
        results=[
            RepoResult(repo_path="/repos/a", status="succeeded", pr_url="https://pr1"),
            RepoResult(repo_path="/repos/b", status="succeeded", pr_url="https://pr2"),
        ],
    )
    assert outcome.all_succeeded
    assert not outcome.any_failed
    assert outcome.pr_urls == ["https://pr1", "https://pr2"]
    assert "✅" in outcome.summary()


def test_multi_repo_outcome_partial_failure():
    t = Task.new("t", repo_path="/repos/a")
    outcome = MultiRepoOutcome(
        task=t,
        results=[
            RepoResult(repo_path="/repos/a", status="succeeded", pr_url="https://pr1"),
            RepoResult(repo_path="/repos/b", status="failed", detail="test failure"),
        ],
    )
    assert not outcome.all_succeeded
    assert outcome.any_failed
    assert "❌" in outcome.summary()


# --------------------------------------------------------------------------- #
# store_multi_repo_outcome                                                     #
# --------------------------------------------------------------------------- #


def test_store_multi_repo_outcome():
    t = Task.new("t", repo_path="/repos/a")
    outcome = MultiRepoOutcome(
        task=t,
        results=[
            RepoResult(repo_path="/repos/a", status="succeeded", pr_url="https://pr1"),
        ],
    )
    store_multi_repo_outcome(t, outcome)
    assert t.context["multi_repo_pr_urls"] == ["https://pr1"]
    assert len(t.context["multi_repo_results"]) == 1


# --------------------------------------------------------------------------- #
# Task serialization with linked_repos                                         #
# --------------------------------------------------------------------------- #


def test_task_linked_repos_roundtrip():
    t = Task.new("multi", repo_path="/repos/a")
    t.linked_repos = ["/repos/b", "/repos/c"]
    row = t.to_row()
    assert json.loads(row["linked_repos"]) == ["/repos/b", "/repos/c"]
    restored = Task.from_row(row)
    assert restored.linked_repos == ["/repos/b", "/repos/c"]


def test_task_linked_repos_default_empty():
    t = Task.new("single", repo_path="/repos/a")
    assert t.linked_repos == []
    row = t.to_row()
    assert json.loads(row["linked_repos"]) == []


# --------------------------------------------------------------------------- #
# DB persistence of linked_repos                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_linked_repos_persist_roundtrip(store):
    t = Task.new("multi", repo_path="/repos/a")
    t.linked_repos = ["/repos/b", "/repos/c"]
    await store.create_task(t)
    restored = await store.get_task(t.id)
    assert restored is not None
    assert restored.linked_repos == ["/repos/b", "/repos/c"]


@pytest.mark.asyncio
async def test_linked_repos_empty_persist(store):
    t = Task.new("single", repo_path="/repos/a")
    await store.create_task(t)
    restored = await store.get_task(t.id)
    assert restored is not None
    assert restored.linked_repos == []


# --------------------------------------------------------------------------- #
# CLI: nh task add --linked-repo                                               #
# --------------------------------------------------------------------------- #


def test_task_add_linked_repo_in_help():
    from no_human.cli.commands import cli
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "add", "--help"])
    assert "--linked-repo" in result.output
