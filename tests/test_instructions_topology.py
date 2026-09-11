"""`.claude/instructions.md` must not describe the machine it runs on.

The compact instructions file is read by the SDK on every task and survives
context compaction. It used to include `discover_local_repos()` — a scan of
`~/git` to depth 2 — which wrote up to 15 lines of
`- <name>: /Users/<user>/git/<name>` into every task, regardless of what the
task was about.

Measured on the maintainer's machine 2026-08-01: 19 repositories, absolute home
paths, and 8 vendor/employer terms. `.claude/**` is `_EPHEMERAL`, so the file
never reaches a PR diff — but the CODER READS IT, which is exactly the threat
model the learned-memory term screen was built for, at an order of magnitude
more exposure and with no screen at all.

Found by an independent reviewer while auditing the memory screen's read path,
by driving the builder with an EMPTY memory store — which is what proved it was
not the learning path.

Asserted on the RENDERED TEXT rather than on `discover_local_repos()`, because
the defect is what reaches the coder. A test of the discovery function would
still pass with the leak fully intact.
"""

from __future__ import annotations

import re

import pytest

from no_human.core.task import Task

_HOME_PATH = re.compile(r"/(?:Users|home)/[^/\s]+/")


@pytest.fixture
def written(tmp_path):
    """Render instructions.md for a task and hand back its text."""
    from no_human.core.orchestrator import Orchestrator

    def _render(task: Task):
        o = Orchestrator.__new__(Orchestrator)
        # Only what the instructions builder reads. A bare instance keeps the
        # test honest about which state this path actually depends on.
        o.config = {}
        o.store = None
        o._active_memories = []
        o._discovered_skills_info = []
        o._active_profile = None
        o._materialize_compact_instructions(tmp_path, task)
        path = tmp_path / ".claude" / "instructions.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    return _render


def test_it_does_not_list_repos_the_task_never_mentions(written, tmp_path):
    """The regression itself.

    The machine running this suite has other repositories on it; none of them
    belong in the instructions for a task about one repo.
    """
    task = Task.new("fix the parser", repo_path=str(tmp_path / "only-repo"))
    text = written(task)
    assert "Known local repos" not in text, (
        "the whole-machine repo scan is back in the instructions")

    homes = {m.group(0) for m in _HOME_PATH.finditer(text)}
    unrelated = {h for h in homes if str(tmp_path) not in h}
    assert not unrelated, (
        f"instructions.md names home directories unrelated to the task: {unrelated}")


def test_a_single_repo_task_gets_no_repo_section_at_all(written, tmp_path):
    """Telling a coder the path of the repo it is already standing in is noise,
    and noise in a file that survives compaction is expensive."""
    task = Task.new("fix the parser", repo_path=str(tmp_path / "solo"))
    assert "Repos in this task" not in written(task)


def test_a_multi_repo_task_still_gets_its_siblings(written, tmp_path):
    """The control, and the reason this is a scope change and not a deletion.

    A task that declares linked repos needs to find them; a path the task itself
    names is not a disclosure. Without this the fix would be a regression for
    the case the section exists to serve.
    """
    task = Task.new("sync the schema", repo_path=str(tmp_path / "api"))
    task.linked_repos = [str(tmp_path / "worker"), str(tmp_path / "shared")]
    text = written(task)
    assert "Repos in this task" in text
    for name in ("api", "worker", "shared"):
        assert name in text, f"the task's own repo {name!r} is missing"


def test_the_rendered_file_carries_no_vendor_term_from_the_machine(written, tmp_path):
    """The reviewer's finding, reproduced as an assertion.

    Driving the builder with an EMPTY memory store previously produced six
    vendor/employer terms, every one of them from the repo scan and none from
    the store. An empty store is what makes this unambiguous: anything found
    here came from the machine, not the learning path.

    The six are deliberately NOT spelled out. An earlier draft of this docstring
    listed them, and the export gate refused to approve the file — correctly,
    since a test about not leaking terms is a poor place to write them down. The
    assertion reads the product's own list at runtime, which is stronger anyway:
    it cannot go stale when the list changes.
    """
    from no_human.eval.vendor_terms import find_banned_terms

    task = Task.new("fix the parser", repo_path=str(tmp_path / "only-repo"))
    hits = sorted(set(find_banned_terms(written(task))))
    assert not hits, (
        f"instructions.md carries terms from the machine it runs on: {hits}")
