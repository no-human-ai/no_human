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
like it adds a check and does not: the default `sub` claim GitHub issues for
a `pull_request` event is `repo:OWNER/REPO:pull_request`, identical for
every pull request whoever opened it. `actor` is not among the claim keys
GitHub allows into a *custom* `sub` claim — the allowed set is `repo`,
`context`, `repository_owner`, `repository_visibility`, `job_workflow_ref`,
`repository_id`, `repository_owner_id`, `environment`, and `repo_property_*`.
None of those discriminates "the maintainer opened this" from "anyone with
push access opened this". The one claim that could — `environment` — only
discriminates if the environment has required reviewers, i.e. a human clicks
Approve on every run, which defeats the automation this gate exists to
provide. AWS, with or without OIDC, adds nothing to the trust boundary here.

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

## B. What stays exactly as it is

This design changes no behaviour in `src/no_human/ci_action/run.py`; the
follow-up adds a `workflow_run` branch *alongside* the existing gate and
narrows neither of the following:

- `_is_fork_pr` (`src/no_human/ci_action/run.py:250-263`) keeps skipping
  forks on the `pull_request` trigger
  (`src/no_human/ci_action/run.py:508-521`), before `credential` is read
  (`src/no_human/ci_action/run.py:524`).
- `pull_request_target` stays refused outright, exit 2
  (`src/no_human/ci_action/run.py:482-488`).

Forks are served by fetching their diff as data over the REST API (section
A, section F) inside the privileged `workflow_run` job — never by checking
out or executing a fork's head, on any trigger. The diff proves this: this
change makes `src/no_human/ci_action/run.py` unchanged (`git diff --stat --
src/no_human/ci_action/run.py` is empty).

## C. What this does NOT fix: prompt injection is a separate problem from exfiltration

The `workflow_run` split removes **exfiltration by code edit** — a branch
author cannot edit the job that holds the credential, because that job's
code only exists on `main` (section A). It does **not** remove **prompt
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
(`src/no_human/testing/runner.py:1748-1783` — the class is defined at
`src/no_human/testing/runner.py:1650`, the raises at
`src/no_human/testing/runner.py:1780` and
`src/no_human/testing/runner.py:1783`). Today's caller treats that
exception as a hard failure: `except TamperCheckUnavailable as exc: return
_fail(...)` (`src/no_human/ci_action/run.py:690-691`) — the whole run exits
2 rather than post a comment.

A diff fetched through the REST API (section F) is text, not a checkout:
there is no `before` tree and no `after` tree for `tamper_check_between` to
`git diff` between, because `testing/runner.py`'s own `_git_files` helper
that the guard walks is built on an unquoted `git ls-tree`
(`src/no_human/ci_action/run.py:34`, out of scope to change), which has
no REST equivalent without re-implementing tree-walking against
`GET /repos/{o}/{r}/git/trees/{sha}?recursive=1` plus one
`GET /repos/{o}/{r}/contents/{path}?ref={sha}` per test file — one request
per file, unbounded by `max_files` because the guard must see the *full*
test tree, not just the reviewed subset
(`src/no_human/ci_action/run.py:25-26`: "against the FULL test tree, not
just the budgeted file subset").

**Decision: do not fabricate a tree.** For a `workflow_run` review whose
diff came from the API, the tamper guard does not run, and the posted
comment says so in the sentence an implementer must copy verbatim:

> Tamper guard: **did not run** — this pull request's diff was fetched
> through the GitHub API with no checked-out test tree, so there was no
> before/after comparison. This verdict covers the diff only.

Two rules follow, both binding on the follow-up implementation: the comment
must never render a tamper-guard row that implies a check ran when it did
not (unlike today's `render_body`, which only ever sets
`tampered=True`/`False` from a real `tamper_report.tampered`,
`src/no_human/ci_action/run.py:739-740` — a `workflow_run` path must not
default `tampered=False` the way the "no changed files" branch does at
`:634`, which would silently read as "checked, and clean"); and
`tampered` must never contribute to the PASS/FAIL verdict
(`src/no_human/ci_action/run.py:754`) when the guard did not run — a
`workflow_run` review's verdict is FAIL only on reviewer findings, never on
an absent tamper signal being coerced to "clean". The deferred alternative
(source both sides through `git/trees?recursive=1` + `contents`, failing
closed whenever any response reports `"truncated": true`) is recorded here,
not built — it is one HTTP request per test file plus whatever fan-out the
directory tree requires, and is explicitly follow-up work.

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
  files, and a `406` is returned for the whole-PR unified-diff media type
  when a PR is too large to render as a diff — both must produce the same
  "refusing rather than reviewing a truncated prefix" message pattern
  `src/no_human/ci_action/run.py:675-687` already uses for the local cap,
  never a partial review presented as complete.

## F. The REST calls this design chooses, and every state they return

All read-only, idempotent; nothing here mutates anything on GitHub.

| Call | States |
|---|---|
| `GET /repos/{o}/{r}/pulls/{n}` | 200; 301 (refuse, do not follow — mirrors `src/no_human/ci_action/github.py:146-150`'s existing rule for the comment endpoints); 304; 401; 403 (not accessible, or rate-limited — check `x-ratelimit-remaining: 0`); 404; 410; 429; 5xx |
| `GET /repos/{o}/{r}/pulls/{n}` with `Accept: application/vnd.github.diff` | 200 with a unified-diff body; 406 when the PR is too large to diff. **Recorded and rejected** for the primary path: one request, but no per-file granularity, so `max_files` cannot be enforced before the bytes are already spent. |
| `GET /repos/{o}/{r}/pulls/{n}/files?per_page=100&page=N` | `filename`, `status` in `{added, removed, modified, renamed, copied, changed, unchanged}`, `additions`, `deletions`, `sha`, `contents_url`, optional `patch` (absent per the binary/oversize case in section E), `previous_filename` on a rename. **Chosen primary path.** |
| `GET /repos/{o}/{r}/contents/{path}?ref={sha}` | `{content, encoding: "base64", size, sha, type}`; `encoding == "none"` with an empty `content` above roughly 1&nbsp;MB (use `raw_url`/the blobs API instead); `type` may be `dir`/`symlink`/`submodule` rather than `file`; 404 for a path absent at that ref (the legitimate `added`/`removed` case, not an error). |

Failure handling mirrors the existing client's taxonomy
(`src/no_human/ci_action/github.py:144-200`): every one of the above states
that is not a clean 200 is "the tamper/review step did not run" → exit 2,
**never a PASS**. Comment posting is unchanged and needs no new code: the
marker-keyed upsert (`MARKER`, `src/no_human/ci_action/run.py:106`;
`find_marked_comment`/`upsert_comment`,
`src/no_human/ci_action/github.py:257-292`) is idempotent by construction,
so repeated `workflow_run` runs for the same PR replace one comment in
place rather than piling up duplicates.

`_assert_write_allowed` (`src/no_human/ci_action/github.py:61-73`) allows
only `GET`/`POST .../issues/{n}/comments` and `PATCH
.../issues/comments/{id}` — none of the four calls in the table above is
reachable through today's `GitHubClient`. Widening that allowlist (or adding
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
