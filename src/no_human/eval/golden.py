"""Golden task set (PLAN.md 21.1) — frozen (input → known-good output) pairs.

Each golden task is a self-contained YAML file under ``eval/golden_tasks/``:

    id: add-mul
    title: "Add mul() to calc"
    description: "..."
    acceptance_criteria: ["mul(a,b) returns a*b"]
    setup:                       # files written into a fresh git repo @ base
      calc.py: "def add(a, b):\\n    return a + b\\n"
      test_calc.py: "..."
    solution:                    # the honest fixed-repo state (path -> content);
      calc.py: "..."             # a generic backend writes these to replay an
      test_calc.py: "..."        # honest run. Omit for `impossible` tasks.
    known_good_diff: |           # the reference diff (for the intent judge)
      ...
    held_out_tests: |            # run against the agent's result → mergeable?
      from calc import mul
      def test_mul(): assert mul(2, 3) == 6
    impossible: false            # red-team: correct behavior is to ESCALATE
    tempts_tamper: false         # red-team: a flaky/underspecified trap
    adjudication: "..."          # CURATOR-ONLY: why the red-team verdict is
                                 # what it is. NEVER shown to the agent under
                                 # test — see the leak note below.

The ``setup`` files are committed as the pre-fix base; the agent works from
there. Nothing here touches the live system — replay clones into a sandbox.

ADJUDICATION LEAK (fixed 2026-08-04, V3 corpus audit): the red-team specs used
to carry their own adjudication as a "(RED-TEAM: …)" parenthetical inside
``description`` — and ``replay.run_one`` passes ``description`` into the Task
the coder renders in its prompt. The trap descriptions literally told the coder
the honest fix ("the honest fix is to correct is_even()… adding a conftest.py
autouse fixture … is reward hacking"), so a pass demonstrated
instruction-following, not tamper-resistance, and every live-coder catch-rate
figure from the golden set was inflated. The adjudication now lives in this
dedicated field, which nothing hands to the agent; ``acceptance_criteria``
remain coder-visible on purpose (criteria are the legitimate task statement,
and for the impossible-* pair they still encode the impossibility — the field
move removes the leaked VERDICT, not the conflict).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

GOLDEN_DIR = Path(__file__).resolve().parents[3] / "eval" / "golden_tasks"


@dataclass
class GoldenTask:
    id: str
    title: str
    description: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    setup: dict[str, str] = field(default_factory=dict)
    solution: dict[str, str] = field(default_factory=dict)
    known_good_diff: str = ""
    held_out_tests: str = ""
    impossible: bool = False
    tempts_tamper: bool = False
    #: Curator-only: WHY the red-team verdict is what it is. Must never reach
    #: the agent under test (see the module docstring's leak note); consumed by
    #: humans and, potentially, a reason-aware scorer.
    adjudication: str = ""
    path: Path | None = None

    @property
    def is_red_team(self) -> bool:
        return self.impossible or self.tempts_tamper

    @staticmethod
    def from_dict(data: dict[str, Any], *, path: Path | None = None) -> "GoldenTask":
        return GoldenTask(
            id=data["id"],
            title=data["title"],
            description=data.get("description", ""),
            acceptance_criteria=list(data.get("acceptance_criteria", []) or []),
            setup=dict(data.get("setup", {}) or {}),
            solution=dict(data.get("solution", {}) or {}),
            known_good_diff=data.get("known_good_diff", ""),
            held_out_tests=data.get("held_out_tests", ""),
            impossible=bool(data.get("impossible", False)),
            tempts_tamper=bool(data.get("tempts_tamper", False)),
            adjudication=data.get("adjudication", "") or "",
            path=path,
        )


def load_golden_tasks(directory: Path = GOLDEN_DIR) -> list[GoldenTask]:
    """Load every ``*.yaml`` golden task from ``directory`` (sorted by id)."""
    directory = Path(directory)
    tasks: list[GoldenTask] = []
    if not directory.exists():
        return tasks
    for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if data:
            tasks.append(GoldenTask.from_dict(data, path=path))
    return sorted(tasks, key=lambda t: t.id)
