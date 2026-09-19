# Reviewing a PR without trusting the branch its workflow came from

Design record. Nothing in this document ships code — the follow-up that adds
a `workflow_run` branch to `src/no_human/ci_action/run.py` and a
`review-gate.yml` workflow lands separately, once this design is reviewed.
`src/no_human/ci_action/run.py` is untouched by this change; see section B.

## The problem this repo actually has

The review gate runs on `pull_request` today
(`src/no_human/ci_action/run.py:489-493` refuses every other event name).
That trigger cannot serve either population this repository has open pull
requests from:

- **Forks.** `_is_fork_pr` (`src/no_human/ci_action/run.py:250-263`) correctly
  skips them before the `credential` input is ever read
  (`src/no_human/ci_action/run.py:508-521`, skip happens before the
  credential read at `src/no_human/ci_action/run.py:524`) — a fork's head must never execute in a job
  holding a secret. But that means every external pull request open at
  filing time is a fork, so the gate reviews none of the diffs written by
  people the operator has never met — the population it is most valuable on.
- **Same-repository branches from someone else.** Push access to this
  repository is not limited to the maintainer. On `pull_request` from a
  branch in this repository, GitHub Actions runs the workflow file **as that
  branch has it**, with repository secrets and no approval step. Nothing
  written inside `review-gate.yml` — an `if:`, a `permissions:` block, an
  author allowlist, the step that reads `credential` — is a boundary,
  because whoever pushed the branch can edit the file that contains it.

## A. Trigger — why `pull_request_target` stays refused and `workflow_run` is the split

**Named GitHub behaviour #1 (why nothing in a `pull_request` workflow file is
a boundary).** For a same-repository `pull_request` event, GitHub Actions
checks out and executes the workflow YAML **from the pull request's own head
ref**, in a job that receives repository secrets and requires no approval.
A branch author who can push commits can therefore rewrite the very file
that would otherwise gate them — there is no version of `review-gate.yml`
that defends against its own author.

**Named GitHub behaviour #2 (why `workflow_run` is different).** A
`workflow_run` workflow is triggered when another workflow completes, and
GitHub Actions always executes the `workflow_run` workflow's YAML **from the
repository's default branch**, regardless of which branch or fork triggered
the workflow it is watching. Changing what that job does requires landing a
commit on `main` — which is the operator's own gate, not something a pull
request author can reach. This is GitHub's documented answer to exactly this
shape of problem: a small `pull_request` workflow holding no secrets records
facts about the PR (number, head sha), and a `workflow_run` workflow —
whose code a contributor cannot change without merging to `main` first —
holds the credential and does the review.

**Why OIDC-to-AWS does not solve it (recorded so it is not re-proposed).** An
AWS key sitting in GitHub Secrets is the same exposure one hop away: whoever
can edit the `pull_request` workflow file can add a step that prints or
exfiltrates whatever the job can reach, cloud credential or not. OIDC looks
like it adds a check and mostly does not: the default `sub` claim GitHub
issues for a `pull_request` event is `repo:OWNER/REPO:pull_request`,
identical for every pull request whoever opened it, so a trust policy keyed
on that default `sub` cannot tell the maintainer's push from anyone-with-
push's. (This document does not enumerate GitHub's full set of customizable
`sub`-claim keys — that enumeration would need an external citation this
design does not have — and rests nothing on it below.)

The one claim that reliably discriminates is `environment`, and the
mechanism that makes it discriminate is a **deployment-branch policy** on
that environment, not a human clicking Approve on every run. Two different
things were checked here, and they are kept separate on purpose: the first
shows only that a policy is *configured*; the second is a measurement of
what the policy actually *does* to a running job.

**Configuration observed** (this is setup, not behaviour):

    $ gh api repos/no-human-ai/no_human/environments/review-gate \
        --jq '{protection_rules:(.protection_rules|length)}'
    {"protection_rules":1}
    $ gh api repos/no-human-ai/no_human/environments/review-gate/deployment-branch-policies \
        --jq '[.branch_policies[].name]'
    ["main"]

