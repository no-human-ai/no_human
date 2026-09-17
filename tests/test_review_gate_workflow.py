"""`.github/workflows/review-gate.yml` — this repository running its own gate.

The Action's own behaviour is covered by `tests/test_ci_action.py`. What this
file pins is the WIRING, and it pins it by comparing the whole parsed workflow
against an expected structure rather than by asserting a handful of values.

That choice is the lesson of this file's first version, which asserted the
values that seemed load-bearing and let nine single-change edits through — each
of which breaks the gate for real while leaving every assertion green. Deleting
`github_token:` (no default in `action.yml`, so `run.py` exits 2). Misspelling
the SECRET's name rather than the input's (same empty credential, same exit 2,
error message blaming a secret that is correctly set). `runs-on: windows-latest`
(a Docker container action only runs on Linux). Putting `uses: ./` before the
checkout that has to materialise it. A job-level `permissions:` block, which
overrides the workflow-level one at runtime while the workflow-level one still
reads correctly. `if: false` or `continue-on-error: true` on the review step.
Dropping the `${{ }}` around the concurrency group, which turns a per-pull-
request group into one constant shared by all of them — so `cancel-in-progress`
starts cancelling OTHER pull requests' runs. Adding `dry_run: "true"`, a
perfectly well-declared input that means no comment is ever posted.

Enumerating assertions loses that race by construction: the mutation you did
not think of is the one that ships. Equality does not have to think of it.

Two things equality cannot see, so they are separate tests: whether a key in
the workflow is an input `action.yml` actually declares (GitHub drops an
undeclared `with:` key silently, and it arrives at `run.py` as an absent
input), and whether `action.yml`'s own defaults still are what leaving them
unset here assumes.

And one thing equality cannot see because its ORACLE cannot: `yaml.safe_load`
is not GitHub's parser, so an edit the two read differently is invisible to a
comparison of what safe_load returned. Two were demonstrated. A YAML anchor
and alias (`name: &gate …` / `name: *gate`) parses to an identical structure
here while GitHub Actions, which does not support anchors, rejects the file
outright. A duplicated `permissions:` block parses to the LAST one under
safe_load — silently, no warning — so the test can read `contents: read` from
a file whose first block says `contents: write`. Both are read through the
lower-level APIs that still see them: the event stream for aliases, the node
graph for duplicate keys.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "review-gate.yml"
ACTION_PATH = ROOT / "action.yml"

#: The whole workflow, as it must parse. Every value here is load-bearing; the
#: named tests below explain the ones whose reason is not obvious from reading.
#: PyYAML resolves the bare key `on` to the boolean True (YAML 1.1), which is
#: why the trigger block appears under `True` and not under "on" — same idiom as
#: tests/test_ci_release_dispatch_concurrency.py.
EXPECTED = {
    "name": "no_human review gate",
    True: {"pull_request": None},
    "permissions": {"contents": "read", "pull-requests": "write"},
    "concurrency": {
        "group": "review-gate-${{ github.event.pull_request.number }}",
        "cancel-in-progress": True,
    },
    "jobs": {
        "review": {
            "name": "no_human review gate",
            "runs-on": "ubuntu-latest",
            # The credential is an environment secret of `review-gate`; a job
            # that does not name its environment is handed an empty string and
            # the Action fails closed. Deleting this key is the single-change
            # mutation that reproduced run 35272950327's
            # "the `credential` input is empty".
            "environment": "review-gate",
            "timeout-minutes": 30,
            "steps": [
                {
                    "uses": "actions/checkout@v4",
                    "with": {
                        "fetch-depth": 0,
                        "ref": "${{ github.event.pull_request.head.sha }}",
                    },
                },
                {
                    "uses": "./",
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


def _review_step() -> dict:
    return _load(WORKFLOW_PATH)["jobs"]["review"]["steps"][1]


def test_the_workflow_is_exactly_this():
    """Structural equality, so an edit nobody anticipated still has to be
    deliberate. A legitimate change updates EXPECTED in the same diff, which is
    the point: the workflow holds a credential and decides a merge check."""
    assert _load(WORKFLOW_PATH) == EXPECTED


def test_checkout_passes_the_two_arguments_run_py_exits_2_without():
    """Why those two `actions/checkout` values are not style.

    `run.py` compares the workspace's HEAD to `pull_request.head.sha` and
    refuses to review a tree that is not the pull request's head, because it
    cites file:line against what is on disk — and on a `pull_request` event
    `actions/checkout` otherwise leaves the ephemeral MERGE commit there.
    `fetch-depth: 0` is what lets it resolve `merge-base <base> <head>`; the
    default depth-1 checkout cannot.
    """
    with_block = _load(WORKFLOW_PATH)["jobs"]["review"]["steps"][0]["with"]
    assert with_block["ref"] == "${{ github.event.pull_request.head.sha }}"
    # 0, not "0": `actions/checkout` reads the string, but a quoted value here
    # would mean someone retyped the line rather than left it alone.
    assert with_block["fetch-depth"] == 0


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
    assert _review_step()["with"]["github_token"] == "${{ github.token }}"


def test_every_with_key_is_an_input_action_yml_declares():
    """GitHub drops an undeclared `with:` key with no warning at all.

    A misspelled key does not fail the workflow; it reaches `run.py` as an
    absent input, and for `credential` that surfaces as the Action's own
    fail-closed exit 2 naming a secret the repository has correctly set. No
    amount of reading either file alone shows it — only reading them together.
    """
    declared = set(_load(ACTION_PATH)["inputs"])
    for key in _review_step()["with"]:
        assert key in declared, (
            f"`with: {key}:` is not an input declared in action.yml "
            f"({sorted(declared)}) — GitHub would drop it silently"
        )


def test_defaults_this_workflow_relies_on_are_still_action_ymls_defaults():
    """`credential_mode` and `fail_on_findings` are deliberately absent above.

    Absent means action.yml decides, so this asserts what it decides. If either
    default moves, the review gate on this repository silently changes
    behaviour — `auto` is what lets the secret rotate between an OAuth token
    and an API key untouched, and `"true"` is what makes a FAIL verdict red.
    """
    inputs = _load(ACTION_PATH)["inputs"]
    # Quoted in action.yml, so it is the STRING "true"; run.py parses the input
    # as text. Asserting a bool here would pin a value that is not there.
    assert inputs["credential_mode"]["default"] == "auto"
    assert inputs["fail_on_findings"]["default"] == "true"


#: Every scalar in this workflow that is NOT a plain string, as (tag, the raw
#: text on disk). Default-deny: a scalar whose tag is not `str` and whose pair
#: is not listed is rejected, whatever it is. That inversion is the point.
#:
#: The earlier version of this guard reported three named constructs — merge
#: keys, duplicate keys, boolean spellings — and permitted everything else. It
#: was called an allowlist and was not one, which a single line disproved:
#: `timeout-minutes: 036` is a YAML 1.1 octal, PyYAML reads it as exactly 30,
#: the structure still equals EXPECTED, the tag is `int` so the boolean check
#: never looks at it, and every test stayed green over bytes that say `036`.
#: Numeric coercion was simply the next class after boolean coercion, and a
#: guard that has to be taught each class in turn will always be one class
#: behind. Listing what is allowed cannot be, because the coercion nobody
#: thought of is not on the list — hex (`0x1e`) and float (`0.0`, which Python's
#: `0.0 == 0` slides past dict equality) were both caught here without anyone
#: having named them.
#:
#: TWO LIMITS, recorded rather than left to be rediscovered.
#:
#: Entries are keyed by PATH as well as by spelling, and the earlier version
#: keyed only on spelling. That was not a stylistic choice, it was wrong, and a
#: one-line edit proved it: `on:` -> `true:` destroys the workflow's trigger key
#: and left all seven tests green. Both `("bool","on")` and `("bool","true")`
#: were admitted anywhere, both resolve to Python True, and EXPECTED holds True
#: in exactly two slots — the trigger key and `cancel-in-progress` — so the two
#: spellings were freely swappable and whole-document equality could not see the
#: swap. The reciprocal, `cancel-in-progress: on`, was green too.
#:
#: The comment that used to sit here argued the opposite: that constraining
#: spelling without position was safe "because EXPECTED is a full-document ==,
#: so an allowed pair cannot be somewhere it does not belong." That argument is
#: false whenever two allowed pairs resolve to the SAME Python value, which is
#: exactly the case here. Equality compares resolved values; it cannot
#: distinguish two spellings of True. Position has to come from this table.
#:
#: Still blind to an EXPLICIT TAG: `!!int 30` composes to the same
#: (tag, raw text) pair as `30`, so the pair check cannot tell them apart.
#: Whether GitHub rejects tags is unverified, so it is recorded rather than
#: guessed at. A `%YAML 1.1` directive is in the same family and also unseen.
_ALLOWED_NON_STRING_SCALARS = {
    # GitHub's trigger key is spelled `on`, which YAML 1.1 reads as boolean
    # True — the one coercion that is mandatory rather than a mistake, and the
    # reason EXPECTED is keyed on True. Admitted at the document root and
    # NOWHERE else, so `true:` at the root is now rejected.
    ("/on", "tag:yaml.org,2002:bool", "on"),
    # `pull_request:` with nothing under it: the trigger takes no filters.
    ("/on/pull_request", "tag:yaml.org,2002:null", ""),
    # The cost bound. `yes`/`on` here are rejected: a reader that treats them
    # as strings would silently stop cancelling superseded runs.
    ("/concurrency/cancel-in-progress", "tag:yaml.org,2002:bool", "true"),
    ("/jobs/review/timeout-minutes", "tag:yaml.org,2002:int", "30"),
    ("/jobs/review/steps[0]/with/fetch-depth", "tag:yaml.org,2002:int", "0"),
}

_STR_TAG = "tag:yaml.org,2002:str"


def _outside_githubs_yaml(node, path: str = "") -> list[str]:
    """Everything in the document outside the subset this file permits.

    Returns readable strings, so a failure names the construct and where it is
    rather than only that something is wrong.
    """
    problems: list[str] = []
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key, value in node.value:
            name = str(key.value)
            here = f"{path}/{name}"
            if name == "<<":
                # A merge key. `<<: *base` carries an anchor and the event-stream
                # test sees it; `<<: {a: 1}` carries none at all, and PyYAML
                # flattens it, so the parsed structure is identical while the
                # file's literal key is `<<` — which GitHub does not resolve.
                problems.append(f"{here}: YAML merge key `<<`")
            if name in seen:
                # safe_load keeps the LAST value and says nothing, so the
                # structure compared against EXPECTED can differ from the bytes.
                problems.append(f"{here}: duplicate key {name!r}")
            seen.add(name)
            problems.extend(_outside_githubs_yaml(key, here))
            problems.extend(_outside_githubs_yaml(value, here))
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            problems.extend(_outside_githubs_yaml(item, f"{path}[{index}]"))
    elif isinstance(node, yaml.ScalarNode):
        if (node.tag != _STR_TAG
                and (path, node.tag, node.value) not in _ALLOWED_NON_STRING_SCALARS):
            # PyYAML is YAML 1.1: it reads `yes`/`on` as booleans and a leading
            # zero as octal. GitHub is not. Wherever the two disagree, EXPECTED
            # is compared against PyYAML's reading rather than the file's
            # meaning — and the failure is silent, because the structure matches.
            problems.append(
                f"{path}: {node.tag.rsplit(':', 1)[-1]} scalar written as "
                f"{node.value!r}, which PyYAML and GitHub need not read alike")
    return problems


def test_the_workflow_stays_inside_the_yaml_this_file_permits():
    """Default-deny over the composed node graph, not a check per known-bad
    construct — each of those was added after an edit had already slipped
    past the previous one."""
    raw = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert _outside_githubs_yaml(yaml.compose(raw)) == []


def test_no_yaml_anchors_or_aliases():
    """Anchors are invisible to the node walk, so they need the event stream.

    `compose` resolves an alias to the node it points at, so the composed graph
    cannot tell an alias from the value. Only four event types can carry an
    anchor and all four expose `.anchor`, which covers an anchor that is
    defined and never used as well as one that is aliased.
    """
    events = list(yaml.parse(WORKFLOW_PATH.read_text(encoding="utf-8")))
    assert not [e for e in events if isinstance(e, yaml.AliasEvent)]
    assert not [e for e in events if getattr(e, "anchor", None) not in (None, "")]
