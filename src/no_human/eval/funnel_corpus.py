"""The nightly funnel corpus (Phase C, task C2) — five tiers of rising size.

Layout mirrors `eval/golden_tasks/` in spirit (a tracked, wholly invented
fixture set with a loader beside it in `src/`) but per-DIRECTORY rather than
per-file, because a tier carries a repo tree rather than a dict of paths::

    eval/funnel_corpus/<tier>/
        ticket.json            title / description (exact line anchors) / ACs
        criteria.json          the tier's FunnelCriteria ceiling
        repo/                  the fixture's files, checked in as real files
        holdout/test_*.py      ONE held-out test, red at base (t2-t5)

Checked-in files plus an init step at load time, rather than a generator
script. The tree's other per-case fixture corpora already materialize a
checked-in `base/` tree the same way, and the git repo + local bare remote are
created by `materialize()` using the same three calls `eval.harness.run_shadow`
(harness.py:116-125) and `eval.northstar._setup_sandbox` use. The corpus is
readable in a diff that way — a generator hides the subject behind a program.

The subjects are invented toys (`tinytodo`). Nothing here derives from any real
employer content or from this project's own source; `tests/test_funnel_corpus.py`
asserts that, along with the two controls that make the corpus non-vacuous.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .funnel_criteria import FunnelCriteria

CORPUS_DIR = Path(__file__).resolve().parents[3] / "eval" / "funnel_corpus"

#: The tiers a real nightly run must contain, by NAME. Asserted rather than
#: counted: a count is satisfied by five copies of the cheapest tier, and the
#: failure this guards against — `eval/funnel_corpus/` present but emptied by a
#: bad merge or a partial checkout — reads as "0 passed, 0 failed, exit 0"
#: without it. Empty is not zero; a corpus that lost a tier measures less than
#: it claims to, and that is a broken instrument, not a clean night.
EXPECTED_TIERS = ("t1_docs_oneliner", "t2_small_fix", "t3_small_feature",
                  "t4_cross_file", "t5_test_first")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@dataclass
class CorpusTask:
    name: str
    ticket: dict
    repo_fixture: Path
    criteria: FunnelCriteria
    #: argv that runs this tier's held-out test, with cwd = the materialized
    #: repo. Empty for a tier with nothing to hold out (t1, prose only).
    holdout_cmd: list[str]

    def materialize(self, dest: Path) -> Path:
        """Build a throwaway git repo for this tier under *dest* and return it.

        origin is a bare repo inside *dest*, so the agent's branch push is
        push-proof by construction rather than by luck — the invariant
        `eval.northstar._setup_sandbox` states and then asserts.

        Re-runnable: a second nightly against the same home finds last night's
        tree and bare origin here and REPLACES both. Not `dirs_exist_ok=True` —
        merging night 1's residue into night 2's fixture contaminates the
        measurement. The bare origin goes too: it still carries last night's
        coder branch, so an agent that can `git fetch origin` a previous
        night's solution is not being measured on this night's work — and the
        base push below is rejected non-fast-forward against it anyway (the
        mutation that cleans only `work` fails the re-run test on exactly
        that). Deleting
        is safe because the work tree is disposable by design and
        `funnel_eval._assert_isolated` has already guaranteed the home is never
        the operator's ~/.no_human.
        """
        dest = Path(dest)
        work = dest / self.name
        bare = dest / f"{self.name}-remote.git"
        for stale in (work, bare):
            if stale.exists():
                shutil.rmtree(stale)
        shutil.copytree(self.repo_fixture, work)
        _git(work, "init", "-b", "main")
        _git(work, "config", "user.email", "corpus@example.invalid")
        _git(work, "config", "user.name", "funnel corpus")
        _git(work, "add", "-A")
        _git(work, "commit", "-m", f"{self.name}: base")
        subprocess.run(["git", "init", "--bare", str(bare)],
                       check=True, capture_output=True)
        _git(work, "remote", "add", "origin", str(bare))
        _git(work, "push", "-u", "origin", "HEAD")
        return work


def load_corpus(directory: Path = CORPUS_DIR) -> list[CorpusTask]:
    """Load every tier directory under *directory*, sorted by name."""
    directory = Path(directory)
    tasks: list[CorpusTask] = []
    for tier in sorted(p for p in directory.iterdir() if p.is_dir()):
        held = sorted((tier / "holdout").glob("test_*.py"))
        tasks.append(CorpusTask(
            name=tier.name,
            ticket=json.loads((tier / "ticket.json").read_text(encoding="utf-8")),
            repo_fixture=tier / "repo",
            criteria=FunnelCriteria.from_dict(
                json.loads((tier / "criteria.json").read_text(encoding="utf-8"))),
            holdout_cmd=([sys.executable, "-m", "pytest", "-q",
                          "-p", "no:cacheprovider", str(held[0])]
                         if held else []),
        ))
    return tasks


def corpus_ceiling_tokens(tasks: list[CorpusTask] | None = None) -> int:
    """The corpus's total weighted-token ceiling — what a whole nightly run can
    cost in the worst case, and the default for `eval.nightly_budget_tokens`."""
    return sum(t.criteria.max_weighted_tokens
               for t in (tasks if tasks is not None else load_corpus()))
