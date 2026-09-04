"""Wiki-generation background job runner (wiki_jobs.py + Store CRUD).

The job survives wizard unmount and server restart: `run_job` drives a row
queued → running → done|failed, and `resume_unfinished` fails the orphans a
restart left behind. A generator that raises must become a failed row carrying
the reason, never an unhandled crash in the background task.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from no_human.core.db import Store
from no_human.docs_gen import WikiResult
from no_human.wiki_jobs import run_job, resume_unfinished


class _GenOk:
    async def generate(self, repo_path):
        return WikiResult(repo_path=repo_path, files_written=["a.md", "b.md"])


class _GenRaises:
    async def generate(self, repo_path):
        raise RuntimeError("boom")


@pytest.fixture
def fake_generator_ok():
    return _GenOk()


@pytest.fixture
def fake_generator_raises():
    return _GenRaises()


async def test_job_lifecycle_done(store, fake_generator_ok):
    jid = await store.create_wiki_job("/r")
    assert (await store.get_wiki_job(jid))["status"] == "queued"
    await run_job(store, jid, fake_generator_ok)
    row = await store.get_wiki_job(jid)
    assert row["status"] == "done" and json.loads(row["files"]) == ["a.md", "b.md"] and row["finished_at"]


async def test_job_failure_records_error(store, fake_generator_raises):
    jid = await store.create_wiki_job("/r")
    await run_job(store, jid, fake_generator_raises)
    row = await store.get_wiki_job(jid)
    assert row["status"] == "failed" and "boom" in row["error"]


async def test_resume_marks_orphans_failed(store):
    jid = await store.create_wiki_job("/r")
    await store.update_wiki_job(jid, status="running")
    await resume_unfinished(store)
    assert (await store.get_wiki_job(jid))["status"] == "failed"


async def test_list_wiki_jobs_filters_by_status(store, fake_generator_ok):
    done = await store.create_wiki_job("/done")
    await run_job(store, done, fake_generator_ok)
    await store.create_wiki_job("/queued")
    assert [j["repo_path"] for j in await store.list_wiki_jobs(status="done")] == ["/done"]
    assert {j["repo_path"] for j in await store.list_wiki_jobs()} == {"/done", "/queued"}
