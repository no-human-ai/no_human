"""The review-gate workflow pair: `review-gate-recorder.yml` (untrusted,
`pull_request`, no secrets) and `review-gate.yml` (credentialed,
`workflow_run`, `environment: review-gate`).

Why two files. On a same-repository `pull_request` event GitHub Actions
checks out and runs the workflow YAML from the PULL REQUEST'S OWN HEAD, so
nothing written inside such a file is a boundary against whoever pushed that
branch — there is no version of a `pull_request` workflow that defends
against its own author. A `workflow_run` workflow always executes the YAML
on the repository's DEFAULT BRANCH, regardless of which branch or fork the
run it watches came from, so only that half may safely hold a credential.
See docs/design/untrusted-pr-review-gate.md sections A and G, and Actions run
35283704459, which measured naming `environment: review-gate` on a
`pull_request` job: the job failed in 2 seconds with an empty `steps` array,
annotation "Branch \"refs/pull/531/merge\" is not allowed to deploy to
review-gate due to environment protection rules."

This file pins the WIRING of both, by comparing each file's whole parsed
structure against an expected document rather than asserting a handful of
values — same lesson the single-file version of this guard already learned
once: `timeout-minutes: 036` is a YAML 1.1 octal that PyYAML reads as exactly
30, so the parsed structure still equals an EXPECTED that says `30`, and
every equality-only assertion stayed green over bytes that say `036`. That is
why `_check_yaml_scope` below is default-deny over the composed node graph,
keyed on (path, tag, raw text) rather than on a value or a bare spelling: a
value-keyed table lets `on:` and `true:` swap freely (both resolve to Python
`True`), which is precisely how an earlier version of this table let
`on: -> true:` through as `("bool", "true")` being merely "elsewhere allowed".

Nine mutations, applied one at a time to the real files and reverted, are
recorded in the PR body together with the command and the failing test name
for each. `test_each_mutation_turns_the_guard_red` below machine-checks the
same nine so the requirement survives the next edit to these files instead of
depending on a human re-running the sweep by hand:
  1. delete `environment: review-gate`
  2. change the pinned `uses:` to `./`
  3. delete the `if: ... conclusion == success` gate
  4. change `timeout-minutes: 30` to `036`
  5. delete `github_token`
  6. typo the secret name
  7. add `continue-on-error: true`
  8. change `runs-on` to `windows-latest`
  9. add a `run:` step to the recorder

`test_checkout_passes_the_two_arguments_run_py_exits_2_without`, from this
file's single-workflow predecessor, is deleted rather than adapted: the
reviewing job here checks out nothing at all (see review-gate.yml's own
comment on why — it fetches the pull request's diff as data through the
REST API, never the fork's or the branch's own head), so there is no
`actions/checkout` step left for that test to be about.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.repoguard

ROOT = Path(__file__).resolve().parent.parent
RECORDER_PATH = ROOT / ".github" / "workflows" / "review-gate-recorder.yml"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "review-gate.yml"
ACTION_PATH = ROOT / "action.yml"

#: Frozen so a change to the pin is a reviewed diff of this constant, not a
#: silent pass-through of whatever the file happens to say.
PINNED_ACTION = "no-human-ai/no_human@dbe45906ecc79fd22eb4ecb9f0679367d4e805a9"
PIN_PATTERN = r"no-human-ai/no_human@[0-9a-f]{40}"

#: The whole recorder, as it must parse. It holds no secret and executes no
#: repository code — one `run:` step that writes two GitHub-supplied scalars
#: through `env:` indirection (never `${{ }}` inside the `run:` body, which
#: is textual substitution) and one artifact upload.
EXPECTED_RECORDER = {
    "name": "no_human review gate recorder",
    True: {"pull_request": None},
    "permissions": {"contents": "read"},
    "jobs": {
        "record": {
            "name": "Record the pull request's identity",
            "runs-on": "ubuntu-latest",
            "timeout-minutes": 5,
            "steps": [
                {
                    "name": "Write pr.json",
                    "env": {
                        "PR_NUMBER": "${{ github.event.pull_request.number }}",
                        "PR_HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
                    },
                    "run": (
                        'jq -n --argjson number "$PR_NUMBER" --arg head_sha '
                        '"$PR_HEAD_SHA" \\\n'
                        "  '{number: $number, head_sha: $head_sha}' > pr.json\n"
                    ),
                },
                {
                    "name": "Upload it as `pr-context`",
                    "uses": "actions/upload-artifact@v4",
                    "with": {
                        "name": "pr-context",
                        "path": "pr.json",
                        "if-no-files-found": "error",
                        "retention-days": 1,
                    },
                },
            ],
        },
    },
}

#: The re-validation step's script, exactly. Frozen here rather than asserted
#: piecemeal so a change to it is a reviewed diff of this constant.
_REVALIDATE_SCRIPT = (
    "const fs = require('fs');\n"
    "const pr = JSON.parse(fs.readFileSync('pr.json', 'utf8'));\n"
    "if (!Number.isInteger(pr.number)) {\n"
    "  core.setFailed(`pr.json number is not an integer: "
    "${JSON.stringify(pr.number)}`);\n"
    "  return;\n"
    "}\n"
    "if (!/^[0-9a-f]{40}$/.test(pr.head_sha)) {\n"
    "  core.setFailed(`pr.json head_sha is not a 40-hex sha: "
    "${JSON.stringify(pr.head_sha)}`);\n"
    "  return;\n"
    "}\n"
    "const trustedHeadSha = context.payload.workflow_run.head_sha;\n"
    "if (pr.head_sha !== trustedHeadSha) {\n"
    "  core.setFailed(`pr.json head_sha ${pr.head_sha} does not match the "
    "triggering run's head_sha ${trustedHeadSha}`);\n"
    "  return;\n"
    "}\n"
    "const { data: pull } = await github.rest.pulls.get({\n"
    "  owner: context.repo.owner,\n"
    "  repo: context.repo.repo,\n"
    "  pull_number: pr.number,\n"
    "});\n"
    "if (pull.head.sha !== trustedHeadSha) {\n"
    "  core.setFailed(`PR #${pr.number}'s current head ${pull.head.sha} does "
    "not match the triggering run's head_sha ${trustedHeadSha}`);\n"
    "  return;\n"
    "}\n"
    "if (pull.state !== 'open') {\n"
    "  core.setFailed(`PR #${pr.number} is not open (state: ${pull.state})`);\n"
    "  return;\n"
    "}\n"
    "core.setOutput('pr_number', String(pr.number));\n"
    "core.setOutput('head_sha', trustedHeadSha);\n"
)

#: The whole reviewer, as it must parse. `True` keys the trigger block, same
#: idiom as the recorder above and as
#: tests/test_ci_release_dispatch_concurrency.py: PyYAML resolves the bare
#: key `on` to the YAML 1.1 boolean True.
EXPECTED_REVIEWER = {
    "name": "no_human review gate",
    True: {
        "workflow_run": {
            "workflows": ["no_human review gate recorder"],
            "types": ["completed"],
        },
    },
    "permissions": {
        "contents": "read",
        "pull-requests": "write",
        "actions": "read",
    },
    "concurrency": {
        "group": "review-gate-${{ github.event.workflow_run.head_branch }}",
        "cancel-in-progress": True,
    },
    "jobs": {
        "review": {
            "name": "Review the pull request",
            "if": "${{ github.event.workflow_run.conclusion == 'success' }}",
            "runs-on": "ubuntu-latest",
            "timeout-minutes": 30,
            "environment": "review-gate",
            "steps": [
                {
                    "name": "Download the recorded pull-request context",
                    "uses": (
                        "actions/download-artifact"
                        "@d3f86a106a0bac45b974a628896c90dbdf5c8093"
                    ),
                    "with": {
                        "name": "pr-context",
                        "run-id": "${{ github.event.workflow_run.id }}",
                        "github-token": "${{ github.token }}",
                    },
                },
                {
                    "name": "Re-validate pr.json against the API",
                    "id": "pr_context",
                    "uses": (
                        "actions/github-script"
                        "@f28e40c7f34bde8b3046d885e986cb6290c5673b"
                    ),
                    "with": {"script": _REVALIDATE_SCRIPT},
                },
                {
                    "name": "Review",
                    "uses": PINNED_ACTION,
                    "with": {
                        "credential": "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}",
                        "github_token": "${{ github.token }}",
                    },
                },
            ],
        },
    },
}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _review_step(loaded: dict) -> dict:
    return loaded["jobs"]["review"]["steps"][2]


# ---------------------------------------------------------------------------
# Shared `_check_*` helpers: raise AssertionError, take TEXT (never re-read a
# path themselves), so the real tests below and the parametrized mutation
# sweep at the bottom of this file exercise exactly one copy of the logic.
# ---------------------------------------------------------------------------


def _check_recorder_structure(text: str) -> None:
    assert yaml.safe_load(text) == EXPECTED_RECORDER


def _check_reviewer_structure(text: str) -> None:
    assert yaml.safe_load(text) == EXPECTED_REVIEWER


def _check_environment(text: str) -> None:
    job = yaml.safe_load(text)["jobs"]["review"]
    assert job.get("environment") == "review-gate"


def _check_pin(value: str) -> None:
    """Raise unless `value` is `owner/repo@<40-hex>` exactly — never `./`,
    never a mutable ref like `@main` or `@v1`."""
    assert re.fullmatch(PIN_PATTERN, value), f"not a pinned main commit sha: {value!r}"


def _check_pin_in_text(text: str) -> None:
    _check_pin(_review_step(yaml.safe_load(text))["uses"])


def _check_if_gate(text: str) -> None:
    job = yaml.safe_load(text)["jobs"]["review"]
    if_expr = job.get("if")
    assert if_expr is not None, "job carries no `if:` gate"
    assert "github.event.workflow_run.conclusion" in if_expr
    assert re.search(r"conclusion\s*==\s*'success'", if_expr), if_expr
    assert "!=" not in if_expr and "!(" not in if_expr.replace(" ", "")


def _check_github_token(text: str) -> None:
    step = _review_step(yaml.safe_load(text))
    assert step["with"].get("github_token") == "${{ github.token }}"


#: Every scalar in either file that is NOT a plain string, as
#: (path, tag, raw text on disk). Default-deny: a scalar whose tag is not
#: `str` and whose triple is not listed here is rejected, whatever it is —
#: see the module docstring for why this must be keyed on path as well as on
#: spelling, and on (tag, raw text) rather than on the parsed value.
_ALLOWED_RECORDER = {
    ("/on", "tag:yaml.org,2002:bool", "on"),
    ("/on/pull_request", "tag:yaml.org,2002:null", ""),
    ("/jobs/record/timeout-minutes", "tag:yaml.org,2002:int", "5"),
    ("/jobs/record/steps[1]/with/retention-days", "tag:yaml.org,2002:int", "1"),
}

_ALLOWED_REVIEWER = {
    ("/on", "tag:yaml.org,2002:bool", "on"),
    ("/concurrency/cancel-in-progress", "tag:yaml.org,2002:bool", "true"),
    ("/jobs/review/timeout-minutes", "tag:yaml.org,2002:int", "30"),
}

_STR_TAG = "tag:yaml.org,2002:str"


def _outside_githubs_yaml(node, allowed: set, path: str = "") -> list[str]:
    """Everything in the document outside the subset this file permits.

    Returns readable strings, so a failure names the construct and where it
    is rather than only that something is wrong.
    """
    problems: list[str] = []
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key, value in node.value:
            name = str(key.value)
            here = f"{path}/{name}"
            if name == "<<":
                # A merge key. `<<: *base` carries an anchor and the event-
                # stream test sees it; `<<: {a: 1}` carries none at all, and
                # PyYAML flattens it, so the parsed structure is identical
                # while the file's literal key is `<<` — which GitHub does
                # not resolve.
                problems.append(f"{here}: YAML merge key `<<`")
            if name in seen:
                # safe_load keeps the LAST value and says nothing, so the
                # structure compared against EXPECTED can differ from bytes.
                problems.append(f"{here}: duplicate key {name!r}")
            seen.add(name)
            problems.extend(_outside_githubs_yaml(key, allowed, here))
            problems.extend(_outside_githubs_yaml(value, allowed, here))
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            problems.extend(_outside_githubs_yaml(item, allowed, f"{path}[{index}]"))
    elif isinstance(node, yaml.ScalarNode):
        if node.tag != _STR_TAG and (path, node.tag, node.value) not in allowed:
            # PyYAML is YAML 1.1: it reads `yes`/`on` as booleans and a
            # leading zero as octal. GitHub is not. Wherever the two
            # disagree, EXPECTED is compared against PyYAML's reading rather
            # than the file's meaning — and the failure is silent, because
            # the structure matches.
            problems.append(
                f"{path}: {node.tag.rsplit(':', 1)[-1]} scalar written as "
                f"{node.value!r}, which PyYAML and GitHub need not read alike"
            )
    return problems


def _check_yaml_scope(text: str, allowed: set) -> None:
    problems = _outside_githubs_yaml(yaml.compose(text), allowed)
    assert problems == [], "; ".join(problems)


# ---------------------------------------------------------------------------
# Real tests.
# ---------------------------------------------------------------------------


def test_the_recorder_is_exactly_this():
    """Structural equality, so an edit nobody anticipated still has to be
    deliberate — including an extra `run:` step, which is a repository-code
    execution this file must never carry."""
    _check_recorder_structure(RECORDER_PATH.read_text(encoding="utf-8"))


def test_the_reviewer_is_exactly_this():
    """Structural equality for the credentialed half. A legitimate change
    updates EXPECTED_REVIEWER in the same diff, which is the point: this
    workflow holds a credential and decides a merge check."""
    _check_reviewer_structure(WORKFLOW_PATH.read_text(encoding="utf-8"))


def test_the_recorder_holds_no_secret_at_all():
    """The recorder runs as the pull request's own head, so it must have
    nothing worth stealing. Checked on RAW TEXT, not the parsed structure —
    a commented-out or oddly-quoted `secrets.` reference would still be a
    string an editor could uncomment, and `environment:` names a repository
    setting that only makes sense next to a credential."""
    text = RECORDER_PATH.read_text(encoding="utf-8")
    assert "secrets." not in text
    assert "environment:" not in text
    assert _load(RECORDER_PATH)[True] == {"pull_request": None}


def test_the_reviewing_job_names_the_credentialed_environment():
    """Actions run 35283704459 measured why this key cannot live on a
    `pull_request` job: naming `environment: review-gate` there failed in 2
    seconds with an empty `steps` array, annotation 'Branch
    "refs/pull/531/merge" is not allowed to deploy to review-gate due to
    environment protection rules.' The reviewing job runs on `workflow_run`
    (ref `refs/heads/main`), which the environment's `["main"]` branch policy
    admits."""
    _check_environment(WORKFLOW_PATH.read_text(encoding="utf-8"))


def test_the_action_is_pinned_to_a_main_commit_sha():
    """`uses: ./` would build the pull request's OWN Dockerfile.action inside
    a job holding the `review-gate` secret — arbitrary code execution with
    the credential in scope. `@main` is a mutable ref. Only a 40-hex commit
    sha on `no-human-ai/no_human` is accepted."""
    uses = _review_step(_load(WORKFLOW_PATH))["uses"]
    assert re.fullmatch(PIN_PATTERN, uses)
    assert uses == PINNED_ACTION


@pytest.mark.parametrize(
    "value",
    ["./", "no-human-ai/no_human@main", "no-human-ai/no_human@v1"],
)
def test_pin_guard_rejects_dot_slash_and_main(value):
    """Proves the regex REJECTS, not merely that today's value happens to
    pass it."""
    with pytest.raises(AssertionError):
        _check_pin(value)


def test_the_review_job_runs_only_after_a_successful_recorder_run():
    """`workflow_run.conclusion` takes ten values: success, failure,
    cancelled, skipped, timed_out, action_required, neutral, stale,
    startup_failure, and null. The `if:` here is an allow-list of exactly
    one (`== 'success'`), never a negated `!= 'failure'` form — a recorder
    cancelled mid-write must not start a credentialed job that then fails
    confusingly at the artifact-download step instead of not starting."""
    _check_if_gate(WORKFLOW_PATH.read_text(encoding="utf-8"))


def test_reviewer_watches_the_recorders_declared_name():
    """`workflows:` matches the recorder's own `name:` field, read from the
    recorder file rather than hardcoded a second time here — a rename of the
    recorder's `name:` with this list left stale would fail OPEN and silent,
    the reviewer simply never firing."""
    reviewer = _load(WORKFLOW_PATH)
    recorder = _load(RECORDER_PATH)
    trigger = reviewer[True]["workflow_run"]
    assert trigger["workflows"] == [recorder["name"]]
    assert trigger["types"] == ["completed"]


def test_the_workflows_stay_inside_the_yaml_these_files_permit():
    """Default-deny over the composed node graph, not a check per known-bad
    construct — each of those was added after an edit had already slipped
    past the previous one. This is the assertion that catches
    `timeout-minutes: 036`: PyYAML resolves the octal to the same Python int
    as `30`, so structural equality cannot see the edit, but its (tag, raw
    text) pair — `("tag:yaml.org,2002:int", "036")` — is not the one entry
    this table allows at `/jobs/review/timeout-minutes`."""
    _check_yaml_scope(RECORDER_PATH.read_text(encoding="utf-8"), _ALLOWED_RECORDER)
    _check_yaml_scope(WORKFLOW_PATH.read_text(encoding="utf-8"), _ALLOWED_REVIEWER)


def test_timeout_minutes_036_is_rejected_although_it_parses_to_30():
    """`036` is YAML 1.1 octal for exactly 30. `yaml.safe_load` reads
    `jobs.review.timeout-minutes` as the identical Python int either way, so
    equality-only assertions (including `test_the_reviewer_is_exactly_this`)
    would stay green over this edit. What catches it is `_check_yaml_scope`:
    the mutated scalar's (tag, raw text) pair is `("tag:yaml.org,2002:int",
    "036")`, which is not the `"30"` this table allows at
    `/jobs/review/timeout-minutes`, so it is rejected on sight and the
    AssertionError names that exact path.
    """
    mutated = WORKFLOW_PATH.read_text(encoding="utf-8").replace(
        "    timeout-minutes: 30\n", "    timeout-minutes: 036\n", 1
    )
    assert yaml.safe_load(mutated)["jobs"]["review"]["timeout-minutes"] == 30
    with pytest.raises(AssertionError, match=r"/jobs/review/timeout-minutes"):
        _check_yaml_scope(mutated, _ALLOWED_REVIEWER)


def test_no_yaml_anchors_or_aliases():
    """Anchors are invisible to the node walk, so they need the event
    stream. `compose` resolves an alias to the node it points at, so the
    composed graph cannot tell an alias from the value. Only four event
    types can carry an anchor and all four expose `.anchor`, which covers an
    anchor that is defined and never used as well as one that is aliased.
    Checked over BOTH files — GitHub Actions does not support anchors and
    would reject either outright."""
    for path in (RECORDER_PATH, WORKFLOW_PATH):
        events = list(yaml.parse(path.read_text(encoding="utf-8")))
        assert not [e for e in events if isinstance(e, yaml.AliasEvent)]
        assert not [
            e for e in events if getattr(e, "anchor", None) not in (None, "")
        ]


def test_github_token_is_passed_although_action_yml_calls_it_optional():
    """`required: false` on this input is not the whole story.

    `action.yml` declares `github_token` optional AND gives it no default,
    because the `github.token` value is not available where an input default
    is evaluated. `run.py` then treats an empty one as a misconfiguration and
    exits 2 — its only fallback is `GITHUB_TOKEN` in the environment, which a
    Docker action does not receive. So omitting it here is a red check, not a
    default, and that is easy to talk yourself into while reading action.yml.
    """
    declared = _load(ACTION_PATH)["inputs"]["github_token"]
    assert declared.get("required") is False
    assert "default" not in declared
    _check_github_token(WORKFLOW_PATH.read_text(encoding="utf-8"))


def test_every_with_key_is_an_input_action_yml_declares():
    """GitHub drops an undeclared `with:` key with no warning at all.

    A misspelled key does not fail the workflow; it reaches `run.py` as an
    absent input, and for `credential` that surfaces as the Action's own
    fail-closed exit 2 naming a secret the repository has correctly set. No
    amount of reading either file alone shows it — only reading them
    together. Checked only against the `Review` step: the other two steps in
    this job are third-party actions with their own `with:` contracts, not
    inputs of this repository's `action.yml`.
    """
    declared = set(_load(ACTION_PATH)["inputs"])
    for key in _review_step(_load(WORKFLOW_PATH))["with"]:
        assert key in declared, (
            f"`with: {key}:` is not an input declared in action.yml "
            f"({sorted(declared)}) — GitHub would drop it silently"
        )


def test_defaults_this_workflow_relies_on_are_still_action_ymls_defaults():
    """`credential_mode` and `fail_on_findings` are deliberately absent above.

    Absent means action.yml decides, so this asserts what it decides. If
    either default moves, the review gate on this repository silently
    changes behaviour — `auto` is what lets the secret rotate between an
    OAuth token and an API key untouched, and `"true"` is what makes a FAIL
    verdict red.
    """
    inputs = _load(ACTION_PATH)["inputs"]
    # Quoted in action.yml, so it is the STRING "true"; run.py parses the
    # input as text. Asserting a bool here would pin a value that is not
    # there.
    assert inputs["credential_mode"]["default"] == "auto"
    assert inputs["fail_on_findings"]["default"] == "true"


# ---------------------------------------------------------------------------
# The nine mutations, machine-checked. The MANUAL sweep — apply each to the
# real files, run the real command, record the command and the failing test
# name in the PR body, revert — is mandatory and separate from this: this
# parametrized test exercises the same nine in memory, against the shared
# `_check_*` helpers, so the requirement survives the next edit to these
# files instead of depending on a human re-running the sweep by hand.
# ---------------------------------------------------------------------------

_NINE_MUTATIONS = [
    pytest.param(
        WORKFLOW_PATH,
        "    environment: review-gate\n",
        "",
        _check_environment,
        id="1-delete-environment",
    ),
    pytest.param(
        WORKFLOW_PATH,
        "uses: no-human-ai/no_human@dbe45906ecc79fd22eb4ecb9f0679367d4e805a9",
        "uses: ./",
        _check_pin_in_text,
        id="2-uses-dot-slash",
    ),
    pytest.param(
        WORKFLOW_PATH,
        "    if: ${{ github.event.workflow_run.conclusion == 'success' }}\n",
        "",
        _check_if_gate,
        id="3-delete-if-gate",
    ),
    pytest.param(
        WORKFLOW_PATH,
        "    timeout-minutes: 30\n",
        "    timeout-minutes: 036\n",
        lambda text: _check_yaml_scope(text, _ALLOWED_REVIEWER),
        id="4-octal-timeout",
    ),
    pytest.param(
        WORKFLOW_PATH,
        "          github_token: ${{ github.token }}\n",
        "",
        _check_github_token,
        id="5-delete-github-token",
    ),
    pytest.param(
        WORKFLOW_PATH,
        "secrets.CLAUDE_CODE_OAUTH_TOKEN",
        "secrets.CLAUDE_CODE_OATH_TOKEN",
        _check_reviewer_structure,
        id="6-typo-secret-name",
    ),
    pytest.param(
        WORKFLOW_PATH,
        "        uses: no-human-ai/no_human@dbe45906ecc79fd22eb4ecb9f0679367d4e805a9\n",
        (
            "        uses: no-human-ai/no_human@dbe45906ecc79fd22eb4ecb9f0679367d4e805a9\n"
            "        continue-on-error: true\n"
        ),
        _check_reviewer_structure,
        id="7-continue-on-error",
    ),
    pytest.param(
        WORKFLOW_PATH,
        "    runs-on: ubuntu-latest\n",
        "    runs-on: windows-latest\n",
        _check_reviewer_structure,
        id="8-windows-latest",
    ),
    pytest.param(
        RECORDER_PATH,
        "      - name: Upload it as `pr-context`\n",
        (
            "      - name: Sneaky repository code\n"
            "        run: echo hi\n"
            "      - name: Upload it as `pr-context`\n"
        ),
        _check_recorder_structure,
        id="9-recorder-run-step",
    ),
]


@pytest.mark.parametrize("path, old, new, checker", _NINE_MUTATIONS)
def test_each_mutation_turns_the_guard_red(path, old, new, checker):
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"fixture drifted: {old!r} not found exactly once"
    mutated = text.replace(old, new, 1)
    with pytest.raises(AssertionError):
        checker(mutated)