`no-human-ai/no_human` has a `review-gate` environment with one protection
rule and a branch policy limited to `main`. That transcript proves the
policy exists; it says nothing about what happens when a job on a
disallowed branch actually requests that environment.

**Refusal observed** (this is the behaviour itself, measured by running a
job, not inferred from configuration): GitHub Actions run `35283704459`
requested the `review-gate` environment from `refs/pull/531/merge` — a
non-`main` ref — and failed in under two seconds with an **empty steps
array**: no step in that job, including the one that would have read the
secret, ever started. The run's annotation reads verbatim:

    Branch "refs/pull/531/merge" is not allowed to deploy to review-gate
    due to environment protection rules.

The precise shape of that behaviour matters for anyone implementing the
follow-up: this is a **hard job failure at the environment boundary**, not
"the job runs, just without the secret." Nothing executes — not a
degraded step, not a partial review, not a comment saying the credential
was unavailable. An implementer who designs a graceful skip (proceed
without the credential, post a note explaining why) is building something
this measurement does not support; the environment boundary aborts the
whole job before its first step, the same way a fork PR is skipped by
`_is_fork_pr` before `credential` is read (section B) rather than allowed
to run degraded.

A job that requests `review-gate` from a contributor's branch is refused
the secret at the environment boundary, automatically, on every run — no
click required, and no code inside `review-gate.yml` decides it. Required
reviewers is a second, heavier layer the same `environment` mechanism
supports (a human clicks Approve before the job proceeds); it is not the
only way `environment` discriminates, and this design does not require it
given the branch-policy refusal just measured. Either way, OIDC-to-AWS is a
detour, not a fix by itself: the environment scoping protects whichever
secret it fronts — a cloud credential or the Anthropic `credential` this
gate already uses — so routing through AWS adds a second credential and a
second trust policy to protect the same thing environment scoping already
protects directly (section C).

**What still needs the split to cover forks.** `workflow_run` is triggered
by an upstream workflow that itself ran on `pull_request` (or
`pull_request_target`) — including for fork PRs, since the lightweight
recorder workflow carries no secret and is safe to run on a fork's head. The
privileged `workflow_run` job then fetches the fork's diff **as data**
through the REST API (section F) rather than checking out the fork's head —
so a fork contributor's code is never executed by the job that holds the
credential, closing the fork gap the pure `pull_request` trigger left open.

**The untrusted-handoff hole a naive `workflow_run` split still has.** The
`workflow_run` event payload carries `workflow_run.head_sha`,
`workflow_run.head_branch`, and `workflow_run.pull_requests` — and
`pull_requests` is **empty whenever the triggering run's head repository is
a fork**, which is precisely the population this split exists to serve. So
the PR number cannot come from the trusted `workflow_run` payload alone for
a fork; it has to be read from an **artifact the untrusted upstream job
wrote** (e.g. `echo "$PR_NUMBER" > pr_number.txt` uploaded as a build
artifact). That artifact is attacker-controlled *data*, not a capability,
and the privileged job must treat it that way: fetch
`GET /repos/{owner}/{repo}/pulls/{artifact_pr_number}` and refuse to proceed
unless the returned `head.sha` equals `workflow_run.head_sha` (a claim the
attacker cannot forge, because it comes from the trusted event payload, not
the artifact). Skipping this re-validation would let a fork PR's artifact
claim an arbitrary PR number and have the privileged job post its review
comment — or worse, act — on a PR it never fetched the diff for.

