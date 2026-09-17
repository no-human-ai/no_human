"""A `workflow_dispatch` release build must not be cancelled by the next push.

`concurrency.group: ci-${{ github.workflow }}-${{ github.ref }}` puts a
`workflow_dispatch` run on `refs/heads/main` (e.g. `-f windows_release=true`)
in the SAME group as an ordinary push to `main`, under
`cancel-in-progress: true`. Whichever starts last cancels the other.

Measured twice: run 35120284385 (windows_release on main, head b4663e4a) was
cancelled mid-NSIS-package when commit fbbc0817 landed and the push run took
the group -- Verify artefact / Checksums / Upload all skipped, no installer
produced. Earlier, faa1370c's own CI was cancelled by the next push, which is
why it landed with no green verdict.

The fix appends a constant `-dispatch` discriminant to the group only for
`workflow_dispatch` runs, so a dispatch and a push on the same ref resolve to
different groups while push/pull_request keep the plain ref-based group
(so a new push still supersedes a stale one there) and two dispatches on the
same ref still serialize with each other.

These tests *render* the `concurrency.group` expression under a small,
deliberately narrow GitHub-Actions-expression evaluator -- scoped to exactly
the constructs this one expression uses -- rather than string-matching the
YAML, so a future rewrite that is semantically equivalent still passes and a
semantically broken one fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOW_PATH = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def _workflow():
    with open(WORKFLOW_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _on_block(workflow: dict) -> dict:
    # PyYAML 1.1-style parses the bare scalar key `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


_EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}")
_STRING_RE = re.compile(r"^'((?:[^']|'')*)'\s*(.*)$", re.DOTALL)
_TRUTHY_FALSE = (None, False, "", 0)


def _truthy(value) -> bool:
    return value not in _TRUTHY_FALSE


def _eval(inner: str, ctx: dict):
    """Evaluate one `${{ ... }}` expression body.

    Supports exactly what `concurrency.group` in ci.yml uses: dotted context
    lookups, single-quoted string literals (`''` as the escaped quote),
    `==`/`!=`, and left-associative `&&`/`||` with GHA truthiness, returning
    the operand VALUE (not a bool) like the real engine does. `&&` binds
    tighter than `||`. Anything else raises, so an unhandled construct fails
    loudly instead of silently rendering empty.
    """
    text = inner.strip()

    def parse_or(s: str):
        value, s = parse_and(s)
        s = s.strip()
        while s.startswith("||"):
            rhs_val, s = parse_and(s[2:].strip())
            value = value if _truthy(value) else rhs_val
            s = s.strip()
        return value, s

    def parse_and(s: str):
        value, s = parse_cmp(s)
        s = s.strip()
        while s.startswith("&&"):
            rhs_val, s = parse_cmp(s[2:].strip())
            value = rhs_val if _truthy(value) else value
            s = s.strip()
        return value, s

    def parse_cmp(s: str):
        value, s = parse_atom(s)
        s = s.strip()
        for op in ("==", "!="):
            if s.startswith(op):
                rhs_val, s = parse_atom(s[len(op):].strip())
                value = (value == rhs_val) if op == "==" else (value != rhs_val)
                s = s.strip()
                break
        return value, s

    def parse_atom(s: str):
        s = s.strip()
        if s.startswith("'"):
            m = _STRING_RE.match(s)
            if not m:
                raise ValueError(f"unterminated string literal in: {s!r}")
            literal = m.group(1).replace("''", "'")
            return literal, m.group(2)
        m = re.match(r"[A-Za-z_][A-Za-z0-9_.]*", s)
        if not m:
            raise ValueError(f"unrecognised expression atom: {s!r}")
        path = m.group(0)
        rest = s[m.end():]
        node = ctx
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                raise ValueError(f"unbound context path {path!r} (ctx={ctx!r})")
            node = node[part]
        return node, rest

    value, remainder = parse_or(text)
    if remainder.strip():
        raise ValueError(f"unconsumed expression trailer: {remainder!r} in {inner!r}")
    return value


def _render(expr: str, ctx: dict) -> str:
    def sub(match: "re.Match[str]") -> str:
        return str(_eval(match.group(1), ctx))

    return _EXPR_RE.sub(sub, expr)


def _group(event_name: str, ref: str, *, run_id: str = "1") -> str:
    workflow = _workflow()
    raw_group = workflow["concurrency"]["group"]
    ctx = {
        "github": {
            "workflow": workflow["name"],
            "ref": ref,
            "event_name": event_name,
            "run_id": run_id,
        }
    }
    return _render(raw_group, ctx)


def _raw_group_expr() -> str:
    return _workflow()["concurrency"]["group"]


def test_a_dispatch_and_a_push_on_main_do_not_share_a_group():
    dispatch_group = _group("workflow_dispatch", "refs/heads/main")
    push_group = _group("push", "refs/heads/main")
    assert dispatch_group and push_group
    assert dispatch_group != push_group
    # Guards against an accidental rename orphaning in-flight push/PR runs.
    assert push_group == "ci-CI-refs/heads/main"


def test_the_dispatch_discriminant_is_a_constant_not_the_run_id():
    first = _group("workflow_dispatch", "refs/heads/main", run_id="111")
    second = _group("workflow_dispatch", "refs/heads/main", run_id="222")
    assert first == second
    assert "run_id" not in _raw_group_expr()


def test_two_dispatches_on_the_same_ref_share_a_group_and_different_refs_do_not():
    for event in ("push", "workflow_dispatch"):
        same_a = _group(event, "refs/heads/main")
        same_b = _group(event, "refs/heads/main")
        assert same_a == same_b

        different = _group(event, "refs/heads/feature-x")
        assert same_a != different


def test_push_and_pull_request_keep_the_plain_ref_group():
    push_group = _group("push", "refs/heads/main")
    pr_group = _group("pull_request", "refs/pull/42/merge")
    assert "dispatch" not in push_group
    assert "dispatch" not in pr_group

    # A second push to the same ref must still land in the same group, so
    # `cancel-in-progress` continues to supersede the stale run.
    assert _group("push", "refs/heads/main") == push_group


def test_cancel_in_progress_is_still_on():
    assert _workflow()["concurrency"]["cancel-in-progress"] is True


def test_the_group_is_workflow_level_not_per_job():
    workflow = _workflow()
    assert "concurrency" in workflow
    for job_name, spec in workflow["jobs"].items():
        assert "concurrency" not in spec, (
            f"job {job_name!r} defines its own `concurrency`, which would "
            "half-apply the workflow-level fix"
        )


def test_every_release_input_is_covered_by_the_dispatch_group():
    raw = _raw_group_expr()
    assert "github.event_name == 'workflow_dispatch'" in raw
    inputs = _on_block(_workflow())["workflow_dispatch"]["inputs"]
    release_inputs = [name for name in inputs if name.endswith("_release")]
    assert set(release_inputs) == {"linux_release", "wheel_release", "windows_release"}
    # The group keys on the EVENT, not on any single input, so all of them
    # ride the same dispatch group regardless of which `*_release` flag is set.
    for name in release_inputs:
        assert f"inputs.{name}" not in raw, (
            f"group expression should not special-case {name!r}; it should "
            "key on github.event_name alone"
        )
