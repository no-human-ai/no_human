"""`nh task show` silently DELETES bracketed text instead of rendering it.

Rich markup is enabled by default on the module `console`, so `[...]` in an
f-string interpolated straight into `console.print(...)` is parsed as a style
tag: `never_push_to=[main,master]` renders as `never_push_to=` while, on the
very same line, `forbidden_paths=[]` survives untouched — an empty bracket
pair is not a valid tag, so it is not consumed. That asymmetry is exactly what
makes the deletion invisible at a glance: the line "looks fine" while the one
piece of information it existed to carry is gone. The stored row is correct;
this is purely a rendering defect in `task_show` (`src/no_human/cli/commands.py`).

These tests pin the BEHAVIOUR — render real operator-supplied text containing
brackets through the real `nh task show` command and assert the output still
contains it byte-for-byte, plus assert the stored row is untouched (so a
sanitise-on-write "fix" would fail these). No assertion here reads the source
text of any module: no regex over source, no AST walk, no
`inspect.getsource`. Only `no_human.cli.commands.cli`, `Store`, `Task`,
`TaskStatus`, `CliRunner`, `sqlite3` and `asyncio` are imported.

ENUMERATION (the open question in the task description) — other call sites in
`commands.py` that interpolate operator/model text into a markup-enabled
print/rule and share this exact root cause, found by inspection while fixing
`task_show`. Per the resolved intake decision, these are documented here for
future action and are NOT changed by this patch:

  * `task_show`, the slot-wait event line (~1668-1679) — machine-generated
    text, pinned separately by `tests/test_slot_wait_followups.py`.
  * `task create`'s confirmation echo of `t.title` (~1161).
  * the one-liner task-creation echo of `t.title` (~1275).
  * `task list`'s table cell `t.title[:50]` (~1646) — `rich.table.Table`
    cells parse markup too.
  * `investigate --show`'s `console.rule(f"...{t.title[:60]}")` (~5627) —
    the report body right below it is already `escape()`d.
  * `recall`'s query echo (~4529) — the result rows themselves are already
    `escape()`d (~4500/4521/4526).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from click.testing import CliRunner

from no_human.cli.commands import cli
from no_human.core.db import Store
from no_human.core.task import Task

pytestmark = pytest.mark.usefixtures("isolated_env_file")

PAYLOAD = "never_push_to=[main,master], forbidden_paths=[]"


def _make_runner(db, monkeypatch):
    import no_human.cli.commands as cmd_mod

    class _Cfg:
        data: dict = {}
        db_path = db

        def get(self, key, default=None):
            return self.data.get(key, default)

    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    return CliRunner()


def _seed(db_path, **fields):
    async def _go():
        async with Store(db_path) as store:
            t = Task.new(
                fields.pop("title", "Fix thing"),
                repo_path=fields.pop("repo_path", "/tmp/repo"),
                description=fields.pop("description", None),
            )
            for k, v in fields.items():
                setattr(t, k, v)
            await store.create_task(t)
            return t
    return asyncio.run(_go())


def test_description_with_brackets_survives_the_render(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    t = _seed(db, description=PAYLOAD)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "show", t.id[:8]])

    assert result.exit_code == 0, result.output
    for line in t.description.splitlines():
        assert line in result.output, result.output
    assert "never_push_to=[main,master]" in result.output, result.output
    assert "never_push_to=," not in result.output, result.output


def test_title_with_brackets_survives_the_render(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    t = _seed(db, title=PAYLOAD)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "show", t.id[:8]])

    assert result.exit_code == 0, result.output
    assert f"title: {t.title}" in result.output, result.output


def test_acceptance_criteria_with_brackets_survive_the_render(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    c1 = "never_push_to=[main,master]"
    c2 = "forbidden_paths=[], allowed=[x]"
    t = _seed(db, acceptance_criteria=[c1, c2])
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "show", t.id[:8]])

    assert result.exit_code == 0, result.output
    assert f"  - {c1}" in result.output, result.output
    assert f"  - {c2}" in result.output, result.output


def test_repo_path_with_brackets_survives_the_render(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    repo = "/tmp/repo[main,master]"
    t = _seed(db, repo_path=repo)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "show", t.id[:8]])

    assert result.exit_code == 0, result.output
    assert f"repo: {repo}" in result.output, result.output


def test_blocker_with_brackets_survives_the_render(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    t = _seed(db, blocker={"question": "never_push_to=[main,master]"})
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "show", t.id[:8]])

    assert result.exit_code == 0, result.output
    assert "blocker:" in result.output, result.output
    assert "never_push_to=[main,master]" in result.output, result.output


def test_unbalanced_tag_does_not_crash_the_render(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    t = _seed(db, description="a [/] b")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "show", t.id[:8]])

    assert result.exit_code == 0, result.output
    assert "a [/] b" in result.output, result.output


def test_emoji_shortcode_is_not_substituted(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    t = _seed(db, description="score :100: percent")
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "show", t.id[:8]])

    assert result.exit_code == 0, result.output
    assert "score :100: percent" in result.output, result.output


def test_bracketed_text_round_trips_byte_exact_in_the_database(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    title = PAYLOAD
    description = PAYLOAD
    acs = ["never_push_to=[main,master]", "forbidden_paths=[]"]
    repo = "/tmp/repo[main]"
    t = _seed(db, title=title, description=description,
              acceptance_criteria=acs, repo_path=repo)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "show", t.id[:8]])
    assert result.exit_code == 0, result.output

    async def _reread():
        async with Store(db) as store:
            return await store.find_task(t.id)

    reread = asyncio.run(_reread())
    assert reread.title == title
    assert reread.description == description
    assert reread.acceptance_criteria == acs
    assert reread.repo_path == repo

    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT title, description, acceptance_criteria FROM tasks WHERE id = ?",
            (t.id,),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == title
    assert row[1] == description
    assert json.loads(row[2]) == acs
