"""The conversational shell driven for real: Textual's Pilot presses the keys
and types the text, and the assertions read the RENDERED frame.

`export_screenshot()` renders the app to SVG through the same compositor a
terminal would drive, so the `<text>` runs it contains are what the operator
would actually see - not the widget attributes we happened to set. A test that
asserted on `Static.renderable` would pass even if the widget were never
mounted, zero-height, or covered.

The HTTP layer is a fake in every test here. Nothing in this file needs a
running server.
"""
from __future__ import annotations

import asyncio
import html
import json
import re
import sys
import time

import pytest
from click.testing import CliRunner
from rich.cells import cell_len
from textual.widgets import Static, RichLog

from no_human.cli import shell as shell_mod
from no_human.cli.api_client import NhApiError, NhServerUnreachable
from no_human.cli.commands import cli
from no_human.cli.shell import ShellApp, run_shell

SIZE = (150, 50)

#: A terminal narrow enough that the right-hand panes are thinner than
#: `RichLog.min_width` (78). Every layout test above runs at SIZE, where the
#: conversation pane happens to be ~78 columns wide - which is exactly why the
#: truncation this file now guards went unnoticed for so long.
NARROW = (84, 24)


def frame(app) -> str:
    """Everything the rendered screen says, as one whitespace-joined string."""
    svg = app.export_screenshot()
    runs = re.findall(r"<text[^>]*>(.*?)</text>", svg)
    text = " ".join(html.unescape(r) for r in runs)
    return text.replace("\xa0", " ")


def _t(**kw) -> dict:
    base = {"id": "0123456789ab", "title": "A task", "status": "pending"}
    base.update(kw)
    return base


class FakeClient:
    """Stands in for NhClient. Records every call; raises what it is told to."""

    def __init__(self, tasks=None, *, grill_frames=None, event_frames=None):
        self.tasks = list(tasks or [])
        self._grill_frames = list(grill_frames or [])
        self._event_frames = list(event_frames or [])
        self.calls: list[tuple] = []
        self.stream_cursors: list[str] = []
        self.grill_payloads: list[dict] = []
        self.created: list[dict] = []
        self.raise_on: dict[str, Exception] = {}
        self.diff_text = "diff --git a/x b/x"

    def _maybe_raise(self, name):
        exc = self.raise_on.pop(name, None)
        if exc is not None:
            raise exc

    async def board(self):
        self.calls.append(("board",))
        self._maybe_raise("board")
        return [dict(t) for t in self.tasks]

    async def act(self, task_id, verb):
        self.calls.append(("act", task_id, verb))
        self._maybe_raise("act")
        return {"status": "ok"}

    async def reply(self, task_id, answer):
        self.calls.append(("reply", task_id, answer))
        self._maybe_raise("reply")
        return {"status": "ok"}

    async def diff(self, task_id):
        self.calls.append(("diff", task_id))
        self._maybe_raise("diff")
        return self.diff_text

    async def events(self, task_id):
        self.calls.append(("events", task_id))
        self._maybe_raise("events")
        return [{"kind": "tool_use", "text": "Read app.py"}]

    async def create_task(self, **kw):
        self.calls.append(("create_task", kw))
        self._maybe_raise("create_task")
        self.created.append(kw)
        created = _t(id="newtask0001", title=kw["title"], status="pending")
        self.tasks.append(created)
        return created

    async def grill_stream(self, **payload):
        self.grill_payloads.append(payload)
        self._maybe_raise("grill_stream")
        batch = self._grill_frames.pop(0) if self._grill_frames else [{"kind": "done"}]
        for f in batch:
            yield f

    async def stream_events(self, task_id, *, last_event_id=""):
        self.calls.append(("stream_events", task_id))
        self.stream_cursors.append(last_event_id)
        for f in self._event_frames:
            yield f


async def type_line(pilot, text):
    pilot.app.query_one("#prompt").value = text
    await pilot.press("enter")
    await pilot.pause()
    # The submit handler kicks off a worker; let it run to completion.
    for _ in range(6):
        await pilot.pause()
        await asyncio.sleep(0)


async def wait_until(pilot, predicate, *, timeout=5.0, poll=0.005) -> bool:
    """Pump the app until `predicate()` holds. True if it did, False on deadline.

    Waiting a fixed number of pauses is a wait on a DURATION for a CONDITION,
    and it is wrong in both directions. Too short and a loaded machine fails a
    correct app - that is the classic flake. Long enough to be safe and it is
    worse than slow: the app keeps working the whole time. The reconnect test
    below used to run `range(20)` pauses against a 10ms reconnect timer, and
    measured on this machine that was ~19 seconds and ~1500 reconnects to prove
    a fact settled by the second one. Every one of those extra cycles was
    another chance to land on the shutdown race in `shell.py` - so the fixed
    duration did not merely tolerate the flake, it manufactured it.

    So: poll the condition, stop the moment it holds, and bound the wait with a
    deadline rather than a cycle count.

    The poll is a plain sleep and NOT `pilot.pause()`, which is the difference
    between ~2 reconnects and ~80. `pause()` returns when the app's message
    queue goes IDLE, and an app with a live 10ms reconnect timer never is - so
    each `pause()` here ran about a second whatever the condition had already
    done. Sleeping yields to the app just as well (it runs on its own task) and
    returns the moment the condition holds. A caller that goes on to assert on
    the RENDERED frame should `await pilot.pause()` itself; `pilot` stays in the
    signature because a wait on app state is meaningless without one.
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll)
    return True


def make_app(client, **kw):
    kw.setdefault("poll_interval", 0)   # 0 = no background polling in tests
    kw.setdefault("follow_reconnect", 0)  # 0 = one pass over the fake's frames
    return ShellApp(client, **kw)


# --------------------------------------------------------------------------- #
# Layout                                                                       #
# --------------------------------------------------------------------------- #

async def test_the_shell_mounts_lanes_conversation_and_detail_panes():
    app = make_app(FakeClient([_t()]))
    async with app.run_test(size=SIZE):
        for pane in ("#header", "#lanes", "#conversation", "#detail", "#prompt"):
            assert app.query_one(pane) is not None


def painted_rows(app, pane: str) -> list[str]:
    """The rows a terminal would actually PAINT for `pane`.

    A RichLog stores each write as a strip rendered at some width and then
    CROPS that strip to the visible width on the way to the screen
    (`RichLog.render_line` -> `Strip.crop_extend`). A line rendered wider than
    the pane is therefore cut here and nowhere else: `log.lines` still holds
    the whole sentence, so an assertion on the widget's state would pass while
    the operator reads half a word. Only the cropped rows tell the truth.
    """
    log = app.query_one(pane, RichLog)
    return [log.render_line(y).text for y in range(log.size.height)]


def reflowed(rows: list[str]) -> str:
    """The painted rows read back as running text, so a sentence split across
    two rows by word wrap compares equal to the sentence that was written."""
    return " ".join(" ".join(rows).split())


MOUNT_SENTENCE = (
    "It asks the same scoping questions the board does. "
    "/help for the slash commands."
)


async def test_the_mount_text_wraps_onto_a_second_row_on_a_narrow_terminal():
    """The regression: on an 84-column terminal the two lines that say what
    this shell IS were cut mid-word ("...in pla" / "...the boar"), because
    `RichLog.min_width` defaults to 78 and is applied AFTER `shrink` has
    clamped the render width down to the pane. See shell.py's compose()."""
    app = make_app(FakeClient([]))
    async with app.run_test(size=NARROW) as pilot:
        await pilot.pause()
        rows = painted_rows(app, "#conversation")

    used = [r for r in rows if r.strip()]
    assert len(used) >= 3, (
        "both mount sentences are longer than the pane, so a wrapping "
        f"conversation pane paints more than two rows; got {used!r}"
    )
    text = reflowed(used)
    assert MOUNT_SENTENCE in text, (
        f"the mount text was cut instead of wrapped; painted: {used!r}"
    )
    assert "say what you want done, in plain English." in text


