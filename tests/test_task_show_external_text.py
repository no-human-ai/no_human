"""Square brackets in task text are eaten or crash the CLI renderer.

`nh task show`/`nh task list` print a task's title, description, acceptance
criteria and repo path through Rich's markup-enabled `console.print`. Rich
parses `[...]` in that text as a style tag: a well-formed bracket pair (a
Python list literal, a regex character class, a `[WIP-BLOCKED]` tag) is
silently dropped, and an unbalanced closing bracket (`[/close]` with no
matching open tag) raises `rich.markup.MarkupError` and takes the whole
command down. Both are reproduced here BEFORE asserting the fix, and the
third test proves the fix does not flatten the program's OWN style tags
(status/kind colours) along with the externally-sourced text.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from click.testing import CliRunner
from rich.console import Console

from no_human.cli.commands import cli
from no_human.core.db import Store
from no_human.core.task import Task

pytestmark = pytest.mark.usefixtures("isolated_env_file")


def _make_runner(path, monkeypatch):
    import no_human.cli.commands as cmd_mod

    class _Cfg:
        data: dict = {}
        db_path = path

        def get(self, key, default=None):
            return self.data.get(key, default)

    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    return CliRunner()


def _seed_task(db, *, title, description=None, acceptance_criteria=None, kind="feature"):
    async def _go():
        async with Store(db) as s:
            t = Task.new(title, repo_path="/tmp/repo", description=description, kind=kind)
            if acceptance_criteria is not None:
                t.acceptance_criteria = acceptance_criteria
            await s.create_task(t)
            return t.id

    return asyncio.run(_go())


# --------------------------------------------------------------------------- #
# AC1 — characters survive intact                                             #
# --------------------------------------------------------------------------- #

def test_task_show_renders_brackets_in_description_and_criteria(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    description = (
        "return [str(t).strip() for t in tests if str(t).strip()]\n"
        "match a regex class [A-Za-z_]+ and a link [docs](http://x)"
    )
    tid = _seed_task(
        db,
        title="[WIP-BLOCKED] fix",
        description=description,
        acceptance_criteria=["match [0-9]{3}"],
    )

    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "show", tid[:8]])

    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "[WIP-BLOCKED] fix" in result.output
    assert "return [str(t).strip() for t in tests if str(t).strip()]" in result.output
    assert "[A-Za-z_]+" in result.output
    assert "[docs](http://x)" in result.output
    assert "match [0-9]{3}" in result.output


# --------------------------------------------------------------------------- #
# AC2 — an unbalanced closing bracket never raises                            #
# --------------------------------------------------------------------------- #

def test_task_show_survives_an_unbalanced_closing_bracket(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    tid = _seed_task(db, title="ordinary task", description="fix [oops] and [/close]")

    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "show", tid[:8]])

    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "fix [oops] and [/close]" in result.output


def test_task_show_unfixed_renderer_would_raise_markup_error():
    """Pins down the defect this module guards against: printing the same
    text directly through a markup-enabled console (the pre-fix shape of
    `task_show`) raises, rather than merely mis-rendering."""
    from rich.errors import MarkupError

    console = Console(file=io.StringIO(), force_terminal=False)
    with pytest.raises(MarkupError):
        console.print(f"description: {'fix [oops] and [/close]'}")


# --------------------------------------------------------------------------- #
# AC2b — the same shape through `task list` (the `Table.add_row` path)        #
# --------------------------------------------------------------------------- #

def test_task_list_survives_a_markup_shaped_title(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    _seed_task(db, title="fix [oops] and [/close] please")

    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "list"])

    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "fix [oops] and" in result.output


# --------------------------------------------------------------------------- #
# AC3 — the program's OWN styling (status/kind colours) is unaffected         #
# --------------------------------------------------------------------------- #

def test_task_show_keeps_its_own_styling(tmp_path, monkeypatch):
    import no_human.cli.commands as cmd_mod

    db = tmp_path / "test.db"
    tid = _seed_task(
        db, title="styling check", description="plain description [not-a-tag]",
        kind="bugfix",
    )

    runner = _make_runner(db, monkeypatch)
    styled_console = Console(
        file=io.StringIO(), force_terminal=True, color_system="truecolor",
        width=200, record=True,
    )
    monkeypatch.setattr(cmd_mod, "console", styled_console)

    result = runner.invoke(cli, ["task", "show", tid[:8]])
    assert result.exit_code == 0, result.output

    styled = styled_console.export_text(styles=True, clear=False)
    # bold id, blue status value, magenta kind — the program's own literal
    # style tags in `task_show`'s header line, unaffected by the fix.
    assert f"\x1b[1m{tid}\x1b[0m" in styled
    assert "\x1b[34mpending\x1b[0m" in styled
    assert "\x1b[35mbugfix\x1b[0m" in styled
    # the description's characters are present and in order (checked on the
    # plain-text export, since Rich's default repr-highlighter may still
    # apply its OWN incidental styling around escaped brackets — a separate,
    # pre-existing concern from the markup-parsing bug this module guards).
    plain = styled_console.export_text(styles=False)
    assert "plain description [not-a-tag]" in plain
