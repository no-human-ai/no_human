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
`inspect.getsource` — the assertions read rendered OUTPUT and stored rows
only. (An earlier version of this sentence listed the imports; it named one
the file does not import and omitted three it does, so the list is gone
rather than corrected.)

ENUMERATION (the open question in the task description). The root cause is
not unique to `task_show`: the general RULE is that any `console.print` or
`rich.table.Table` cell in `commands.py` that interpolates task-owned or
model-owned text without `markup=False`/`escape()` is at risk of the same
silent deletion (or, for an unclosed tag, a crash) — whether or not a hand
enumeration below has caught it. Per the resolved intake decision, only
`task_show` is fixed by this patch; the rest are documented here for future
action, verified (not guessed) where noted:

  * `nh blocked` (~3518/~3521) renders `t.title` and `blocker.question` the
    exact same way `task_show` used to, unfixed — confirmed to both silently
    drop bracketed text and raise `rich.errors.MarkupError` on an unbalanced
    tag.
  * `nh task list` (~1647) renders `t.title[:50]` in a `Table` cell —
    confirmed to raise `MarkupError` and abort the entire listing on one bad
    title, not merely delete text from one row.
  * `nh agents --all` (~4726) renders the same `t.title[:50]` pattern in its
    own `Table` — same crash risk, a separate call site from `task list`'s.
  * `task_show`'s own slot-wait event lines — FIXED in this change, and
    listed here because the reason they were first dismissed was wrong twice
    over. They are not machine-generated: `pool_paused_text` embeds the
    auth-profile name the operator picked with `nh auth use`, so `acme[prod]`
    lost its bracket run and `acme[/]x` aborted the whole render with
    MarkupError. And `tests/test_slot_wait_followups.py` did not pin them —
    it contains zero rendering assertions (`grep -c console` -> 0 against 7
    test functions). Pinned now by
    `test_the_slot_wait_lines_do_not_parse_operator_text_as_markup`.
  * `task create`'s confirmation echo of `t.title` (~1162) and the one-liner
    task-creation echo of `t.title` (~1276).
  * the interactive scoping echo of title/description/acceptance-criteria
    (~505/507/509).
  * `investigate --show`'s `console.rule(f"...{t.title[:60]}")` (~5657) —
    the report body right below it is already `escape()`d.
  * `recall`'s query echo (~4529) — the result rows themselves are already
    `escape()`d (~4520-4530).
  * rules tables (~2615/~2988), playbooks (~2782) and transcripts (~6394).

  (Line numbers above are APPROXIMATE and some are known stale: the sites
  below `task_show` drifted by 177-220 lines when this change was re-cut onto
  a moved trunk, and nothing re-measured them. An earlier version of this
  sentence claimed they had been re-measured against this tree; that was
  false. Nothing gates them either — `scripts/reanchor_citations.py` covers
  `docs/security.md`, `docs/eval.md`, `docs/KNOWN_ISSUES.md` and
  CITATION_TABLE, so a test-file docstring is structurally ungated. Treat
  every number here as a search hint and re-grep the symbol.)

This list is a lead for follow-up work, not a claim of completeness: a hand
enumeration can miss a call site, or misjudge whether it deletes text versus
crashes, exactly as an earlier round of this list did.
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


def test_kind_with_brackets_survives_the_render(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    kind = "bugfix[main,master]"
    t = _seed(db, kind=kind)
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "show", t.id[:8]])

    assert result.exit_code == 0, result.output
    assert kind in result.output, result.output


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


