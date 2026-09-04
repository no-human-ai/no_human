"""P6: the target repo's own agent-instruction files are surfaced to the agent
(via the compact-instructions section that is actually read at runtime)."""


from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.notify.slack import SlackNotifier


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


def _orch(store, tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


async def test_discovers_and_orders_instruction_files(store, tmp_path):
    repo = tmp_path / "repo"; (repo / ".github").mkdir(parents=True)
    (repo / "CLAUDE.md").write_text("Use tabs, not spaces.")
    (repo / "AGENTS.md").write_text("Run `make check` before committing.")
    (repo / ".github" / "copilot-instructions.md").write_text("Prefer composition.")  # term-ok: real file convention
    orch = _orch(store, tmp_path)

    section = orch._repo_instruction_section(repo)
    assert section is not None
    assert "AUTHORITATIVE" in section
    assert "Use tabs, not spaces." in section
    assert "Run `make check` before committing." in section
    assert "Prefer composition." in section
    # CLAUDE.md has highest precedence → appears before AGENTS.md.
    assert section.index("CLAUDE.md") < section.index("AGENTS.md")


async def test_no_instruction_files_returns_none(store, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    orch = _orch(store, tmp_path)
    assert orch._repo_instruction_section(repo) is None


async def test_large_file_is_truncated(store, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "AGENTS.md").write_text("x" * (orch_max := 3000 + 500))
    orch = _orch(store, tmp_path)
    section = orch._repo_instruction_section(repo)
    assert "… (truncated)" in section
    assert len(section) < orch_max + 500  # capped, not the full 3500


async def test_included_in_compact_instructions(store, tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "AGENTS.md").write_text("PROJECT_MARKER_CONVENTION")
    orch = _orch(store, tmp_path)
    from no_human.core.task import Task
    t = Task.new("do a thing", repo_path=str(repo))
    orch._materialize_compact_instructions(repo, t)
    content = (repo / ".claude" / "instructions.md").read_text()
    assert "PROJECT_MARKER_CONVENTION" in content
    # Repo conventions come before the generic standing rules.
    assert content.index("PROJECT_MARKER_CONVENTION") < content.index("Standing rules")


# --- the PLANNER's copy: advisory, capped, bounded, and reported --------------
#
# Everything below existed as behaviour before it existed as a test. An
# independent review found the aggregate cap, the bounded read and the audit
# emit all unasserted — in the commit whose stated purpose was answering an
# earlier review about untested claims. That is the failure these close.


async def test_planning_conventions_are_advisory_not_authoritative(store, tmp_path):
    """The planner's copy must NOT carry the coder's authority header.

    The coder is told its repo's files are "AUTHORITATIVE ... follow these over
    generic guidance". Giving the planner that ranks repo-authored text above
    the planner's own directives, and the plan feeds `declared_files`, which the
    coder's scope guard reads.

    SCOPE, stated because the honest limit matters: this pins the header WE
    write. It cannot stop a repo from putting the word AUTHORITATIVE in its own
    file — that text is interpolated verbatim. This asserts our framing, not
    immunity.
    """
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "AGENTS.md").write_text("use tabs")
    orch = _orch(store, tmp_path)
    section, _ = orch._planning_conventions_section(repo)
    assert "advisory" in section.lower()
    assert "AUTHORITATIVE" not in section
    assert "planning instructions below" in section


async def test_planning_conventions_are_capped_in_aggregate(store, tmp_path):
    """Per-file caps do not bound the total: five files at 3,000 each is 15,000,
    and the MoA path pays it once per proposer."""
    repo = tmp_path / "repo"; (repo / ".github").mkdir(parents=True)
    # Named from the orchestrator's OWN list, not retyped: a literal here both
    # cites a document the export drops and stops tracking the real set the
    # day a sixth filename is recognised.
    for name in Orchestrator._REPO_INSTRUCTION_FILES[:3]:
        (repo / name).write_text("x" * 20_000)
    orch = _orch(store, tmp_path)
    section, meta = orch._planning_conventions_section(repo)
    assert meta["chars"] <= orch._PLANNING_CONVENTIONS_TOTAL_CAP, meta
    # And the coder's uncapped path on the SAME repo is bigger — the two readers
    # differ on purpose, so prove the difference rather than assuming it.
    coder = orch._repo_instruction_section(repo)
    assert len(coder) > len(section)


async def test_truncation_is_marked_in_the_text_and_reported_honestly(store, tmp_path):
    """A leading newline used to make a truncated file report `truncated=False`.

    `read(cap + 1)` followed by `.strip()` shortens the sample below the cap, so
    the length test said "nothing was cut" while the rest of the file had
    already been discarded. One newline was enough to lose 17,000 characters
    with no signal. The check now reads the RAW length, before stripping.

    And the model must be told: a fragment ending mid-word is read as the whole
    convention unless it is marked.
    """
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "AGENTS.md").write_text("\n" + "y" * 20_000)   # leading whitespace
    orch = _orch(store, tmp_path)
    section, meta = orch._planning_conventions_section(repo)
    assert meta["truncated"] is True, "a stripped-then-measured file hid its own truncation"
    assert "… (truncated)" in section, "the model cannot tell the file continued"


async def test_no_truncation_flag_when_nothing_was_cut(store, tmp_path):
    """The flag must not fire for free. It previously went true whenever the
    budget happened to land on zero, even with no further file to drop —
    a warning about nothing, which teaches readers to ignore the warning."""
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "AGENTS.md").write_text("short and complete")
    orch = _orch(store, tmp_path)
    _, meta = orch._planning_conventions_section(repo)
    assert meta["truncated"] is False, meta
    assert meta["dropped"] == [], meta


async def test_files_dropped_by_the_cap_are_named(store, tmp_path):
    """A file the planner never saw is exactly what an approver needs told.

    The loop used to `break` at the cap, so later files vanished with no record
    — `.cursorrules` saying "never touch prod" could be absent from the plan's
    context and absent from the log, and nobody could ask about a file they were
    never told existed.
    """
    repo = tmp_path / "repo"; repo.mkdir()
    # Two files at the 3,000 per-file cap exhaust the 4,000 aggregate budget.
    # ONE 20,000-char file does NOT: it truncates to 3,000 and leaves 1,000, so
    # a small third file still fits — which is what this test got wrong first.
    _first, _second, _third = Orchestrator._REPO_INSTRUCTION_FILES[:3]
    (repo / _first).write_text("z" * 20_000)
    (repo / _second).write_text("w" * 20_000)
    (repo / _third).write_text("never touch prod")   # must be NAMED
    orch = _orch(store, tmp_path)
    section, meta = orch._planning_conventions_section(repo)
    assert _third in meta["dropped"], meta
    assert "never touch prod" not in section
