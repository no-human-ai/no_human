"""A job-level `env:` may only use contexts GitHub makes available there.

One unavailable context in a job-level `env:` does not fail that job — it
rejects the WHOLE workflow. GitHub creates ZERO jobs, names the run after the
file path instead of the workflow's `name:`, and reports only "This run likely
failed because of a workflow file issue". Every gate in the file stops running
and nothing says which one broke.

That happened here: `${{ runner.temp }}` was added to the windows and desktop
jobs' `env:` in 5f99b7af, and CI created no jobs at all for four days — 39
consecutive runs — while pull requests still showed a tick from the one
workflow that still parsed.

ALLOW-LIST on purpose. A deny-list of "not runner" would pass the next
unavailable context someone reaches for (`steps`, `job`, `env` itself); the
set below is the one GitHub documents for this position, so anything outside
it fails here rather than in production.

Scope, stated rather than implied: this checks job-level `env:` only. Other
job-level keys (`if:`, `concurrency:`, `runs-on:`) have their own availability
rules and are not covered.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))

#: https://docs.github.com/en/actions/learn-github-actions/contexts
#: — "Context availability", row `jobs.<job_id>.env`.
JOB_ENV_CONTEXTS = frozenset({
    "github", "inputs", "matrix", "needs", "secrets", "strategy", "vars",
})

_EXPR = re.compile(r"\$\{\{(.+?)\}\}", re.S)
_ROOT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


def _contexts(value: str) -> set[str]:
    """Root context names referenced by every `${{ }}` expression in *value*.

    The ROOT only: `runner.temp` and `runner.os` are both the `runner`
    context, and it is the root that availability is defined on.
    """
    found: set[str] = set()
    for expr in _EXPR.findall(str(value)):
        for token in re.split(r"[^A-Za-z0-9_.\-\[\]']+", expr):
            m = _ROOT.match(token)
            if m and "." in token or (m and token == m.group(0) and "(" not in token):
                root = m.group(0)
                if root not in {"true", "false", "null", "format", "join",
                                "contains", "startsWith", "endsWith", "toJSON",
                                "fromJSON", "hashFiles", "success", "always",
                                "cancelled", "failure", "not"}:
                    found.add(root)
    return found


def test_there_are_workflows_to_check():
    """Positive control: an empty glob would make every test below vacuous."""
    assert WORKFLOWS, "no workflow files found — the guard below would pass on nothing"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_job_level_env_uses_only_available_contexts(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    offenders = []
    for job_id, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for key, value in (job.get("env") or {}).items():
            for ctx in _contexts(value) - JOB_ENV_CONTEXTS:
                offenders.append(f"{path.name}: jobs.{job_id}.env.{key} uses '{ctx}'")
    assert not offenders, (
        "a context GitHub does not provide in a job-level env rejects the WHOLE "
        "workflow and creates zero jobs:\n  " + "\n  ".join(offenders)
        + f"\navailable there: {sorted(JOB_ENV_CONTEXTS)}"
    )
