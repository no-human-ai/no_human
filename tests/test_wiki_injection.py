"""M-A: the locally-generated repo wiki (docs_gen, at .no_human/wiki/) is
provided to the agent as an INDEX-only reference and copied into the worktree.
No-op when the repo has no wiki, so the default path is unchanged."""


from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


def _orch(store, tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


def _seed_wiki(repo_dir, *, body="BODYTEXT"):
    wdir = repo_dir / ".no_human" / "wiki"
    wdir.mkdir(parents=True)
    (wdir / "architecture.md").write_text(f"# Architecture\n\n{body}")
    (wdir / "modules.md").write_text(f"# Modules\n\n{body}")


def test_no_injection_when_no_wiki(store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path=str(repo))
    assert orch._wiki_index_section(repo, t) is None


def test_injection_lists_present_pages_and_copies_in(store, tmp_path):
    canonical = tmp_path / "repo"
    canonical.mkdir()
    _seed_wiki(canonical)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path=str(canonical))

    section = orch._wiki_index_section(worktree, t)
    assert section is not None
    assert "Repo wiki (local, on-demand)" in section
    assert ".no_human/wiki/architecture.md" in section
    assert ".no_human/wiki/modules.md" in section
    # conventions.md was not generated → not listed.
    assert "conventions.md" not in section
    # INDEX-only: page bodies are never inlined.
    assert "BODYTEXT" not in section
    # Copied into the worktree (commit-excluded .no_human/).
    assert (worktree / ".no_human" / "wiki" / "architecture.md").is_file()


def test_injection_noop_when_canonical_equals_worktree(store, tmp_path):
    """Non-worktree runs (repo == canonical) must not self-copy or crash."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_wiki(repo)
    orch = _orch(store, tmp_path)
    t = Task.new("t", repo_path=str(repo))
    section = orch._wiki_index_section(repo, t)
    assert section is not None
    assert (repo / ".no_human" / "wiki" / "architecture.md").is_file()