def test_blocker_wraps_without_overrunning_console_width(tmp_path, monkeypatch):
    """A `console.print("[red]blocker:[/]", end=" ")` label followed by a
    separate `markup=False` payload print (an earlier round of this fix) made
    rich wrap the payload as though it started at column 0, ignoring the
    9-character "blocker: " prefix already written -- rows overran the
    console width. A single `Text("blocker: ", style="red")` + `.append(...)`
    print (this fix) folds the whole line, prefix included, as one unit.
    """
    import no_human.cli.commands as cmd_mod

    db = tmp_path / "test.db"
    long_question = "ab " * 30
    t = _seed(db, blocker={"question": long_question})
    runner = _make_runner(db, monkeypatch)

    result = runner.invoke(cli, ["task", "show", t.id[:8]])

    assert result.exit_code == 0, result.output
    width = cmd_mod.console.width
    for line in result.output.splitlines():
        assert len(line) <= width, (line, width, result.output)


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


def test_attempt_test_results_with_brackets_survive_the_render(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    t = _seed(db)
    runner = _make_runner(db, monkeypatch)

    async def _add_attempt():
        async with Store(db) as store:
            attempt_id = await store.create_attempt(t.id, 1)
            await store.update_attempt(
                attempt_id,
                status="failed",
                branch_name="task/x",
                pr_url="",
                turns_used=3,
                test_results={
                    "failing_tests": [
                        "tests/test_x.py::test_a[context]",
                        "tests/test_y.py::test_b[/tmp/wt]",
                    ]
                },
            )

    asyncio.run(_add_attempt())

    result = runner.invoke(cli, ["task", "show", t.id[:8]])

    assert result.exit_code == 0, result.output
    assert "test_a[context]" in result.output, result.output
    assert "test_b[/tmp/wt]" in result.output, result.output


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


def test_the_slot_wait_lines_do_not_parse_operator_text_as_markup(
        tmp_path, monkeypatch):
    """The residual instance of this ticket's own defect, inside the very
    command it names, found by review after the first fix was called done.

    `task_show` printed the pool-paused line as
    `console.print(f"[magenta]{pool_paused_text(pause)}[/]")`, and that text
    embeds the auth-profile name the OPERATOR chose with `nh auth use`. So a
    profile named `acme[prod]` had its bracket run deleted, and one named
    `acme[/]x` raised MarkupError and aborted the WHOLE `task show` render —
    the same abort-the-listing class the notes called out for `nh task list`,
    sitting one screen above the fields this change fixes.

    Driven through the REAL command, not by re-rendering the line here: a
    test that rebuilds the expected output from the code under test would
    pass even if `task_show` stopped calling this path at all. The crash
    spelling matters most, because it loses every other field too — so the
    assertions check that the TITLE still renders, which is only true if the
    command did not abort."""
    import io
    from rich.console import Console
    from no_human.cli import commands as _cmds

    db = tmp_path / "test.db"
    t = _seed(db, title="a distinctive title")
    runner = _make_runner(db, monkeypatch)

    monkeypatch.setattr(_cmds, "is_waiting_for_slot", lambda *a, **k: True)
    for profile in ("acme[prod]", "acme[/]x"):
        pause = {"reason": "quota", "until": "12:00", "profile": profile}
        monkeypatch.setattr(
            _cmds, "_running_pool_stats", lambda _c, _p=pause: (1, 2, _p))
        buf = io.StringIO()
        monkeypatch.setattr(
            _cmds, "console", Console(file=buf, force_terminal=True, width=200))
        result = runner.invoke(cli, ["task", "show", t.id[:8]])
        out = buf.getvalue() + result.output
        assert result.exit_code == 0, (
            f"profile {profile!r} aborted `task show`: {out}\n"
            f"{result.exception!r}")
        assert profile in out, (
            f"operator profile {profile!r} did not survive: {out!r}")
        assert "a distinctive title" in out, (
            f"the render aborted before the rest of the task: {out!r}")


def test_the_stale_pool_note_keeps_its_own_style_and_is_not_recoloured(
        tmp_path, monkeypatch):
    """The stale-pool line is TWO spans, and a base style silently merges
    them.

    Found by review after the slot-wait fix was called done. A comment here
    asserted these lines were "one colour by design"; that is true of the
    paused and plain branches and FALSE of this one, which renders a blue
    wait line followed by a DIM, DEFAULT-COLOUR note. Written as
    `Text(text, style="blue")` the base style colours everything appended
    after it, so the note rendered `2;34` (dim + blue) where trunk renders
    `2` (dim) -- the same base-style bleed the `blocker:` line is written to
    avoid, one branch away from the comment explaining it.

    It is invisible to every other test here: reverting that branch to a
    markup string left all of them green, because they assert on TEXT
    SURVIVING and this defect changes only the style run. So this asserts on
    the ANSI, which is the only place the difference exists."""
    import io
    from rich.console import Console
    from no_human.cli import commands as _cmds
    from no_human.core import slot_wait as _sw

    db = tmp_path / "test.db"
    t = _seed(db, title="a distinctive title")
    runner = _make_runner(db, monkeypatch)

    monkeypatch.setattr(_cmds, "is_waiting_for_slot", lambda *a, **k: True)
    # A REAL slot-wait event: this branch indexes `waits[-1]`, so faking only
    # the predicate leaves the list empty and the command dies on IndexError
    # rather than rendering the line under test.
    wait_event = {"kind": _sw.KIND, "text": "waiting for a worker slot (2/2 busy)"}
    async def _events(_task_id):
        return [wait_event]
    monkeypatch.setattr(_cmds.Store, "list_events", staticmethod(_events), raising=False)
    # `stats is None` is the stale-pool branch: the pool could not be reached.
    monkeypatch.setattr(_cmds, "_running_pool_stats", lambda _c: None)
    buf = io.StringIO()
    monkeypatch.setattr(
        _cmds, "console", Console(file=buf, force_terminal=True, width=200))
    result = runner.invoke(cli, ["task", "show", t.id[:8]])
    out = buf.getvalue() + result.output
    assert result.exit_code == 0, out
    assert "\x1b[" in out, f"forced terminal produced no ANSI: {out!r}"
    assert _sw.STALE_POOL_NOTE in out, out

    head, sep, tail = out.partition(_sw.STALE_POOL_NOTE)
    assert sep, out
    # The style run opened immediately before the note must be dim and must
    # NOT carry a colour. "2;34" is dim+blue -- the bleed.
    assert "2;34" not in head, (
        "the blue wait text bled into the dim note: the branch was built with "
        f"a base style instead of spans. {out!r}")


def test_the_blocker_label_is_styled_but_the_payload_is_not(tmp_path, monkeypatch):
    """The red goes on the LABEL SPAN, not on the `Text` object.

    `Text(s, style="red")` sets a BASE style, which every later `append()`
    inherits — so building the line that way silently reddens the operator's
    payload and loses trunk's label-only emphasis. That regression shipped
    once and was caught only by review; nothing pinned it, so reverting the
    span to a base style left every other test in this file green.

    The module's `console` is swapped for one with `force_terminal=True` so
    the real render emits ANSI: `CliRunner` strips it otherwise, and an
    assertion guarded on "if ANSI is present" would never run at all.
    """
    import io
    from rich.console import Console
    from no_human.cli import commands as _cmds

    db = tmp_path / "test.db"
    t = _seed(db, blocker={"category": "AMBIGUITY",
                           "question": "never_push_to=[main,master]"})
    runner = _make_runner(db, monkeypatch)

    buf = io.StringIO()
    monkeypatch.setattr(
        _cmds, "console", Console(file=buf, force_terminal=True, width=200))
    result = runner.invoke(cli, ["task", "show", t.id[:8]])
    assert result.exit_code == 0, buf.getvalue() + result.output

    out = buf.getvalue()
    assert "\x1b[" in out, f"forced terminal produced no ANSI: {out!r}"
    assert "never_push_to=[main,master]" in out, out

    head, sep, tail = out.partition("blocker: ")
    assert sep, out
    before_payload = tail.split("never_push_to")[0]
    assert "\x1b[0m" in before_payload, (
        "the payload sits inside the label's style run — the style was put on "
        f"the Text object rather than on the label span: {out!r}")
