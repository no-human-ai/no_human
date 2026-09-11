"""The pure pieces behind the conversational shell: lane grouping, the burn
figure, slash-command parsing, and the intake state machine.

None of this touches Textual or the network, so it is where the semantics get
pinned. The rendering tests live in tests/test_cli_shell_app.py and drive the
real app with Pilot.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from rich.cells import cell_len

from no_human.cli.shell_input import (
    SLASH_COMMANDS,
    IntakeSession,
    SlashError,
    help_text,
    is_slash,
    parse_slash,
)
from no_human.cli.shell_lanes import (
    LANE_KEYS,
    LANES,
    flat_order,
    group_by_lane,
    is_real_failure,
    is_waiting,
    lane_for,
    needs_you,
    needs_you_count,
    render_header,
    render_lanes,
    task_burn,
    total_burn,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _t(**kw) -> dict:
    base = {"id": "0123456789ab", "title": "A task", "status": "pending"}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# Lane routing — the server's field wins, local routing is the fallback        #
# --------------------------------------------------------------------------- #

def test_lane_keys_and_order_match_the_board():
    """web/src/boardLanes.js:19-25, left to right."""
    assert [lane.key for lane in LANES] == [
        "answer", "working", "failed", "review", "done"]
    assert [lane.label for lane in LANES] == [
        "Needs Answer", "Working", "Failed", "Review PR", "Done"]
    assert LANE_KEYS == {"answer", "working", "failed", "review", "done"}


def test_exactly_the_needs_you_lanes_are_loud():
    assert {lane.key for lane in LANES if lane.needs_you} == {"answer", "review"}


def test_lane_prefers_the_server_provided_field():
    """A parallel branch adds `lane` to the board payload. When it is there it
    is the truth — the CLI must not re-derive a second opinion."""
    assert lane_for(_t(status="pending", lane="review")) == "review"


@pytest.mark.parametrize("bogus", ["waiting", "", 7, True, {}, None])
def test_a_bogus_server_lane_falls_back_to_local_routing(bogus):
    """Rendering a phantom column because the payload said "waiting" is worse
    than routing it ourselves."""
    assert lane_for(_t(status="awaiting_approval", lane=bogus)) == "review"


def test_lane_field_absent_degrades_to_local_routing():
    """The branch that adds `lane` may not have merged yet."""
    assert lane_for(_t(status="escalated")) == "answer"
    assert lane_for(_t(status="implementing")) == "working"
    assert lane_for(_t(status="failed")) == "failed"
    assert lane_for(_t(status="done")) == "done"


def test_blocked_routes_on_its_wake_condition():
    """boardLanes.js:41-43 — with a wake condition it self-resolves (Working);
    without one a human owes it an answer."""
    assert lane_for(_t(status="blocked", blocker_wake_condition="CI goes green")) == "working"
    assert lane_for(_t(status="blocked")) == "answer"
    assert lane_for(_t(status="blocked", blocker_wake_condition=None)) == "answer"
    assert lane_for(_t(status="blocked", blocker_wake_condition="")) == "answer"


def test_unknown_status_falls_back_to_working_not_to_a_new_lane():
    """boardLanes.js:47. A status the CLI has never heard of must not invent a
    lane — no lane may claim a state the API does not report."""
    assert lane_for(_t(status="quantum_superposition")) == "working"
    assert lane_for(_t(status=None)) == "working"
    assert lane_for({}) == "working"


def test_is_waiting_covers_quota_pause_and_self_resolving_blocks():
    assert is_waiting(_t(status="paused_quota")) is True
    assert is_waiting(_t(status="blocked", blocker_wake_condition="pr merges")) is True
    assert is_waiting(_t(status="blocked")) is False
    assert is_waiting(_t(status="implementing")) is False


# --------------------------------------------------------------------------- #
# "Needs you" — the count in the header                                        #
# --------------------------------------------------------------------------- #

def test_needs_you_is_true_for_the_gate_lanes():
    assert needs_you(_t(status="awaiting_input")) is True
    assert needs_you(_t(status="escalated")) is True
    assert needs_you(_t(status="awaiting_approval")) is True
    assert needs_you(_t(status="blocked")) is True
    assert needs_you(_t(status="implementing")) is False
    assert needs_you(_t(status="done")) is False


def test_an_approved_pr_stops_shouting_but_keeps_its_lane():
    """boardLanes.js:71-73 — approved, waiting on the merge, not on you."""
    task = _t(status="awaiting_approval", approved_at="2026-07-30T00:00:00Z")
    assert needs_you(task) is False
    assert lane_for(task) == "review"


def test_a_human_stopped_blocker_stops_shouting_but_keeps_its_lane():
    """boardLanes.js:76 — the human already gave their answer."""
    task = _t(status="escalated", blocker_human_stopped=True)
    assert needs_you(task) is False
    assert lane_for(task) == "answer"


def test_needs_you_count_matches_the_loud_lane_membership():
    tasks = [
        _t(id="a", status="awaiting_input"),
        _t(id="b", status="awaiting_approval"),
        _t(id="c", status="awaiting_approval", approved_at="2026-07-30T00:00:00Z"),
        _t(id="d", status="implementing"),
        _t(id="e", status="blocked"),
        _t(id="f", status="blocked", blocker_wake_condition="ci"),
    ]
    assert needs_you_count(tasks) == 3


def test_an_escalated_task_with_a_stale_approval_shouts_again():
    """The bug this ticket fixes: an approval recorded on awaiting_approval,
    then the task escalates (or gets sent back / starts a new attempt) — the
    row now sits in Needs Answer but still carries the old approved_at.
    `approval_pending` must read this as NOT pending (status disagrees), so
    `needs_you` must count it and `lane_for` must NOT put it back in review."""
    task = _t(
        status="escalated",
        approved_at="2026-07-30T00:00:00Z",
        approval_superseded_at="2026-08-01T00:00:00Z",
    )
    assert needs_you(task) is True
    assert lane_for(task) == "answer"


def test_needs_you_count_excludes_a_stale_approval_but_counts_the_escalation():
    tasks = [
        _t(id="a", status="awaiting_approval", approved_at="2026-07-30T00:00:00Z"),
        _t(id="b", status="escalated", approved_at="2026-07-30T00:00:00Z",
           approval_superseded_at="2026-08-01T00:00:00Z"),
    ]
    # "a" stays quiet (live approval); "b" now shouts (superseded, escalated).
    assert needs_you_count(tasks) == 1


# --------------------------------------------------------------------------- #
# Grouping                                                                     #
# --------------------------------------------------------------------------- #

def test_group_by_lane_returns_every_lane_even_when_empty():
    groups = group_by_lane([_t(id="a", status="done")])
    assert list(groups) == ["answer", "working", "failed", "review", "done"]
    assert groups["done"][0]["id"] == "a"
    assert groups["answer"] == []


def test_group_by_lane_uses_the_server_field_per_task():
    groups = group_by_lane([
        _t(id="a", status="pending", lane="review"),
        _t(id="b", status="pending"),
    ])
    assert [t["id"] for t in groups["review"]] == ["a"]
    assert [t["id"] for t in groups["working"]] == ["b"]


def test_flat_order_walks_lanes_in_board_order():
    """Selection with ctrl+n/ctrl+p follows what the eye sees."""
    tasks = [
        _t(id="done1", status="done"),
        _t(id="work1", status="implementing"),
        _t(id="ans1", status="escalated"),
    ]
    assert flat_order(tasks) == ["ans1", "work1", "done1"]


# --------------------------------------------------------------------------- #
# Burn — never tokens_used alone                                               #
# --------------------------------------------------------------------------- #

def test_task_burn_sums_all_nine_buckets():
    """web/src/cost.js `totalBurn`: coder + reviewer + aux, fresh + creation +
    read. `nh logs` reported "tokens=731" for an attempt that spent 4M because
    it showed the first bucket alone."""
    task = _t(
        total_tokens=1, total_cache_read=10, total_cache_creation=100,
        total_review_tokens=1000, total_review_cache_read=10_000,
        total_review_cache_creation=100_000,
        total_aux_tokens=1_000_000, total_aux_cache_read=10_000_000,
        total_aux_cache_creation=100_000_000,
    )
    assert task_burn(task) == 111_111_111


def test_task_burn_is_never_the_fresh_bucket_alone():
    task = _t(total_tokens=731, total_cache_read=4_000_000)
    assert task_burn(task) == 4_000_731


def test_task_burn_treats_missing_and_null_buckets_as_zero():
    assert task_burn(_t()) == 0
    assert task_burn(_t(total_tokens=None, total_cache_read=5)) == 5


def test_total_burn_sums_the_board():
    assert total_burn([_t(total_tokens=2), _t(total_cache_read=3)]) == 5


# --------------------------------------------------------------------------- #
# Rendering — pure markup, asserted for truthfulness                           #
# --------------------------------------------------------------------------- #

def test_header_shows_the_needs_you_count():
    out = render_header([_t(status="escalated"), _t(status="awaiting_approval")])
    assert "NEEDS YOU" in out
    assert "2" in out


def test_header_says_all_clear_when_nothing_needs_you():
    out = render_header([_t(status="implementing")])
    assert "NEEDS YOU" not in out
    assert "all clear" in out.lower()


def test_header_burn_includes_cache_read():
    out = render_header([_t(total_tokens=731, total_cache_read=4_000_000)])
    assert "4,000,731" in out
    assert "731 " not in out.replace("4,000,731", "")


def test_render_lanes_labels_every_lane_and_its_count():
    out = render_lanes([_t(id="a1b2c3d4", title="Fix the gate", status="escalated")])
    for lane in LANES:
        assert lane.label in out
    assert "Fix the gate" in out
    assert "a1b2c3d4"[:8] in out


def test_render_lanes_shows_the_status_the_api_reported():
    out = render_lanes([_t(status="implementing", title="T")])
    assert "implementing" in out


def test_render_lanes_marks_the_selected_task():
    tasks = [_t(id="aaaa1111", status="escalated"), _t(id="bbbb2222", status="escalated")]
    out = render_lanes(tasks, selected_id="bbbb2222")
    selected_line = [ln for ln in out.splitlines() if "bbbb2222" in ln][0]
    other_line = [ln for ln in out.splitlines() if "aaaa1111" in ln][0]
    assert "reverse" in selected_line
    assert "reverse" not in other_line


# --------------------------------------------------------------------------- #
# Row shape — title on one part, status and tags on the next, aligned          #
# --------------------------------------------------------------------------- #

def _full_task(**kw) -> dict:
    """A task carrying EVERY field a row can show, so a test can assert that
    the row shape change dropped none of them."""
    base = _t(id="feedbeef1234", title="Gate must refuse to start",
              status="paused_quota", live_status="waits quota",
              claimed=True,
              blocker_human_stopped=True, subtask_progress="2/5 subtasks",
              total_tokens=1_234, total_cache_read=4_000_000)
    # NOTE: `approved_at` used to live in this fixture too, rendering
    # "approved - merge pending" alongside a `paused_quota` status. That
    # combination is exactly the contradiction this ticket's fix closes —
    # `approval_pending` now requires `status == awaiting_approval`, so a
    # stale approval on a non-awaiting_approval row can no longer produce the
    # chip. See test_the_approved_chip_only_renders_for_a_live_awaiting_approval_row.
    base.update(kw)
    return base


def _row_lines(out: str, short_id: str) -> tuple[str, str]:
    lines = out.splitlines()
    i = next(n for n, ln in enumerate(lines) if short_id in ln)
    return lines[i], lines[i + 1]


def test_a_row_is_title_then_an_aligned_meta_line():
    out = render_lanes([_full_task()])
    title_line, meta_line = _row_lines(out, "feedbeef")
    assert "Gate must refuse to start" in title_line
    # the status moved off the title line and onto its own, indented, line
    assert "waits quota" not in title_line
    assert "waits quota" in meta_line
    assert meta_line.startswith(" " * 13), repr(meta_line)


def test_every_field_visible_today_is_still_visible():
    out = render_lanes([_full_task()])
    _, meta_line = _row_lines(out, "feedbeef")
    for needle in ("waits quota", "running", "waits for its own signal",
                   "you stopped it", "2/5 subtasks", "4,001,234 tok"):
        assert needle in meta_line, needle


def test_status_takes_the_boards_colour_vocabulary():
    running = render_lanes([_t(id="aaaa0000", status="implementing", claimed=True)])
    waiting = render_lanes([_t(id="bbbb0000", status="paused_quota")])
    failed = render_lanes([_t(id="cccc0000", status="failed")])
    plain = render_lanes([_t(id="dddd0000", status="pending")])
    assert "[green]implementing[/]" in running
    assert "[yellow]paused_quota[/]" in waiting
    assert "[red]failed[/]" in failed
    assert "[dim]pending[/]" in plain


def test_a_long_title_wraps_with_a_hanging_indent_when_a_width_is_given():
    title = "one two three four five six seven eight nine ten eleven twelve"
    out = render_lanes([_t(id="eeee0000", title=title)], width=40)
    lines = out.splitlines()
    first = next(n for n, ln in enumerate(lines) if "eeee0000" in ln)
    continuation = lines[first + 1]
    # the title continues under the title column, not under the id
    assert continuation.startswith(" " * 13), repr(continuation)
    assert "twelve" in out
    # and the meta line comes AFTER the whole title, every line between indented
    meta = next(n for n in range(first + 1, first + 6) if "pending" in lines[n])
    assert "twelve" in "".join(lines[first:meta])
    assert all(lines[n].startswith(" " * 13) for n in range(first + 1, meta + 1))
    # no line of THIS task's block is wider than the pane (markup tags take
    # no cells; the lane's empty hints are not this test's business)
    import re
    for ln in lines[first:meta + 1]:
        assert len(re.sub(r"\[/?[^\]]*\]", "", ln)) <= 40, repr(ln)


def test_a_long_meta_line_wraps_under_the_title_column_too():
    """Seen on the live shell: a task carrying status, several tags and a
    nine-digit burn put its last tag back at column 0. Tags wrap as units —
    never mid-tag — and every overflow row keeps the indent."""
    out = render_lanes([_full_task(id="abab1212", title="Short")], width=40)
    import re
    lines = out.splitlines()
    first = next(n for n, ln in enumerate(lines) if "abab1212" in ln)
    block = []
    for ln in lines[first + 1:]:
        if not ln.startswith(" " * 13):
            break
        block.append(ln)
    assert len(block) >= 2, block
    plain = [re.sub(r"\[/?[^\]]*\]", "", ln) for ln in block]
    assert all(len(ln) <= 40 for ln in plain), plain
    joined = " ".join(" ".join(plain).split())
    for needle in ("waits quota", "running", "waits for its own signal",
                   "you stopped it", "2/5 subtasks", "4,001,234 tok"):
        assert needle in joined, needle
    # a tag is never split across rows
    assert not any(ln.rstrip().endswith("-") for ln in plain)


def test_the_approved_chip_only_renders_for_a_live_awaiting_approval_row():
    """AC2, pinned at the RENDER level (not just the payload/predicate): the
    board-CLI's actual row output must show "approved - merge pending" only
    when approval is live AND status is awaiting_approval — never for a
    stale approved_at sitting on any other status, including the exact
    contradictory shape the 16 broken rows had (approved_at present,
    status one of failed/escalated/implementing/paused_quota)."""
    live = _t(id="livexxxx1111", title="Live", status="awaiting_approval",
              approved_at="2026-08-20T10:00:00Z")
    stale_escalated = _t(id="escxxxxx2222", title="Stale-esc", status="escalated",
                          approved_at="2026-08-20T10:00:00Z",
                          approval_superseded_at="2026-08-21T00:00:00Z")
    stale_implementing = _t(id="implxxxx3333", title="Stale-impl", status="implementing",
                             approved_at="2026-08-20T10:00:00Z",
                             approval_superseded_at="2026-08-21T00:00:00Z")
    contradictory_no_marker = _t(id="contrxxx4444", title="Contradictory",
                                  status="paused_quota",
                                  approved_at="2026-08-20T10:00:00Z")

    out = render_lanes([live, stale_escalated, stale_implementing, contradictory_no_marker])
    _, live_meta = _row_lines(out, "livexxxx")
    _, esc_meta = _row_lines(out, "escxxxxx")
    _, impl_meta = _row_lines(out, "implxxxx")
    _, contra_meta = _row_lines(out, "contrxxx")

    assert "approved - merge pending" in live_meta
    assert "approved - merge pending" not in esc_meta
    assert "approved - merge pending" not in impl_meta
    assert "approved - merge pending" not in contra_meta


def test_a_single_tag_wider_than_the_pane_is_broken_not_overflowed():
    """Review of 740827caf: a long server-supplied `subtask_progress` was
    emitted as one row wider than the pane, which Textual then wrapped back
    to column 0 — the defect this change removes. A tag that alone exceeds
    the column is hard-wrapped and every piece keeps the indent."""
    long_tag = "phase 3 of 5: waiting on the reviewer to finish its second pass"
    out = render_lanes([_t(id="cafe0000", title="T", subtask_progress=long_tag)], width=40)
    import re
    lines = out.splitlines()
    first = next(n for n, ln in enumerate(lines) if "cafe0000" in ln)
    block = [ln for ln in lines[first + 1:first + 8] if ln.startswith(" " * 13)]
    plain = [re.sub(r"\[/?[^\]]*\]", "", ln) for ln in block]
    assert all(len(ln) <= 40 for ln in plain), plain
    assert long_tag in " ".join(" ".join(plain).split())


def test_cell_counting_treats_an_escaped_bracket_as_one_cell():
    """Review of 740827caf: `_cells("[dim]a\\[b[/]")` returned 2 for a
    3-cell string, because the markup regex swallowed the escaped bracket.
    A title or tag containing "[" must be measured as it renders."""
    from no_human.cli.shell_lanes import render_lanes as rl
    tag = "[x] 2/5 done"
    out = rl([_t(id="beef0000", title="T", subtask_progress=tag)], width=40)
    import re
    lines = out.splitlines()
    first = next(n for n, ln in enumerate(lines) if "beef0000" in ln)
    meta = [ln for ln in lines[first + 1:first + 4] if ln.startswith(" " * 13)]
    # protect escaped brackets FIRST, as Rich does, then strip the tags
    rendered = [re.sub(r"\[/?[^\]]*\]", "", ln.replace("\\[", "\x00")).replace("\x00", "[")
                for ln in meta]
    assert any(tag in ln for ln in rendered), rendered
    assert all(len(ln) <= 40 for ln in rendered), rendered


def test_without_a_width_the_title_is_never_wrapped():
    title = "x " * 80
    out = render_lanes([_t(id="ffff0000", title=title)])
    title_line, _ = _row_lines(out, "ffff0000")
    assert title.strip() in title_line


def test_empty_loud_lane_says_so_instead_of_going_blank():
    out = render_lanes([])
    assert "All caught up" in out


def _plain(line: str) -> str:
    """Strip Rich markup, protecting an escaped bracket first (as Rich does),
    so this measures the same string `_cells`'s arithmetic measures — the
    two-step approach `test_cell_counting_treats_an_escaped_bracket_as_one_cell`
    already pins."""
    return re.sub(r"\[/?[^\]]*\]", "", line.replace("\\[", "\x00")).replace("\x00", "[")


def test_a_double_width_title_is_measured_in_cells_not_code_points():
    """CJK text: every character is two cells. Measuring with `len()` (code
    points) would let a title that is actually 64 cells wide pass a 40-cell
    budget unwrapped, and Textual would then wrap it back to column 0."""
    title = "回归测试" * 8  # 32 code points, 64 terminal cells
    out = render_lanes([_t(id="cjk00000", title=title)], width=40)
    lines = out.splitlines()
    first = next(n for n, ln in enumerate(lines) if "cjk00000" in ln)
    meta = next(n for n in range(first + 1, first + 10) if "pending" in lines[n])
    block = lines[first:meta]
    for ln in block:
        assert cell_len(_plain(ln)) <= 40, repr(ln)
    assert len(block) > 1, block  # proves it actually wrapped
    # reflow: strip the fixed 13-cell header prefix from the first row, then
    # the indent from every continuation, and confirm the title survives
    # whole — nothing dropped, nothing reordered, nothing recoded
    header, *rest = block
    reflowed = _plain(header)[13:] + "".join(_plain(ln).strip() for ln in rest)
    assert reflowed == title


def test_a_double_width_tag_is_measured_in_cells():
    """A long CJK `subtask_progress` tag is measured in cells: measuring with
    `len()` would under-count its width and let it overflow the pane instead
    of hard-wrapping, the way test_a_single_tag_wider_than_the_pane_... above
    already pins for ASCII."""
    tag = "阶段三共五阶段等待评审完成第二遍检查工作进度报告清单确认无误提交合并"
    out = render_lanes([_t(id="wide0000", title="T", subtask_progress=tag)], width=40)
    lines = out.splitlines()
    first = next(n for n, ln in enumerate(lines) if "wide0000" in ln)
    block = [ln for ln in lines[first + 1:first + 12] if ln.startswith(" " * 13)]
    plain = [_plain(ln) for ln in block]
    assert all(cell_len(ln) <= 40 for ln in plain), plain
    assert tag in "".join(ln.strip() for ln in plain)


def test_a_zwj_family_emoji_title_is_not_split_mid_grapheme():
    """Independent review of e6ccb0b11/fa1d32f6c: the old per-code-point
    `_chop` (and, before it, `textwrap.wrap`) measured a ZWJ-joined family
    emoji one code point at a time, so a title too long to fit its wrap
    width got hard-broken INSIDE the four-person cluster - a lone joiner
    left dangling at the end of one row with its partner stranded at the
    start of the next. `rich.cells.chop_cells` (grapheme-aware) does not do
    this. Going through the full Textual app hides the bug (Static's own
    Rich-based renderer re-flows and happens to heal it), so this is
    checked at the `render_lanes` level, before Textual ever sees the text -
    confirmed by running this exact assertion against fa1d32f6c's `_chop`,
    which produces a row ending in a bare trailing ZWJ (and the next row
    starting with the stranded partner)."""
    family = "\U0001f468‍\U0001f469‍\U0001f467‍\U0001f466"  # man-woman-girl-boy
    title = family * 12
    out = render_lanes([_t(id="zwjrow00", title=title, status="implementing")], width=18)
    lines = out.splitlines()
    first = next(n for n, ln in enumerate(lines) if "zwjrow00" in ln)
    end = next(n for n in range(first, len(lines)) if lines[n] == "")
    block = [_plain(ln) for ln in lines[first:end]]
    for ln in block:
        assert cell_len(ln) <= 18, (18, repr(ln))
        stripped = ln.strip()
        assert not stripped.startswith("‍") and not stripped.endswith("‍"), (
            f"a ZWJ family emoji was split mid-grapheme across rows: {ln!r}"
        )


def test_a_vs16_emoji_title_is_not_split_mid_grapheme():
    """The VS16 half of the same finding: a base character plus U+FE0F (the
    'render as emoji' selector) is one 2-cell grapheme. The old code-point
    accumulator could hard-break between the base and its selector,
    stranding a bare heart on one row and a lone selector at the start of
    the next - both malformed on a real terminal. Checked before Textual,
    same reasoning as the ZWJ test above: at width=40 fa1d32f6c's `_chop`
    never even fires (the whole title, measured whole, "fits" in one
    67-cell row per the reviewer's own probe) - it is the per-word length
    check upstream of `_chop` that undercounts by using code points, so this
    also pins AC1's "none exceeds 40 cells"."""
    title = "❤️" * 30  # heart + VS16, thirty times
    out = render_lanes([_t(id="vs16row0", title=title, status="implementing")], width=40)
    lines = out.splitlines()
    first = next(n for n, ln in enumerate(lines) if "vs16row0" in ln)
    end = next(n for n in range(first, len(lines)) if lines[n] == "")
    block = [_plain(ln) for ln in lines[first:end]]
    for ln in block:
        assert cell_len(ln) <= 40, (40, repr(ln))
        cleaned = ln.replace("❤️", "")
        assert "❤" not in cleaned and "️" not in cleaned, (
            f"a VS16 emoji was split mid-grapheme across rows: {ln!r}"
        )


def test_a_pane_narrower_than_the_title_column_still_wraps():
    """Below a 23-cell pane, `_TITLE_COLUMN` (13) leaves under 10 cells for
    the title — too narrow to wrap sensibly with a hanging indent, so
    residual 2 used to bail out entirely and hand Textual an unwrapped row,
    which wraps it back to column 0. Below the threshold, wrap on the bare
    pane width instead, with no indent."""
    title = "one two three four five six seven eight nine ten eleven twelve"
    for width in (10, 15, 20):
        out = render_lanes([_full_task(id="narrow01", title=title)], width=width)
        lines = out.splitlines()
        first = next(n for n, ln in enumerate(lines) if "narrow0" in ln)
        end = next(n for n in range(first, len(lines)) if lines[n] == "")
        block = lines[first:end]
        for ln in block:
            assert cell_len(_plain(ln)) <= width, (width, repr(ln))
        # proves it no longer bails out to Textual: there IS a wrapped
        # continuation, and it carries NO indent at these widths
        assert len(block) > 3, (width, block)
        assert not any(ln.startswith(" " * 13) for ln in block), (width, block)
        assert "twelve" in "".join(block)


def test_below_two_cells_wrapping_is_left_to_textual():
    """`_layout` returns None below 2 cells — nothing sensible to wrap to —
    so the title is emitted unwrapped, same as the `width is None` path."""
    title = "a long title that would otherwise wrap into many short rows"
    out = render_lanes([_t(id="tiny0001", title=title)], width=1)
    title_line, _ = _row_lines(out, "tiny0001")
    assert title in title_line


def test_an_empty_lane_hint_is_wrapped_to_the_pane():
    """Residual 3: 'All caught up - nothing needs your input' is 43 cells —
    wider than many real terminal panes — and used to be emitted as one
    unwrapped row that Textual then wrapped back to column 0."""
    hint = "All caught up - nothing needs your input"

    for width in (36, 20):  # 36: wide-mode branch; 20: narrow-mode branch
        out = render_lanes([], width=width)
        lines = out.splitlines()
        for ln in lines:
            assert cell_len(_plain(ln)) <= width, (width, repr(ln))
        # nothing reworded or truncated: the full hint reassembles
        reflowed = " ".join(
            " ".join(_plain(ln).strip() for ln in lines if _plain(ln).strip()).split())
        assert hint in reflowed, (width, reflowed)


def test_without_a_width_the_empty_hint_is_the_single_line_it_always_was():
    out = render_lanes([])
    hint_lines = [ln for ln in out.splitlines() if "All caught up" in ln]
    assert len(hint_lines) == 1
    assert "All caught up - nothing needs your input" in hint_lines[0]


def test_a_lane_header_is_left_unwrapped_in_narrow_mode():
    """Narrow-mode fallback pinned per the plan's Section E: unlike titles,
    tags and empty hints, a lane header mixes independently-styled spans
    (the gate bar's background fill, or `[bold ...]label[/] [dim]count[/]`)
    that `_cell_wrap` cannot split without restyling each fragment, so it is
    left unwrapped even below the 23-cell threshold — see the NOTE next to
    `lines.append(head)` in `render_lanes`. Textual still wraps an
    over-width header itself; this only pins that `render_lanes` does not."""
    out = render_lanes([_t(id="deadbeef", status="awaiting_input")], width=10)
    lines = out.splitlines()
    header = next(ln for ln in lines if "Needs Answer" in ln)
    assert cell_len(_plain(header)) > 10, repr(header)


# --------------------------------------------------------------------------- #
# Failed lane — an operator cancel stays listed but drops out of the count     #
# --------------------------------------------------------------------------- #

def test_is_real_failure_excludes_a_cancelled_task():
    """boardLanes.js `isRealFailure` ported: `failed` + `cancelled` is an
    operator stop, not a capability failure."""
    assert is_real_failure(_t(status="failed")) is True
    assert is_real_failure(_t(status="failed", cancelled=True)) is False
    assert is_real_failure(_t(status="done")) is False
    assert is_real_failure(None) is False


def test_render_lanes_failed_count_excludes_cancels_but_lists_both_rows():
    """Two FAILED tasks, one an operator cancel: the header count must read 1,
    both rows must still be listed, and the cancelled row must carry a
    distinguishing tag — the same "list it, don't hide it" treatment the
    board's Failed Outcomes table gives a cancelled row."""
    tasks = [
        _t(id="aaaa1111", title="Real failure", status="failed"),
        _t(id="bbbb2222", title="Cancelled task", status="failed", cancelled=True),
    ]
    out = render_lanes(tasks)

    failed_head = [ln for ln in out.splitlines() if "Failed" in ln][0]
    assert "1" in failed_head, failed_head
    assert "2" not in failed_head, failed_head
    assert "Real failure" in out
    assert "Cancelled task" in out
    _, cancelled_meta = _row_lines(out, "bbbb2222")
    _, real_meta = _row_lines(out, "aaaa1111")
    assert "cancelled" in cancelled_meta
    assert "cancelled" not in real_meta


def test_render_lanes_failed_lane_with_only_cancels_shows_rows_not_the_empty_hint():
    """A lane holding only cancelled rows must not print its empty-hint above
    a row that IS there — the exact regression a prior fix introduced by
    gating the empty-hint on the filtered count instead of on row presence."""
    out = render_lanes([_t(id="cccc3333", title="Only a cancel", status="failed",
                            cancelled=True)])

    assert "No failures" not in out
    assert "Only a cancel" in out


# --------------------------------------------------------------------------- #
# Slash commands — every one maps to an endpoint that already exists           #
# --------------------------------------------------------------------------- #

def test_the_documented_slash_set_is_exactly_what_is_registered():
    assert set(SLASH_COMMANDS) == {
        "approve", "diff", "logs", "pause", "resume", "cancel", "reply",
        "retry", "help", "quit",
    }


def test_every_slash_route_is_a_route_the_server_already_serves():
    """No invented server behaviour: each command's path template must appear
    verbatim in a decorator in api/app.py."""
    source = (REPO_ROOT / "src" / "no_human" / "api" / "app.py").read_text(encoding="utf-8")
    for name, spec in SLASH_COMMANDS.items():
        if spec.path is None:
            continue
        decorator = f'@app.{spec.method.lower()}("{spec.path}"'
        assert decorator in source, f"/{name} -> {decorator} not found in api/app.py"


def test_is_slash_only_for_a_leading_slash():
    assert is_slash("/approve") is True
    assert is_slash("  /approve") is True
    assert is_slash("approve the pr") is False
    assert is_slash("fix /api/tasks routing") is False
    assert is_slash("") is False


def test_parse_slash_defaults_to_the_focused_task():
    cmd = parse_slash("/approve", selected_id="deadbeef")
    assert (cmd.name, cmd.task_id, cmd.arg) == ("approve", "deadbeef", "")


def test_parse_slash_takes_an_explicit_task_id():
    cmd = parse_slash("/diff 1234abcd", selected_id="deadbeef")
    assert (cmd.name, cmd.task_id) == ("diff", "1234abcd")


def test_parse_slash_without_a_task_says_which_task():
    err = parse_slash("/approve", selected_id=None)
    assert isinstance(err, SlashError)
    assert "task" in err.message.lower()


def test_reply_keeps_the_whole_message_as_its_argument():
    cmd = parse_slash("/reply use the second option, not the first",
                      selected_id="deadbeef")
    assert cmd.task_id == "deadbeef"
    assert cmd.arg == "use the second option, not the first"


def test_reply_with_an_explicit_id_splits_id_from_message():
    cmd = parse_slash("/reply 1234abcd go with option 2", selected_id="deadbeef")
    assert cmd.task_id == "1234abcd"
    assert cmd.arg == "go with option 2"


def test_a_head_word_with_a_trailing_newline_is_not_read_as_a_task_id():
    """`$` matches before a trailing newline, `\\Z` does not.

    The head word is split on a literal SPACE, not on whitespace, so a newline
    rides along inside it: `/reply 1234abcd\\n <message>` puts "1234abcd\\n" in
    the id slot, and a `$` anchor accepted that as an id.

    A BACKSTOP, NOT A LIVE EXPLOIT — the reachability was checked in both
    directions and the value cannot get here, nor out if it did:

    * It cannot reach `parse_slash`. The sole caller is `shell.py`'s
      `on_input_submitted`, fed by a stock Textual `Input` (no subclass, no
      `on_paste` override, no non-TTY feed). Textual 8.2.7 keeps only
      `event.text.splitlines()[0]` on paste and inserts a keystroke only when
      `event.is_printable`, which is False for `\\n`.
    * It would not reach the wire. `api_client.reply` interpolates the id into
      `/api/tasks/{task_id}/reply`, and httpx refuses to build that URL
      (`InvalidURL: Invalid non-printable ASCII character in URL`). That is not
      caught by `shell.py`'s `(NhServerUnreachable, NhApiError)` handler, so the
      outcome would be a crashed worker, not a control character in a request.

    So this pins the anchor as the MIDDLE of three guards, on the principle that
    a backstop must hold on its own: the Textual one is upstream of it, the
    httpx one downstream, and neither is stated at this line. Do not read the
    test as evidence of a live defect.
    """
    cmd = parse_slash("/reply 1234abcd\n go with option 2",
                      selected_id="deadbeef")
    assert cmd.task_id == "deadbeef"
    # The other half of the correct reading: the token is not an id, so the
    # whole line is the message rather than being silently cut at the space.
    assert cmd.arg == "1234abcd\n go with option 2"


def test_reply_without_a_message_is_an_error():
    err = parse_slash("/reply", selected_id="deadbeef")
    assert isinstance(err, SlashError)
    assert "message" in err.message.lower()


def test_unknown_slash_names_the_commands_that_do_exist():
    err = parse_slash("/merge", selected_id="deadbeef")
    assert isinstance(err, SlashError)
    assert "/merge" in err.message
    assert "/approve" in err.message


def test_help_and_quit_need_no_task():
    for name in ("help", "quit"):
        cmd = parse_slash(f"/{name}", selected_id=None)
        assert cmd.name == name


def test_help_text_lists_every_command():
    text = help_text()
    for name in SLASH_COMMANDS:
        assert f"/{name}" in text


# --------------------------------------------------------------------------- #
# Intake — the same grill the composer runs                                    #
# --------------------------------------------------------------------------- #

def test_a_first_message_becomes_the_title_and_the_description():
    s = IntakeSession.start("Add a retry button to the failed lane", repo_path="/repo")
    assert s.payload() == {
        "title": "Add a retry button to the failed lane",
        "description": "Add a retry button to the failed lane",
        "repo_path": "/repo",
        "qa_history": [],
    }


def test_a_long_first_message_titles_from_the_first_line_and_keeps_it_all():
    text = "Fix the review gate\n\nIt rejects everything because the parser and\nthe prompt disagree."
    s = IntakeSession.start(text, repo_path=None)
    assert s.payload()["title"] == "Fix the review gate"
    assert s.payload()["description"] == text


def test_a_very_long_single_line_title_is_truncated_but_the_body_is_not():
    text = "x" * 300
    s = IntakeSession.start(text, repo_path=None)
    assert len(s.payload()["title"]) <= 120
    assert s.payload()["description"] == text


def test_a_question_then_an_answer_builds_qa_history_the_server_expects():
    s = IntakeSession.start("Add retry", repo_path="/repo")
    s.take_question({"question": "Which lane?", "suggestions": ["failed"], "round": 1})
    assert s.pending_question == "Which lane?"
    s.take_answer("the failed lane")
    assert s.payload()["qa_history"] == [
        {"question": "Which lane?", "answer": "the failed lane"}]
    assert s.pending_question is None


def test_an_answer_with_no_question_pending_is_not_recorded_as_qa():
    s = IntakeSession.start("Add retry", repo_path="/repo")
    with pytest.raises(ValueError):
        s.take_answer("hello?")


def test_the_result_frame_becomes_the_create_task_payload():
    s = IntakeSession.start("Add retry", repo_path="/repo")
    s.take_result({
        "kind": "grill_result", "type": "done",
        "title": "Add a retry action to the Failed lane",
        "description": "Refined description",
        "acceptance_criteria": ["ac one", "ac two"],
    })
    assert s.result is not None
    assert s.task_payload() == {
        "title": "Add a retry action to the Failed lane",
        "description": "Refined description",
        "repo_path": "/repo",
        "acceptance_criteria": ["ac one", "ac two"],
    }


def test_task_payload_before_a_result_is_refused():
    s = IntakeSession.start("Add retry", repo_path="/repo")
    with pytest.raises(ValueError):
        s.task_payload()


def test_a_result_with_no_acceptance_criteria_still_creates_a_task():
    s = IntakeSession.start("Add retry", repo_path=None)
    s.take_result({"title": "T", "description": "D"})
    assert s.task_payload()["acceptance_criteria"] == []