@pytest.mark.parametrize("pane", ["#conversation", "#detail"])
async def test_a_line_wider_than_the_pane_survives_into_a_second_row(pane):
    """Not just the mount strings: ANY write wider than the pane has to wrap.
    Constraining callers to ~40 characters is not a fix, it is a workaround."""
    long_line = " ".join(f"word{n:02d}" for n in range(20))  # 139 cells
    app = make_app(FakeClient([]))
    async with app.run_test(size=NARROW) as pilot:
        await pilot.pause()
        app.query_one(pane, RichLog).clear()
        app.query_one(pane, RichLog).write(long_line)
        await pilot.pause()
        rows = painted_rows(app, pane)

    used = [r for r in rows if r.strip()]
    assert len(used) >= 2, f"{pane} painted one row for a 139-cell line: {used!r}"
    assert reflowed(used) == long_line, (
        f"{pane} cut the line instead of wrapping it; painted: {used!r}"
    )


async def test_a_long_title_wraps_under_its_own_column_in_the_lanes_pane():
    """A wrapped title used to fall back to column 0 on its second row, where
    it read as a new task. With the pane width fed to the renderer, every
    continuation row and the status row start under the title column, and
    the full title still reaches the screen — nothing is cut."""
    title = " ".join(f"word{n:02d}" for n in range(14))  # 97 cells
    app = make_app(FakeClient([_t(id="abcdef012345", title=title, status="implementing")]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        rows = _lanes_rows(app)
    first = next(n for n, r in enumerate(rows) if "abcdef01" in r)
    block = []
    for r in rows[first + 1:]:
        if not r.strip() or not r.startswith(" " * 13):
            break
        block.append(r)
    assert block, f"no indented continuation rows after the title row: {rows!r}"
    assert "implementing" in block[-1], f"status row is not the last indented row: {block!r}"
    painted = reflowed([rows[first]] + block[:-1])
    assert title in painted, f"title was cut or mis-wrapped: {painted!r}"


def _lanes_rows(app) -> list[str]:
    """The rows the lanes pane PAINTS, over its whole box. `render_lines`
    takes a region in the widget's outer coordinates, so cropping at
    `size.width` (the content box) would slice the last cell of every full
    row and hide an overflow behind the crop; the outer `region` is the
    honest window — it includes the 1-cell padding on each side."""
    from textual.geometry import Region
    lanes = app.query_one("#lanes", Static)
    return [strip.text for strip in lanes.render_lines(
        Region(0, 0, lanes.region.width, lanes.region.height))]


def _title_block(rows: list[str], short_id: str) -> tuple[str, list[str]]:
    first = next(n for n, r in enumerate(rows) if short_id in r)
    block = []
    for r in rows[first + 1:]:
        if not r.strip() or not r.startswith(" " * 13):
            break
        block.append(r)
    return rows[first], block


async def test_a_terminal_resize_rewraps_titles_under_the_column_at_once():
    """Review of 740827caf: the App-level `on_resize` repainted with the
    pane's PRE-resize width, so after 150→90 the continuation rows sat at
    column 1 until the next 3 s poll. The lanes pane now repaints on ITS
    resize, which carries the new size, and the wrap is right immediately."""
    title = " ".join(f"word{n:02d}" for n in range(14))
    app = make_app(FakeClient([_t(id="abcdef012345", title=title, status="implementing")]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        wide_width = app.query_one("#lanes", Static).region.width
        await pilot.resize_terminal(90, 30)
        await pilot.pause()
        rows = _lanes_rows(app)
        narrow_width = app.query_one("#lanes", Static).region.width
    assert narrow_width < wide_width, (wide_width, narrow_width)
    title_row, block = _title_block(rows, "abcdef01")
    assert block, f"no indented rows after resize: {rows!r}"
    assert "implementing" in block[-1], block
    assert title in reflowed([title_row] + block[:-1]), rows
    assert all(len(r) == narrow_width for r in [title_row] + block), rows
    # the continuation rows are not pressed against the pane's right edge:
    # a row that ends in a non-space at the last content cell was cropped
    assert all(r.endswith(" ") for r in block), block


async def test_no_lanes_row_overflows_the_pane_at_a_narrow_terminal():
    """Residual 2, proven at the App level: below `_TITLE_COLUMN + 10` (23)
    cells, the lanes pane used to hand Textual an unwrapped row, which Textual
    then wrapped on its own and pressed back to column 0. Read over the pane's
    OUTER `region` (see `_lanes_rows`'s docstring - `size` is the content box
    and would crop the last cell of every full row), every row must still fill
    the pane exactly and end in the 1-cell padding: nothing cropped, nothing
    bled past the edge, regardless of how narrow the pane got."""
    title = "one two three four five six seven eight nine ten eleven twelve"
    app = make_app(FakeClient([_t(id="narrow00abcd", title=title, status="implementing")]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.resize_terminal(40, 30)
        await pilot.pause()
        lanes = app.query_one("#lanes", Static)
        width = lanes.region.width
        assert width < 23, (
            f"precondition: this test only proves something below the 23-cell "
            f"threshold; got a pane {width} cells wide"
        )
        rows = _lanes_rows(app)
    assert rows, "no rows painted at all"
    for row in rows:
        assert cell_len(row) == width, (width, repr(row))
        assert row.endswith(" "), f"row pressed against the pane's edge: {row!r}"
    # the title is not just present cell-for-cell, it is still whole: nothing
    # dropped by the narrow-mode wrap, just re-flowed onto more rows
    assert title in reflowed(rows), reflowed(rows)


async def test_a_zwj_family_emoji_title_is_not_split_mid_grapheme_at_a_narrow_terminal():
    """Independent review of e6ccb0b11 found `_chop`'s old per-code-point
    accumulator overcounted a ZWJ family emoji - four people joined by three
    zero-width joiners is ONE 2-cell grapheme cluster, but summing `cell_len`
    per code point charged each person as its own glyph - and hard-broke the
    cluster mid-sequence. The test above uses only ASCII, so it can't tell
    this fix from the bug it replaces: Textual's own line handling already
    guarantees a lot regardless of what `_chop` hands it. It does NOT paper
    over this one - confirmed by literally swapping in the pre-fix module and
    running this exact test: on that code this title painted rows measuring
    13 cells instead of 14 (`' \U0001f468‍\U0001f469‍\U0001f467‍\U0001f466\U0001f468‍\U0001f469‍         '`,
    the family sliced apart right at a joiner) with the orphaned tail
    (`\U0001f467‍\U0001f466...`) picked up dangling at the start of the next row - so the
    same `cell_len(row) == width` assertion this file already uses IS the
    discriminator here; the dangling-ZWJ check just names the failure mode."""
    family = "\U0001f468‍\U0001f469‍\U0001f467‍\U0001f466"  # man-woman-girl-boy, ZWJ-joined
    title = family * 12
    app = make_app(FakeClient([_t(id="zwjrow00abcd", title=title, status="implementing")]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.resize_terminal(40, 30)
        await pilot.pause()
        lanes = app.query_one("#lanes", Static)
        width = lanes.region.width
        assert width < 23, (
            f"precondition: this test only proves something below the 23-cell "
            f"threshold; got a pane {width} cells wide"
        )
        rows = _lanes_rows(app)
    assert rows, "no rows painted at all"
    for row in rows:
        assert cell_len(row) == width, (width, repr(row))
        assert row.endswith(" "), f"row pressed against the pane's edge: {row!r}"
        stripped = row.strip()
        assert not stripped.startswith("‍") and not stripped.endswith("‍"), (
            f"a ZWJ family emoji was split mid-grapheme across two rows: {row!r}"
        )


async def test_a_vs16_emoji_title_keeps_its_column_at_a_less_narrow_terminal():
    """The other half of the same reviewer finding: a VS16 emoji (a base
    character plus U+FE0F, the invisible selector that forces emoji
    presentation) is ONE 2-cell grapheme, but the pre-fix `_chop` summed
    `cell_len` per code point and undercounted it, packing roughly twice as
    many hearts into a row as actually fit - reproducing the reviewer's own
    repro (`render_lanes([{title: '❤️'*30}], width=40)` painted a 67-cell
    row) confirms the miscount at the model level. At the App level, swapping
    in the pre-fix module for this exact test shows a second symptom of the
    same miscount: the wrapped continuation rows lose the title column
    entirely and bleed back to column 0 - `test_a_long_title_wraps_under_its_
    own_column_in_the_lanes_pane`'s literal regression, reintroduced by a
    grapheme a plain per-code-point sum gets wrong. That's the discriminator
    here (`cell_len(row) == width` alone stays true either way at this pane
    width, since Textual still pads whatever it's given to the exact width)."""
    heart = "❤️"  # heart + VS16: one 2-cell grapheme
    title = heart * 30
    app = make_app(FakeClient([_t(id="heartrow0abc", title=title, status="implementing")]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.resize_terminal(80, 30)
        await pilot.pause()
        lanes = app.query_one("#lanes", Static)
        width = lanes.region.width
        assert width >= 23, (
            f"precondition: this test proves the WIDE-mode title column is "
            f"kept; got a pane {width} cells wide (narrow mode)"
        )
        rows = _lanes_rows(app)
    assert rows, "no rows painted at all"
    for row in rows:
        assert cell_len(row) == width, (width, repr(row))
    title_row, block = _title_block(rows, "heartrow")
    assert block, f"no indented continuation rows after the title row: {rows!r}"
    assert "implementing" in block[-1], f"status row is not the last indented row: {block!r}"
    # every row between the title row and the status row is a wrapped title
    # continuation, and every one of them must still hang under the title
    # column - not bleed back to the pane's left margin, which is exactly
    # what the pre-fix miscount did to this title.
    PANE_LEFT_PADDING = 1
    title_column = PANE_LEFT_PADDING + 13
    for row in block[:-1]:
        assert row.startswith(" " * title_column), (
            f"a wrapped continuation row lost the title column: {row!r}"
        )
    # hearts carry no whitespace of their own, so `reflowed`'s space-joining
    # would insert a space at every wrap point and falsely "cut" the title
    # (see the CJK test below); reconstruct it the same way that test does -
    # strip the title column off each title row and concatenate with nothing
    # in between.
    title_rows = [title_row] + block[:-1]
    reconstructed_title = "".join(r[title_column:].rstrip() for r in title_rows)
    assert reconstructed_title == title, (
        f"the VS16 title was cut or mis-wrapped: {reconstructed_title!r}"
    )


async def test_a_double_width_title_still_renders_correctly_at_the_app_level():
    """Content-parity check for AC 4: a CJK title, painted for real through the
    Textual pipeline (not just `render_lanes` in isolation), still reaches the
    screen whole, and every other field on the row - the same set
    `test_every_field_visible_today_is_still_visible` pins at the model level -
    survives alongside it."""
    title = "回归测试" * 8  # 32 code points, 64 terminal cells
    # NOTE: this fixture used to also carry `approved_at` alongside
    # `status="paused_quota"`, rendering "approved - merge pending" beside a
    # paused row. That combination is exactly the stale-approval contradiction
    # this ticket's fix closes - `approval_pending` (core/lanes.py) now
    # requires `status == "awaiting_approval"`, so a stale approved_at on a
    # non-awaiting_approval row can no longer produce the chip. See
    # test_cli_shell_model.py::test_the_approved_chip_only_renders_for_a_live_awaiting_approval_row.
    app = make_app(FakeClient([_t(
        id="cjkrow00abcd", title=title, status="paused_quota",
        live_status="waits quota", claimed=True,
        blocker_human_stopped=True,
        subtask_progress="2/5 subtasks", total_tokens=1_234,
        total_cache_read=4_000_000,
    )]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        rows = _lanes_rows(app)
    title_row, block = _title_block(rows, "cjkrow00")
    assert block, f"no indented continuation rows after the title row: {rows!r}"
    # CJK text carries no whitespace of its own, so `reflowed`'s space-joining
    # would insert a space at every wrap point and falsely "cut" the title;
    # reconstruct it the same way the model-level test does instead - strip the
    # title column off each title row and concatenate with nothing in between.
    # `_TITLE_COLUMN` (13) is relative to the pane's content box (`size`), but
    # these rows were read over the OUTER `region`, which adds `#lanes`'s own
    # `padding: 0 1` - one more cell on the left - so the column starts at 14
    # here, not 13. The tag rows (after the title) are plain ASCII with real
    # spaces, so `reflowed` is exactly right for those.
    PANE_LEFT_PADDING = 1
    title_column = PANE_LEFT_PADDING + 13
    tag_start = next(i for i, r in enumerate(block) if "waits quota" in r)
    title_rows = [title_row] + block[:tag_start]
    reconstructed_title = "".join(r[title_column:].rstrip() for r in title_rows)
    assert reconstructed_title == title, (
        f"the CJK title was cut or mis-wrapped: {reconstructed_title!r}"
    )
    tag_text = reflowed(block[tag_start:])
    for needle in ("waits quota", "running", "waits for its own signal",
                   "you stopped it",
                   "2/5 subtasks", "4,001,234 tok"):
        assert needle in tag_text, needle


async def test_the_lanes_pane_draws_the_board_columns_in_board_order():
    tasks = [
        _t(id="aaaaaaaa1111", title="Ship it", status="done"),
        _t(id="bbbbbbbb2222", title="Answer me", status="escalated"),
        _t(id="cccccccc3333", title="In flight", status="implementing"),
    ]
    app = make_app(FakeClient(tasks))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        text = frame(app)
    order = [text.index(label) for label in
             ("Needs Answer", "Working", "Failed", "Review PR", "Done")]
    assert order == sorted(order)
    assert "Answer me" in text
    assert "In flight" in text
    assert "Ship it" in text


async def test_a_server_provided_lane_places_the_task_even_against_its_status():
    """The parallel branch's `lane` field is the truth when it is there."""
    app = make_app(FakeClient([_t(id="ffff0000aaaa", title="Server says review",
                                 status="pending", lane="review")]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app.lane_of("ffff0000aaaa") == "review"


# --------------------------------------------------------------------------- #
# "Needs you" is the point                                                     #
# --------------------------------------------------------------------------- #

async def test_the_header_shows_the_needs_you_count():
    tasks = [_t(id="a" * 12, status="escalated"),
             _t(id="b" * 12, status="awaiting_approval"),
             _t(id="c" * 12, status="implementing")]
    app = make_app(FakeClient(tasks))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        text = frame(app)
    assert "NEEDS YOU: 2" in text


async def test_the_header_says_all_clear_when_nothing_is_waiting_on_you():
    app = make_app(FakeClient([_t(status="implementing")]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        text = frame(app)
    assert "NEEDS YOU" not in text
    assert "all clear" in text


async def test_the_first_task_that_needs_you_is_selected_on_startup():
    """Not merely the topmost row. The first row here sits in Needs Answer but
    has already had its answer (the human stopped it), so it is NOT a gate -
    landing on it would open the shell on the one task that wants nothing."""
    tasks = [
        _t(id="stopped00001", status="escalated", blocker_human_stopped=True),
        _t(id="work00000001", status="implementing"),
        _t(id="gate00000001", status="awaiting_approval"),
    ]
    app = make_app(FakeClient(tasks))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app.selected_id == "gate00000001"


async def test_the_burn_on_screen_includes_cache_reads():
    app = make_app(FakeClient([
        _t(id="burner000001", title="Expensive", status="implementing",
           total_tokens=731, total_cache_read=4_000_000)]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        text = frame(app)
    assert "4,000,731" in text


# --------------------------------------------------------------------------- #
# Selection and the event tail                                                 #
# --------------------------------------------------------------------------- #

async def test_ctrl_n_and_ctrl_p_walk_the_selection_in_lane_order():
    tasks = [_t(id="cccccccc3333", status="done"),
             _t(id="aaaaaaaa1111", status="escalated"),
             _t(id="bbbbbbbb2222", status="implementing")]
    app = make_app(FakeClient(tasks))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app.selected_id == "aaaaaaaa1111"
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert app.selected_id == "bbbbbbbb2222"
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert app.selected_id == "cccccccc3333"
        await pilot.press("ctrl+p")
        await pilot.pause()
        assert app.selected_id == "bbbbbbbb2222"


async def test_the_detail_pane_tails_the_selected_tasks_event_stream():
    client = FakeClient(
        [_t(id="tailme000001", title="Tail me", status="implementing")],
        event_frames=[{"kind": "tool_use", "text": "Edit shell.py"}],
    )
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        for _ in range(8):
            await pilot.pause()
            await asyncio.sleep(0)
        text = frame(app)
    assert ("stream_events", "tailme000001") in client.calls
    assert "Edit shell.py" in text


async def test_the_detail_pane_reopens_the_stream_the_server_closed():
    """The server ends this stream five idle ticks after the task leaves the
    inflight set - about six seconds for anything parked, queued or awaiting
    approval, which is most of what this surface shows. Running it once left
    the pane permanently silent, and re-selecting the task was the only cure.

    The reconnect carries the `last-event-id` cursor, so it resumes rather than
    replaying the server's 200-event deque.

    The wait is on the CONDITION - a second stream open - under a deadline, not
    on a cycle count; `wait_until`'s docstring records what the count cost."""
    client = FakeClient(
        [_t(id="tailme000001", status="implementing")],
        event_frames=[{"kind": "tool_use", "text": "Edit shell.py", "ts": 1712.5}],
    )
    app = make_app(client, follow_reconnect=0.01)
    async with app.run_test(size=SIZE) as pilot:
        reopened = await wait_until(pilot, lambda: len(client.stream_cursors) >= 2)
        opened = [c for c in client.calls if c[0] == "stream_events"]
        cursors = list(client.stream_cursors)
    assert reopened, "the stream never re-opened within the deadline"
    assert len(opened) > 1, "one pass and the pane goes dead for good"
    assert cursors[0] == ""
    assert cursors[1] == "1712.5", "a reconnect resumes, it does not replay"


async def test_quitting_while_the_stream_is_live_exits_instead_of_crashing():
    """Textual's `App._shutdown()` closes the screens - which unmounts every
    widget - and only the message loop's own `finally` cancels the workers,
    strictly afterwards. `_follow` is therefore scheduled at least once with no
    `#detail` left: `query_one` raised `NoMatches`, Textual re-raised it as
    `WorkerFailed`, and quitting `nh shell` with the detail pane streaming
    printed a traceback instead of exiting.

    This is what made the reconnect test above flaky rather than the reconnect
    logic: that test spun the loop ~1500 times, and each pass was another draw
    against this race. Against an endless stream the race is not rare at all -
    10/10 crashes before the guard in `shell.py`, 0/10 after.
    """
    class Endless(FakeClient):
        """A stream the server never closes, so the worker is always inside
        the write path when the app tears down."""

        async def stream_events(self, task_id, *, last_event_id=""):
            self.calls.append(("stream_events", task_id))
            while True:
                await asyncio.sleep(0)
                yield {"kind": "tool_use", "text": "Edit shell.py", "ts": 1.0}

    client = Endless([_t(id="tailme000001", status="implementing")])
    app = make_app(client, follow_reconnect=0.001)
    async with app.run_test(size=SIZE) as pilot:
        streaming = await wait_until(
            pilot, lambda: ("stream_events", "tailme000001") in client.calls)
        assert streaming, "the stream never opened, so nothing was under test"
    # `run_test.__aexit__` re-raises whatever killed a worker, so arriving here
    # is already the verdict - but say it out loud, because a silent pass is
    # exactly what this test is here to stop being mistaken for.
    assert app._exception is None, app._exception


async def test_moving_the_selection_switches_which_stream_is_followed():
    client = FakeClient([_t(id="aaaaaaaa1111", status="escalated"),
                         _t(id="bbbbbbbb2222", status="implementing")])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+n")
        for _ in range(8):
            await pilot.pause()
            await asyncio.sleep(0)
    followed = [c[1] for c in client.calls if c[0] == "stream_events"]
    assert followed[-1] == "bbbbbbbb2222"


# --------------------------------------------------------------------------- #
# Natural language is the primary affordance                                   #
# --------------------------------------------------------------------------- #

QUESTION = [
    {"kind": "tool_use", "text": "Grep boardLanes.js"},
    {"kind": "grill_question", "type": "question",
     "question": "Which lane should the button live in?",
     "suggestions": ["Failed", "Review PR"], "round": 1},
    {"kind": "done"},
]
RESULT = [
    {"kind": "eval_verdict", "verdict": "good", "rationale": "clear enough"},
    {"kind": "grill_result", "type": "done", "title": "Add a retry action",
     "description": "Refined description", "acceptance_criteria": ["ac one"]},
    {"kind": "done"},
]


async def test_typing_plain_text_runs_the_grill_and_renders_its_question():
    client = FakeClient([_t()], grill_frames=[QUESTION])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "add a retry button")
        text = frame(app)
    assert client.grill_payloads[0]["title"] == "add a retry button"
    assert client.grill_payloads[0]["qa_history"] == []
    assert "Which lane should the button live in?" in text
    assert "Grep boardLanes.js" in text
    assert "Failed" in text


async def test_the_answer_goes_back_as_qa_history_on_the_same_endpoint():
    client = FakeClient([_t()], grill_frames=[QUESTION, RESULT])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "add a retry button")
        await type_line(pilot, "the Failed lane")
        text = frame(app)
    assert client.grill_payloads[1]["qa_history"] == [
        {"question": "Which lane should the button live in?", "answer": "the Failed lane"}]
    assert "Add a retry action" in text
    assert "ac one" in text


async def test_accepting_the_refined_spec_creates_the_task():
    client = FakeClient([_t()], grill_frames=[RESULT])
    app = make_app(client, repo_path="/repo")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "add a retry button")
        await type_line(pilot, "yes")
    assert client.created == [{
        "title": "Add a retry action",
        "description": "Refined description",
        "repo_path": "/repo",
        "acceptance_criteria": ["ac one"],
    }]


async def test_saying_yes_creates_a_task_a_REAL_SERVER_MODEL_would_accept():
    """The one test the whole feature rests on, and the one the suite did not
    have: `FakeClient.create_task` accepts any keywords and `MockTransport`
    returned 201 for any body, so 115 tests passed while the shipped client put
    `kind: null, priority: null` on the wire and every single create 422'd.

    Here the transport runs the server's OWN `CreateTaskRequest` over the bytes
    the real `NhClient` sends, and answers 422 in FastAPI's shape when it does
    not validate - so a body the server would refuse fails HERE."""
    import httpx
    from pydantic import ValidationError

    from no_human.api.models import CreateTaskRequest
    from no_human.cli.api_client import NhClient

    sse = "".join(f"data: {json.dumps(f)}\n\n" for f in RESULT)
    posted: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/grill/stream":
            return httpx.Response(200, text=sse)
        if path == "/api/tasks" and request.method == "POST":
            body = json.loads(request.content)
            posted["body"] = body
            try:
                parsed = CreateTaskRequest(**body)
            except ValidationError as exc:
                return httpx.Response(422, json={"detail": exc.errors(
                    include_url=False, include_context=False, include_input=False)})
            posted["parsed"] = parsed
            return httpx.Response(201, json={"id": "newtask0001",
                                             "title": parsed.title,
                                             "status": "pending"})
        return httpx.Response(200, json=[])

    client = NhClient(transport=httpx.MockTransport(handler))
    app = make_app(client, repo_path="/repo")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "add a retry button")
        await type_line(pilot, "yes")
        text = frame(app)
    await client.aclose()

    assert "parsed" in posted, (
        f"the server model refused the client's body: {posted.get('body')}")
    assert posted["parsed"].title == "Add a retry action"
    assert posted["parsed"].acceptance_criteria == ["ac one"]
    assert posted["parsed"].kind == "feature"
    assert posted["parsed"].priority == "medium"
    assert "created newtask" in text


async def test_a_rejected_create_tells_the_operator_which_fields_were_rejected():
    """A bare `POST /api/tasks -> 422` in the conversation pane is what let a
    broken body ship. The field names have to reach the screen."""
    import httpx

    from no_human.cli.api_client import NhClient

    sse = "".join(f"data: {json.dumps(f)}\n\n" for f in RESULT)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/grill/stream":
            return httpx.Response(200, text=sse)
        if request.url.path == "/api/tasks" and request.method == "POST":
            return httpx.Response(422, json={"detail": [
                {"type": "string_type", "loc": ["body", "kind"],
                 "msg": "Input should be a valid string"}]})
        return httpx.Response(200, json=[])

    client = NhClient(transport=httpx.MockTransport(handler))
    app = make_app(client, repo_path="/repo")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "add a retry button")
        await type_line(pilot, "yes")
        text = frame(app)
        still_up = app.is_running
    await client.aclose()
    assert "kind" in text
    assert "valid string" in text
    assert still_up


async def test_declining_the_refined_spec_creates_nothing():
    client = FakeClient([_t()], grill_frames=[RESULT])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "add a retry button")
        await type_line(pilot, "no")
        text = frame(app)
    assert client.created == []
    assert "discarded" in text.lower()


async def test_a_grill_error_reads_as_a_sentence_and_the_shell_survives():
    client = FakeClient([_t()])
    client.raise_on["grill_stream"] = NhApiError("repo_path 'x' is not a git repository")
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "add a retry button")
        text = frame(app)
        still_up = app.is_running
    assert "not a git repository" in text
    assert "Traceback" not in text
    assert still_up


# --------------------------------------------------------------------------- #
# Slash commands                                                               #
# --------------------------------------------------------------------------- #

async def test_slash_approve_acts_on_the_selected_task():
    client = FakeClient([_t(id="gate00000001", status="awaiting_approval")])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "/approve")
    assert ("act", "gate00000001", "approve") in client.calls


@pytest.mark.parametrize("verb", ["pause", "resume", "cancel", "retry"])
async def test_the_lifecycle_slashes_hit_their_endpoints(verb):
    client = FakeClient([_t(id="gate00000001", status="escalated")])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, f"/{verb}")
    assert ("act", "gate00000001", verb) in client.calls


async def test_slash_reply_sends_the_message_to_the_selected_task():
    client = FakeClient([_t(id="gate00000001", status="awaiting_input")])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "/reply go with the second option")
    assert ("reply", "gate00000001", "go with the second option") in client.calls


async def test_slash_diff_renders_into_the_detail_pane():
    client = FakeClient([_t(id="gate00000001", status="awaiting_approval")])
    client.diff_text = "--- a/lanes.py\n+++ b/lanes.py\n+one added line"
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "/diff")
        text = frame(app)
    assert "one added line" in text


async def test_slash_logs_replays_the_event_log():
    client = FakeClient([_t(id="gate00000001", status="escalated")])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "/logs")
        text = frame(app)
    assert ("events", "gate00000001") in client.calls
    assert "Read app.py" in text


async def test_slash_help_lists_the_commands():
    app = make_app(FakeClient([_t()]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "/help")
        text = frame(app)
    assert "/approve" in text
    assert "/quit" in text


async def test_an_unknown_slash_names_the_real_commands_and_does_not_crash():
    client = FakeClient([_t(id="gate00000001", status="escalated")])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "/merge")
        text = frame(app)
        still_up = app.is_running
    assert "/merge is not a command" in text
    assert "/approve" in text
    assert still_up
    assert not any(c[0] == "act" for c in client.calls)


async def test_a_failed_slash_call_is_reported_plainly():
    client = FakeClient([_t(id="gate00000001", status="pending")])
    client.raise_on["act"] = NhApiError("cannot approve a task in status pending")
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "/approve")
        text = frame(app)
        still_up = app.is_running
    assert "cannot approve a task in status pending" in text
    assert "Traceback" not in text
    assert still_up


async def test_slash_quit_exits_the_shell():
    app = make_app(FakeClient([_t()]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await type_line(pilot, "/quit")
        await pilot.pause()
    assert not app.is_running


async def test_ctrl_c_leaves_the_shell():
    """Textual 8 does not quit on ctrl+c. `App` binds it to `help_quit`, whose
    own docstring says it "no longer quits", and a focused `Input` binds it to
    `copy` - and #prompt holds focus for the app's whole life. So the reflex
    every terminal user has did nothing here. Only a PRIORITY binding is
    checked ahead of the focused widget, which is why this is not a plain
    tuple in BINDINGS.

    Read INSIDE the context: `run_test`'s exit stops the app either way, so an
    assertion after the block passes whether or not the key did anything."""
    app = make_app(FakeClient([_t()]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert app.query_one("#prompt").has_focus, "the reflex fires while typing"
        await pilot.press("ctrl+c")
        await pilot.pause()
        left = not app.is_running
    assert left


async def test_ctrl_q_still_leaves_the_shell():
    app = make_app(FakeClient([_t()]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+q")
        await pilot.pause()
        left = not app.is_running
    assert left


# --------------------------------------------------------------------------- #
# The server going away                                                        #
# --------------------------------------------------------------------------- #

async def test_the_server_dropping_mid_session_says_how_to_start_it():
    client = FakeClient([_t()])
    app = make_app(client)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        client.raise_on["board"] = NhServerUnreachable(
            "no_human is not running at http://127.0.0.1:8420\nStart it first:\n  nh start")
        await type_line(pilot, "/help")  # any interaction; refresh runs on ctrl+r
        await pilot.press("ctrl+r")
        for _ in range(6):
            await pilot.pause()
            await asyncio.sleep(0)
        text = frame(app)
        still_up = app.is_running
    assert "nh start" in text
    assert still_up


def test_run_shell_refuses_plainly_when_nothing_is_listening(capsys, monkeypatch):
    """A real connect to a port nothing is bound to - no live server, no mock,
    and no traceback.

    The tty guard is forced open: pytest's captured stdio is not a terminal, so
    without this the run would stop at the guard and never reach the probe this
    test is about."""
    monkeypatch.setattr(shell_mod, "stdio_is_interactive", lambda: True)
    code = run_shell(base_url="http://127.0.0.1:1")
    out = capsys.readouterr().out
    assert code == 1
    assert "nh start" in out
    assert "Traceback" not in out


# --------------------------------------------------------------------------- #
# No terminal, no full-screen app                                              #
# --------------------------------------------------------------------------- #

class _FakeStdio:
    """Only the one method the guard asks about."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.mark.parametrize("stdin_tty,stdout_tty", [(False, True), (True, False),
                                                  (False, False)])
def test_the_shell_will_not_take_a_terminal_it_does_not_have(
        monkeypatch, stdin_tty, stdout_tty):
    """`nh </dev/null` used to hang forever: the probe passed, Textual took a
    screen nobody was watching, and the process never exited - 0 bytes on
    stdout and a full-screen paint on stderr. In a CI job or an agent's shell
    that is a wedge, and the SIGINT that eventually kills it exits 0, so the
    wedge reads as success.

    Port 1 is refused, so reaching the probe at all would return 1. Getting 2
    proves the guard runs BEFORE any network call, not after a 15 s connect
    timeout."""
    monkeypatch.setattr(sys, "stdin", _FakeStdio(stdin_tty))
    monkeypatch.setattr(sys, "stdout", _FakeStdio(stdout_tty))
    said: list[str] = []
    code = run_shell(base_url="http://127.0.0.1:1", _echo=said.append)
    assert code == 2
    out = "\n".join(said)
    assert "terminal" in out.lower()
    for verb in ("start", "approve", "watch", "status"):
        assert verb in out, "the fallback is the help it used to print"


def test_stdio_is_interactive_is_true_only_when_both_ends_are_a_terminal(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeStdio(True))
    monkeypatch.setattr(sys, "stdout", _FakeStdio(True))
    assert shell_mod.stdio_is_interactive() is True


def test_a_detached_stream_is_not_interactive(monkeypatch):
    """A closed or replaced stream raises rather than answering. Guessing
    `interactive` there is how the hang comes back."""
    class _Closed:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(sys, "stdin", _Closed())
    monkeypatch.setattr(sys, "stdout", _FakeStdio(True))
    assert shell_mod.stdio_is_interactive() is False


# --------------------------------------------------------------------------- #
# The `nh` entry point — additive, every existing verb untouched               #
# --------------------------------------------------------------------------- #

# Every verb registered on `nh` before the shell landed. Scripts and the
# installer call these; the shell is only allowed to ADD.
EXISTING_VERBS = {
    "init", "task", "repo", "config", "auth", "rules", "playbook",
    "merge-stack", "skills", "watch", "mcp-serve", "onboard", "docs",
    "ci-gate", "blocked", "reply", "wake", "serve", "status", "autonomy",
    "recall", "agents", "unblock", "approve", "review-comments", "reject",
    "diff", "review", "investigate", "logs", "learnings-curate", "learnings",
    "history", "doctor", "start", "dashboard", "stop", "eval", "bench",
    "shadow", "test",
}


def test_every_pre_existing_verb_is_still_registered():
    assert EXISTING_VERBS <= set(cli.commands)


def test_nh_help_still_lists_the_verbs():
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    for verb in ("start", "approve", "watch", "status"):
        assert verb in result.output


def test_nh_with_no_arguments_enters_the_shell(monkeypatch):
    import no_human.cli.shell as shell_mod
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return 0

    monkeypatch.setattr(shell_mod, "run_shell", fake)
    result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0
    assert seen != {}


def test_nh_shell_is_also_an_explicit_verb(monkeypatch):
    import no_human.cli.shell as shell_mod
    monkeypatch.setattr(shell_mod, "run_shell", lambda **kw: 0)
    result = CliRunner().invoke(cli, ["shell"])
    assert result.exit_code == 0


def test_nh_repo_before_the_shell_verb_is_not_dropped(monkeypatch):
    """`--repo` is declared at both levels so both orders read naturally. The
    group-level one used to be parsed and then silently discarded the moment a
    subcommand followed it, so `nh --repo /x shell` filed against the cwd."""
    import no_human.cli.shell as shell_mod
    seen = {}
    monkeypatch.setattr(shell_mod, "run_shell",
                        lambda **kw: (seen.update(kw), 0)[1])

    assert CliRunner().invoke(cli, ["--repo", "/x", "shell"]).exit_code == 0
    assert seen["repo_path"] == "/x"

    seen.clear()
    assert CliRunner().invoke(cli, ["shell", "--repo", "/y"]).exit_code == 0
    assert seen["repo_path"] == "/y"

    seen.clear()
    assert CliRunner().invoke(cli, ["--repo", "/x", "shell", "--repo", "/y"]).exit_code == 0
    assert seen["repo_path"] == "/y", "the nearer option wins"


def test_a_subcommand_still_runs_instead_of_the_shell(monkeypatch):
    import no_human.cli.shell as shell_mod

    def boom(**kw):
        raise AssertionError("the shell must not launch for a subcommand")

    monkeypatch.setattr(shell_mod, "run_shell", boom)
    result = CliRunner().invoke(cli, ["approve", "--help"])
    assert result.exit_code == 0
    assert "approve" in result.output


# --- the flow has ONE name, anchored on DESTINATION rather than on location ---

_RENDER_SINKS = {"print", "rule", "echo", "secho", "_say"}
"""Attribute calls that put a string in front of the operator verbatim."""

_CLICK_DECORATORS = {"command", "group"}
"""Click's own decorators. Click renders the docstring of a function they wrap
as the BODY of that command's ``--help`` (`nh shell --help` prints `shell_cmd`'s
docstring word for word), so those docstrings are operator-facing text."""

_LOG_LEVELS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
"""Logger method names. An argument of one of these goes to a log file, not to
a screen - a STRUCTURAL exemption, not a list of tolerated sentences."""

_PROMPTISH = re.compile(r"prompt", re.IGNORECASE)
"""Names that mark LLM prompt text: text addressed to a model, not a human."""


def _is_click_command(node) -> bool:
    """True if `node` is a function Click turns into a command or a group."""
    import ast

    for dec in getattr(node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = (target.attr if isinstance(target, ast.Attribute)
                else target.id if isinstance(target, ast.Name) else "")
        if name in _CLICK_DECORATORS:
            return True
    return False


def _operator_text_fields() -> dict[str, set[str]]:
    """Which fields of the intake payloads carry text a render loop prints,
    read off THE CODE'S OWN TYPES rather than a list kept here.

    `grill_step`'s return annotation names the payloads the CLI loop
    (`commands.py::_run_cli_grill`), the shell TUI and the web composer all
    render; every `str` / `list[str]` field of those dataclasses is text that
    ends up on a screen. Add a field, or a third payload type to the union, and
    it is covered without anyone editing this test.
    """
    import dataclasses
    import typing

    from no_human.intake.grill import grill_step

    annotation = typing.get_type_hints(grill_step)["return"]
    payloads = typing.get_args(annotation) or (annotation,)
    out: dict[str, set[str]] = {}
    for payload in payloads:
        if not dataclasses.is_dataclass(payload):
            continue
        hints = typing.get_type_hints(payload)
        names = set()
        for f in dataclasses.fields(payload):
            declared = hints.get(f.name, f.type)
            if declared is str or (typing.get_origin(declared) is list
                                   and str in typing.get_args(declared)):
                names.add(f.name)
        if names:
            out[payload.__name__] = names
    return out


def _operator_strings() -> list[tuple[str, int, str, str]]:
    """Every string literal in `no_human` classified by WHERE IT ENDS UP.

    The previous three versions of this guard classified by LOCATION - first a
    list of render sinks, then sinks unioned with prose-shape, then the `cli/`
    directory - and each rebuild moved the enumeration to a new axis instead of
    removing it. The `cli/` walk was `glob("*.py")`, so a `cli/panels/`
    subpackage was invisible, and `intake/grill.py` builds the suggestion
    buttons the CLI, the TUI and the web composer all print, from outside the
    directory entirely. That is how "B: Skip the grill" shipped under a flow
    called "Let's scope this".

    So the universe is now the whole package - `src/no_human`, recursively, no
    directory named anywhere - and each literal is admitted by its DESTINATION:

    * ``sink``       - an argument of a render call (`print`/`rule`/`echo`/
                       `secho`/`_say`), whatever the literal's shape. Catches a
                       bare one-word string prose-shape would miss.
    * ``click-help`` - a ``help=`` keyword: Click prints it under Options.
    * ``click-doc``  - the docstring of a Click command or group: Click prints
                       it as the ``--help`` body.
    * ``payload``    - a value given to a text field of an intake payload
                       (`_operator_text_fields`, discovered from the types), so
                       a render loop somewhere prints it.
    * ``prose``      - the literal holds a space, so it is a sentence and not an
                       identifier, an endpoint or a worker-group name. The
                       catch-all for text that reaches the operator by a route
                       nobody enumerated - `shell_input.py::help_text` RETURNS
                       its sentences rather than printing them, and a mutant
                       reverting that line survived the sink-only version.

    Two STRUCTURAL exemptions narrow ``prose`` only - never the four
    destinations above, which win outright - and neither is a list of tolerated
    sentences, so new text of the same shape is exempt without an edit here:

    * ``log``    - the literal is an argument of a logger call. It reaches a log
                   file; `nh doctor` and a maintainer read those, not a user
                   mid-flow.
    * ``prompt`` - the literal is assigned to a ``*prompt*`` name, or built into
                   an expression that reads one. It is addressed to a model.

    Docstrings are excluded EXCEPT Click's, which are rendered - the earlier
    claim that docstrings are "never rendered to anyone running the program" was
    simply false, and this branch edited `shell_cmd`'s docstring precisely
    because it IS the ``--help`` body.

    Returns (path, lineno, why, text); ``why`` in {sink, click-help, click-doc,
    payload, prose} is in scope, {log, prompt} is exempted and returned so the
    exemptions can be asserted live rather than trusted.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "no_human"
    text_fields = _operator_text_fields()
    found: list[tuple[str, int, str, str]] = []

    def collect(nodes, into: set) -> None:
        for n in nodes:
            for sub in ast.walk(n):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    into.add(id(sub))

    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = str(path.relative_to(root.parent.parent))

        plain_docs: set[int] = set()
        click_docs: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.FunctionDef,
                                     ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            first = node.body[0] if node.body else None
            if not (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                continue
            (click_docs if _is_click_command(node) else plain_docs).add(
                id(first.value))

        sinks: set[int] = set()
        helps: set[int] = set()
        payloads: set[int] = set()
        logged: set[int] = set()
        prompted: set[int] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = (fn.attr if isinstance(fn, ast.Attribute)
                        else fn.id if isinstance(fn, ast.Name) else "")
                if isinstance(fn, ast.Attribute):
                    if fn.attr in _RENDER_SINKS:
                        collect(node.args, sinks)
                    if fn.attr in _LOG_LEVELS:
                        collect(node.args, logged)
                collect([kw.value for kw in node.keywords if kw.arg == "help"],
                        helps)
                fields = text_fields.get(name)
                if fields:
                    collect([kw.value for kw in node.keywords
                             if kw.arg in fields], payloads)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                value = node.value
                if value is None:
                    continue
                targets = (node.targets if isinstance(node, ast.Assign)
                           else [node.target])
                names = [t.id for t in targets if isinstance(t, ast.Name)]
                names += [n.id for n in ast.walk(value) if isinstance(n, ast.Name)]
                names += [n.attr for n in ast.walk(value)
                          if isinstance(n, ast.Attribute)]
                if any(_PROMPTISH.search(n) for n in names):
                    collect([value], prompted)

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in plain_docs:
                continue
            if id(node) in sinks:
                why = "sink"
            elif id(node) in helps:
                why = "click-help"
            elif id(node) in click_docs:
                why = "click-doc"
            elif id(node) in payloads:
                why = "payload"
            elif " " in node.value.strip():
                why = ("log" if id(node) in logged
                       else "prompt" if id(node) in prompted else "prose")
            else:
                continue
            found.append((rel, node.lineno, why, node.value))
    return found


_IN_SCOPE = ("sink", "click-help", "click-doc", "payload", "prose")
_EXEMPT = ("log", "prompt")


def test_the_intake_flow_has_one_name_in_everything_an_operator_reads():
    """The operator renamed the intake flow to "Let's scope this". Every string
    a person running no_human can read must call it that - wherever in the
    package the string is written, and whichever surface prints it.

    `--grill/--no-grill` is the ONE exemption and it is a TOKEN exemption, not a
    string one: the flag token is deleted from the text and what is left still
    has to be clean. The previous version searched the whole literal for the
    flag and dropped the whole literal when it matched, so any sentence that
    happened to mention `--no-grill` could say anything at all beside it.

    The internal vocabulary is untouched on purpose - `grill_step`,
    `GrillResult`, `/api/grill/stream` and `group="grill"` are identifiers, hold
    no space and pass no render sink, so they are out of scope by shape rather
    than by a list.

    KNOWN RESIDUALS, measured and stated rather than implied shut. A single word
    IS seen at a named render sink and IS seen in a payload field given by
    keyword; what is not seen is:

      * a single word reaching an operator by a route this guard does not name
        (`self._advisory("grill")` - `_advisory` both logs AND emits an event
        the shell's `_format_event` prints, so its PROSE is in scope, but one
        bare word through it is not),
      * a single word handed to an intake payload POSITIONALLY rather than by
        keyword,
      * text assembled at run time from pieces none of which reads "grill"
        (`"gr" + "ill"`), which no static walk can see,
      * anything outside Python: `web/`, the docs, the desktop app.

    Closing the first two means rendering every command's real output, which
    this file does for the shell TUI and which
    `test_the_renamed_suggestion_reaches_the_operator` below does for the intake
    loop, but which cannot be done for every surface.
    """
    import re

    strings = _operator_strings()
    in_scope = [s for s in strings if s[2] in _IN_SCOPE]

    # Non-vacuity. A discovery scan that finds nothing passes for the same
    # reason a correct one does, so every detector must be shown alive, both
    # exemptions must be shown FIRING on the real tree, and the renamed rule
    # must be among what the walk found - which is also what pins it.
    assert len(in_scope) > 100, f"the AST walk found only {len(in_scope)} strings"
    seen = {why for _, _, why, _ in strings}
    for why in _IN_SCOPE:
        assert why in seen, f"the {why!r} detector found nothing"
    for why in _EXEMPT:
        assert why in seen, f"the {why!r} exemption never fired, so it is dead"
    assert any("Let's scope this" in text for _, _, _, text in in_scope), \
        "the renamed intake rule is not among the strings the walk found"

    # The exemption removes the FLAG TOKEN, then the rest of the sentence is
    # judged on its own.
    flag = re.compile(r"--(?:no-)?grill\b")
    offenders = [
        (name, line, why, text) for name, line, why, text in in_scope
        if re.search(r"grill", flag.sub("", text), re.IGNORECASE)
    ]
    assert offenders == [], (
        "no_human still calls the intake flow 'grill' in text an operator can "
        f"read: {offenders}"
    )

    # The exemption is exercised rather than dead code: the flag's help IS found.
    assert any(flag.search(text) for _, _, _, text in in_scope), \
        "the walk never saw the --grill help text, so the exemption is untested"


def test_the_renamed_suggestion_reaches_the_operator(monkeypatch):
    """Anchor the guard above to the RENDERED artefact, not only to the source.

    `grill_step`'s `except asyncio.TimeoutError` branch builds the A/B
    suggestions and `commands.py::_run_cli_grill` prints them; this drives the
    real branch (a backend whose call times out) through the real loop and
    reads what the operator's terminal actually received. Nothing about the
    branch, the payload or the printing is stubbed - only the paid backend.
    """
    import asyncio
    import io

    import click

    from no_human.cli import commands as cmds

    class _TimingOutBackend:
        def __init__(self, *_a, **_kw):
            pass

        async def run(self, *_a, **_kw):
            raise asyncio.TimeoutError

    class _Cfg:
        primary_model = "claude-sonnet-5"
        _d = {"safety": {"forbidden_paths": []}, "git": {"never_push_to": []}}

        def __getitem__(self, key):
            return self._d[key]

    class _Task:
        title = "Add a retry to the uploader"
        description = None
        repo_path = None
        acceptance_criteria: list = []
        context: dict = {}

    monkeypatch.setattr(cmds, "ClaudeBackend", _TimingOutBackend)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # EOF ends the loop
    # Rich's own capture, NOT `console.file = ...`: assigning `.file` pins the
    # module-level Console to one stream for the rest of the process, and
    # restoring it pins it to the pre-test stdout - which silently emptied
    # `CliRunner(...).output` in 17 unrelated tests when I first wrote this.
    with cmds.console.capture() as captured:
        try:
            asyncio.run(cmds._run_cli_grill(_Cfg(), _Task(), store=None))
        except (click.exceptions.Abort, EOFError):
            pass

    rendered = captured.get()
    assert "Let's scope this" in rendered, rendered
    assert "A: Let me provide a more specific description" in rendered, rendered
    assert "grill" not in rendered.lower(), rendered