Two more conditions the follow-up must enforce, neither optional. First,
the artifact must be downloaded scoped to the triggering
`workflow_run.id` specifically (`GET
/repos/{owner}/{repo}/actions/runs/{workflow_run.id}/artifacts`, then
download by the returned artifact id) — a lookup by artifact *name* alone,
without pinning the run id, could resolve to a same-named artifact
uploaded by a different, unrelated workflow run and let that run's author
substitute their own PR number. Second, `head.sha == workflow_run.head_sha`
is necessary but **not sufficient** to identify a unique pull request: the
same head commit can be the head of two simultaneously open pull requests
from the same branch against two different base branches, and both would
satisfy that equality. `GET /repos/{owner}/{repo}/pulls/{artifact_pr_number}`
alone cannot distinguish them. The follow-up must instead resolve the PR
through `GET /repos/{owner}/{repo}/commits/{workflow_run.head_sha}/pulls`
(GitHub's own commit-to-PR lookup, keyed on the trusted `head_sha`, not on
the artifact's claimed number) and refuse — post nothing, exit non-zero —
unless that call returns **exactly one** open pull request; the artifact's
number is then only a cross-check against that result, never the sole
source of truth for which PR to comment on.

**The `workflow_run` trigger is not scoped by branch or event on its own.**
`workflow_run` fires whenever a workflow *with the watched name* completes,
regardless of what triggered that run. The lightweight recorder workflow is
itself a `pull_request` workflow (section A), so its YAML lives on whichever
branch is running it — a branch author can add an `on: push` trigger to
that same workflow file on their own branch and have the privileged
`workflow_run` job fire for a plain push, not a pull request. The privileged
job must therefore check `github.event.workflow_run.event == "pull_request"`
before doing anything else, and must check
`github.event.workflow_run.conclusion == "success"` (a triggering run that
failed or was cancelled must not be reviewed, and its artifact must not be
trusted). Both fields come from the trusted `workflow_run` event payload
itself, not from attacker-controlled artifact data, so — unlike the PR
number (previous paragraph) — checking them requires no round-trip to the
REST API. Neither check is optional: skipping either lets a branch author
drive the privileged job from an event this design does not intend to serve.

**Downloading the untrusted artifact is out of scope for this design, and
that is stated here rather than left implicit.** The artifact the recorder
workflow uploads (previous paragraph) is itself untrusted input once
downloaded: GitHub serves a workflow-run artifact as a zip, and code that
extracts one must not trust entry names (path traversal via `../` segments
or absolute paths) or entry sizes (a zip bomb) before writing anything to
disk. The follow-up implementation must either extract under a fresh
temporary directory with path-containment and size checks applied to every
entry before it is trusted, or avoid extraction entirely by reading only the
one small text field it actually needs (the PR number) as a byte stream from
the download API without writing the zip's other entries to disk. This
design does not choose between those two; it only requires that whichever
one the follow-up picks treats the artifact as adversarial, the same way
section F treats every REST response as adversarial.

## B. What stays exactly as it is

This design changes no behaviour in `src/no_human/ci_action/run.py`; the
follow-up adds a `workflow_run` branch *alongside* the existing gate and
narrows neither of the following:

- `_is_fork_pr` (`src/no_human/ci_action/run.py:251`: "True when the PR's
  head repository is not the base repository.") keeps skipping forks on the
  `pull_request` trigger (`src/no_human/ci_action/run.py:508-521`), before
  `credential` is read (`src/no_human/ci_action/run.py:524`).
- `pull_request_target` stays refused outright, exit 2
  (`src/no_human/ci_action/run.py:482-488`).
- An unsupported event name (anything other than `pull_request` or
  `pull_request_target`, including `workflow_run` before the follow-up
  lands) is also refused, not silently skipped
  (`src/no_human/ci_action/run.py:491`: "this Action only").

Forks are served by fetching their diff as data over the REST API (section
A, section F) inside the privileged `workflow_run` job — never by checking
out or executing a fork's head, on any trigger. The diff proves this: this
change makes `src/no_human/ci_action/run.py` unchanged (`git diff --stat --
src/no_human/ci_action/run.py` is empty).

## C. What this does NOT fix: prompt injection is a separate problem from exfiltration, and the split alone does not protect the credential either

The `workflow_run` split removes exactly one thing: **exfiltration by
editing this job's code.** A branch author cannot edit the job that holds
the credential, because that job's code only exists on `main` (section A).
That is narrower than "the credential is safe": a GitHub **repository**
secret is readable by any workflow run in this repository's own context —
any branch someone with push access controls, under any trigger that
receives secrets at all (`push`, `workflow_dispatch`, `schedule`,
`workflow_run`, and same-repository `pull_request`, section A). Someone
with push access does not need to edit `review-gate.yml` at all; they can
push a branch carrying their own `.github/workflows/anything.yml` with
`on: push` and reference `secrets.ANTHROPIC_API_KEY` directly. The
`workflow_run` split stops none of that on its own. This is deliberately
scoped to the same-repository population — a **fork's** `pull_request` run
is the one context that does *not* receive repository secrets at all
(GitHub withholds them there; that is the entire reason
`pull_request_target` exists, and it is why section A refuses it,
`src/no_human/ci_action/run.py:482-488`), so nothing above weakens the
fork boundary section B already keeps.

`action.yml:13-14`: "Pass it from a repository secret, e.g.
secrets.ANTHROPIC_API_KEY." — that is exactly the exposure described above,
and it is the language this design corrects rather than repeats. The
credential is protected only when it is instead an **environment secret**,
on an environment whose deployment-branch policy restricts deployment to
`main` — the same mechanism verified live in section A (`review-gate`
environment, one protection rule, branch policy `["main"]`). Under that
configuration a job can request the environment's secret only when it is
running from an allowed branch; a job triggered from a contributor's branch,
by any trigger, is refused the secret at the environment boundary before any
step in that job executes. The `workflow_run` split and the environment-
scoped secret are two different, complementary fixes: the split closes the
fork/same-repo diff-review gap on `pull_request` by moving the reviewing
code to a boundary a branch author cannot edit; the environment scoping is
what keeps the credential itself out of reach of every *other* workflow a
branch author can add. Neither is sufficient alone; the follow-up
implementation needs both.

It does **not** remove **prompt
injection**: an attacker-chosen diff still enters a job that holds a
credential and can read files and run commands. This is not hypothetical in
this codebase's own history — `docs/UNTRUSTED_PR_REVIEW.md:5-10` records
that the review gate has already caught a prompt-injection channel into the
merge gate, on a *friendly* diff written by someone with no adversarial
intent. An external pull request over the `workflow_run` split is that same
channel with an adversary on the other end of it.

Existing, unchanged mitigations for the part of this that the split does not
touch:

- The PR title and body are explicitly labelled untrusted data in the task
  handed to the reviewer, never as instructions
  (`src/no_human/ci_action/run.py:696-704`).
- `_PR_BODY_CAP = 4000` bounds how much of that untrusted text reaches the
  model at all (`src/no_human/ci_action/run.py:113`).
- The Action's only possible writes are two comment endpoints — it can never
  merge, push, or edit the pull request itself
  (`src/no_human/ci_action/github.py:61-73`).
- The reviewed diff is never a checkout of the fork's head; under this
  design it is REST-fetched text (section F), which is *stronger* than
  today's same-repo case (today's `pull_request` path does execute `git
  diff` against a real checked-out merge commit,
  `src/no_human/ci_action/run.py:601-624`) precisely because there is no
  working tree for an injected file to plant a hook script or tool config
  into.

The container profile in `docs/UNTRUSTED_PR_REVIEW.md` remains the stronger
boundary for anything beyond posting a single comment; this design does not
supersede it.

## D. The tamper guard has no checked-out tree to walk

Today, `tamper_check_between(workspace, merge_base, head_sha)`
(`src/no_human/ci_action/run.py:689-692`) walks a real git working tree on
disk and raises `TamperCheckUnavailable` rather than silently reporting
"clean" when it cannot
(`src/no_human/testing/runner.py:1764`: "Raises `TamperCheckUnavailable`
when the checkout is not there to inspect." — the class is defined at
`src/no_human/testing/runner.py:1650`, the raises at
`src/no_human/testing/runner.py:1780` and
`src/no_human/testing/runner.py:1783`). Today's caller treats that
exception as a hard failure: `except TamperCheckUnavailable as exc: return
_fail(...)` (`src/no_human/ci_action/run.py:690-691`) — the whole run exits
2 rather than post a comment.

A diff fetched through the REST API (section F) is text, not a checkout —
and `tamper_check_between` does not run `git diff` at all, so the reason it
cannot run here is not "there is nothing to diff", it is "there is nothing
to snapshot". For each of `before_ref` and `after_ref` it *lists* every path
with `_git_files` (`git ls-tree -r --name-only <ref>`, run via
`src/no_human/testing/runner.py:1572`: "ls-tree"), *reads* each test-path
file's
content at that ref with `_git_show` (`git show <ref>:<path>`,
`src/no_human/testing/runner.py:1554-1567`), and hands the two
`{path: source}` snapshots to `tamper_guard.check`
(`src/no_human/testing/runner.py:1788-1802`). Both helpers require a real
`.git` checkout on disk; `tamper_check_between` itself raises
`TamperCheckUnavailable` up front when `repo_path` is not a directory or has
no `.git` (`src/no_human/testing/runner.py:1779-1784`) rather than reporting
clean. There is no REST equivalent to "list this ref's tree, then read this
path's blob at this ref" without re-implementing that walk against
`GET /repos/{o}/{r}/git/trees/{sha}?recursive=1` plus one
`GET /repos/{o}/{r}/contents/{path}?ref={sha}` per test file — one request
per file, unbounded by `max_files` because the guard must see the *full*
test tree, not just the reviewed subset
(`src/no_human/ci_action/run.py:26-27`: "against the FULL test tree, not
just the budgeted file subset").

(Separately, and **not** the reason a tree is missing here: `_git_files`'s
`git ls-tree` is unquoted, so a non-ASCII test filename round-trips
C-quoted rather than as its real path — `src/no_human/ci_action/run.py:33-40`
records that as a pre-existing limitation of the *git-based* path itself,
orthogonal to whether a tree exists to walk at all. The API path this design
chooses has no C-quoting exposure because it never shells out to `git`.)

**Decision: do not fabricate a tree — and do not fabricate a single sentence
either.** The reason "the tamper guard did not run" must be a **parameter
the caller passes to `render_body`**, not one fixed string baked into the
renderer. There are at least two distinct true reasons a review can lack a
tamper verdict, they are different in kind, and collapsing them into one
wording makes the comment assert a false cause on whichever path did not
write it:

**Reason (a) — `workflow_run` / API context, no checkout at all.** The diff
was fetched through the GitHub API (section F); no `git` checkout of the PR
exists in this job, so `tamper_check_between` (section D above) is never
invoked. Its reason text for this path must be: *"no checked-out repository
tree was available in this workflow_run context, so no test-tampering check
was performed."*

**Reason (b) — checkout (`pull_request`) path's own no-changed-files
branch.** This path already exists today and is unrelated to the API split:
when `kept` is empty, `main()` calls `render_body(..., tampered=False, ...)`
with `note="No file changes were found between the merge base and the head
commit — nothing to review."` (`src/no_human/ci_action/run.py:630-637`, the
literal note text is `src/no_human/ci_action/run.py:635`). A real checkout exists on this path —
the reason there is no tamper verdict is not "no tree", it is "no changed
files to check for tampering". Its reason text must stay this one, verbatim
close to what already ships: *"no file changes were found between the merge
base and the head commit, so there was nothing to check for tampering."*

**Binding rule 0 (new): a fixed sentence is itself a bug.** A follow-up
implementation that reuses reason (a)'s wording on path (b), or vice versa —
for example by threading one shared string constant through both call
sites — misstates the cause on whichever path did not write it, and must be
rejected in review exactly as if it were the fabricated-clean bug below.
Each path renders its **own** true reason; no path may borrow another's.

Two more rules follow, both already true of today's code and both binding on
the follow-up. Neither is about `render_body` fabricating a clean tamper row
by itself: `render_body` (`src/no_human/ci_action/run.py:400-453`) has no
else branch on `tampered` — it appends a "TAMPERED" line when `tampered` is
true and otherwise says nothing about tamper at all
(`src/no_human/ci_action/run.py:423-424`), and it is already called today
with a hardcoded `tampered=False` and no real tamper report in the
legitimate "no changed files" branch (reason (b) above,
`src/no_human/ci_action/run.py:630-637`). That call is fine precisely
because there is nothing to tamper with when there are no changed files.

The actual mechanism that can fabricate a clean-looking tamper signal is
`_tamper_checklist_items` (`src/no_human/ci_action/run.py:341-371`): it
builds a `ChecklistItem` labelled "tamper guard" with a `passed` field set
per `src/no_human/ci_action/run.py:364`: "passed=not report.tampered"
verbatim from whatever `TamperReport` it is handed. When `tampered` is false
that item is routed into the *advisory* list alongside the reviewer's own
advisory findings (`src/no_human/ci_action/run.py:744-745`) and rendered by
`_findings_table` (`src/no_human/ci_action/run.py:391-397`) as an ordinary
passing row in the "Advisory findings" table — indistinguishable there from
a tamper guard that actually ran and found nothing. **Rule 1**: a
`workflow_run` path that did not run the guard must never call
`_tamper_checklist_items` with a fabricated `TamperReport(tampered=False)`
to manufacture that row; when the guard did not run, no "tamper guard" row
may appear in the findings table at all — only the path's own true reason
text, delivered through the parameter below. **Rule 2**: `tampered` must
never contribute to the PASS/FAIL verdict
(`src/no_human/ci_action/run.py:754`) when the guard did not run — a
`workflow_run` review's verdict is FAIL only on reviewer findings, never on
an absent tamper signal being coerced to "clean".

**The proposed signature.** `render_body` already carries a `tampered: bool`
and a free-form `note: str = ""` today (`src/no_human/ci_action/run.py:410-411`,
`note` already used this way at `:635`). Neither `tamper_ran` nor
`tamper_skip_reason` exists in this tree — **this design does not add
them**, since `run.py` is deliberately unchanged here (section B); the
follow-up implementation must add both: `tamper_ran: bool = False` (the
render **defaults to "did not run", fail closed — never to "clean"**) and
`tamper_skip_reason: str | None = None`, the per-path parameter carrying
reason (a) or reason (b) verbatim. `_tamper_checklist_items` may be called
only when `tamper_ran` is `True` and a real `TamperReport` was produced.
This generalizes rules 1 and 2 above into the call signature itself: a
caller cannot pass `tamper_ran=True` without also having a real report,
because there is no report parameter to fabricate one from.

The deferred alternative (source both sides through
`git/trees?recursive=1` + `contents`, failing closed whenever any response
reports `"truncated": true`) is recorded here, not built — it is one HTTP
request per test file plus whatever fan-out the directory tree requires,
and is explicitly follow-up work.

## E. Cost bound: `max_files` and the reviewer cap both still apply

Today, `max_files` (`DEFAULT_MAX_FILES = 15` at
`src/no_human/ci_action/run.py:96`; parsed and applied as a sorted, first-N
slice at `src/no_human/ci_action/run.py:601-624`) caps the file list
*before* any diff content is computed, and the reviewer's own single-turn
cap refuses rather than silently truncates
(`src/no_human/ci_action/run.py:675-687`; the cap itself,
`_DIFF_CAP = 60_000`, is `src/no_human/review/reviewer.py:166`, imported as
`_REVIEWER_DIFF_CAP` at `src/no_human/ci_action/run.py:80`). Both bounds
apply unchanged to a `workflow_run` review — an external pull request can be
arbitrarily large, and nothing about fetching a diff over the network
relaxes either limit.

For the API path specifically (section F), `max_files` must be applied to
the **file list** (`GET .../pulls/{n}/files`, paginated) before any file
`contents` are fetched — fetching all changed files' content and cropping
afterward would spend the network and byte cost the cap exists to avoid.
New bounds this path needs that the git-based path gets for free:

- Pagination is capped at `ceil(max_files / 100)` pages of
  `GET .../pulls/{n}/files?per_page=100&page=N` — never an unbounded
  `--paginate`.
- `patch` is absent from a file-list entry for a binary file or one over
  GitHub's per-file diff size limit; that must be treated the same as
  today's "could not compute the scoped diff" refusal
  (`src/no_human/ci_action/run.py:644-645`), a loud refusal, never a
  silently empty diff for that file.
- GitHub stops listing files in the `/files` response at 3000 changed
  files. `max_files` has no upper bound enforced today
  (`src/no_human/ci_action/run.py:601-605` parses it as any positive
  integer), so an operator who raises it past the default 15 can reach
  both this cap and more than one page of pagination; either must produce
  the same "refusing rather than reviewing a truncated prefix" message
  pattern `src/no_human/ci_action/run.py:675-687` already uses for the
  local cap, never a partial review presented as complete. (The `406` on
  the whole-PR unified-diff media type, table row 2 below, is a property
  of an endpoint this design rejects for the primary path — it cannot be
  hit through the chosen `/files` + `/contents` path and is recorded in
  section F only as the reason that endpoint was rejected, not as a state
  the follow-up must handle.)
- `GET /repos/{o}/{r}/pulls/{n}` returns `changed_files`, the PR's true
  total independent of pagination; the follow-up must read it from there; a
  count derived from summing paginated `/files` pages alone is only correct
  once every page has been fetched, and `render_body`'s
  `"Files reviewed: N of M"` line with its "(capped by `max_files`)" suffix
  (`src/no_human/ci_action/run.py:420-421`) needs the true `M` to render
  that suffix correctly when `max_files` truncates the list.

## F. The REST calls this design chooses, and every state they return

All read-only, idempotent; nothing here mutates anything on GitHub.

| Call | States |
|---|---|
| `GET /repos/{o}/{r}/pulls/{n}` | 200; 301 (refuse, do not follow — mirrors `src/no_human/ci_action/github.py:146-150`'s existing rule for the comment endpoints); 304; 401; 403 (not accessible, or rate-limited — check `x-ratelimit-remaining: 0`); 404; 410; 429; 5xx |
| `GET /repos/{o}/{r}/pulls/{n}` with `Accept: application/vnd.github.diff` | 200 with a unified-diff body; 406 when the PR is too large to diff. **Recorded and rejected** for the primary path: one request, but no per-file granularity, so `max_files` cannot be enforced before the bytes are already spent. |
| `GET /repos/{o}/{r}/pulls/{n}/files?per_page=100&page=N` | `filename`, `status` in `{added, removed, modified, renamed, copied, changed, unchanged}`, `additions`, `deletions`, `sha`, `contents_url`, optional `patch` (absent per the binary/oversize case in section E), `previous_filename` on a rename. **Chosen primary path.** |
| `GET /repos/{o}/{r}/contents/{path}?ref={sha}` | `{content, encoding: "base64", size, sha, type}`; `encoding == "none"` with an empty `content` above roughly 1&nbsp;MB (use `raw_url`/the blobs API instead); `type` may be `dir`/`symlink`/`submodule` rather than `file`; 404 for a path absent at that ref (the legitimate `added`/`removed` case, not an error). **Secondary, per-file fallback** — only called for a changed file whose `/files` entry has no `patch` (the binary/oversize case above); an entry that already carries a `patch` needs no separate content fetch, so this call is not on the path for the common case. |

Failure handling mirrors the existing client's taxonomy
(`src/no_human/ci_action/github.py:144-200`): every one of the above states
that is not a clean 200 is "the tamper/review step did not run" → exit 2,
**never a PASS**. Comment posting is unchanged and needs no new code: the
marker-keyed upsert (`MARKER`, `src/no_human/ci_action/run.py:106`;
`find_marked_comment`/`upsert_comment`,
`src/no_human/ci_action/github.py:257-292`) is idempotent by construction,
so repeated `workflow_run` runs for the same PR replace one comment in
place rather than piling up duplicates.

`_assert_write_allowed` (`src/no_human/ci_action/github.py:71`: "the
Action's only allowed writes are") allows only `GET`/`POST
.../issues/{n}/comments` and `PATCH .../issues/comments/{id}` — none of the
four calls in the table above is reachable through today's `GitHubClient`. Widening that allowlist (or adding
a second read-only client) to reach `pulls`, `pulls/files`, and `contents`
is explicit follow-up work this design describes and does not implement;
`src/no_human/ci_action/github.py` is unmodified by this change.

## G. Scope

This is a design document. The `workflow_run` branch in
`src/no_human/ci_action/run.py`, the new `review-gate.yml` +
`review-gate-recorder.yml` workflow pair, and the widened `GitHubClient`
read surface all land as a follow-up once this design is reviewed. The
smallest proof this design includes is a test that exercises the REST path
chosen in section F against one real, immutable, merged public pull
request — see `tests/test_pr_diff_via_api.py` — without adding any
production module; the fetch path here is designed as data, not shipped.
