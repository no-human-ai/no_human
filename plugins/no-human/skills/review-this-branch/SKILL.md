---
name: review-this-branch
description: Run the no_human review gate (fresh-session adversarial reviewer + tamper guard) over the current branch or a GitHub pull request, with no server, no database, and no onboarding, and relay the pass/fail checklist.
---

# Review this branch

Run `nh gate` to get a fresh-session, adversarial code review and a
test-tampering check over a diff, right now, with nothing installed or
running beyond the `nh` CLI itself.

## When to use

- Someone asks "review this branch" / "check this PR" / "is this safe to
  merge" and wants a real second-model review with file:line citations, not
  a summary you write yourself.
- There is **no** no_human server running and **no** `~/.no_human` database
  set up. Unlike `file-a-task`, this skill needs neither. It runs once and
  exits.

## Prerequisite: your own Claude credential

`nh gate` uses **your own** Claude credential, the same one every other `nh`
command uses, never one belonging to no_human itself. If none is on file,
create it with:

```bash
claude setup-token
```

`nh gate` also needs the `nh` CLI and the `claude` CLI on `PATH`. If either
precondition is missing, the command refuses and names exactly what is
missing. It never prints a pass when it could not actually run.

## Running it

Two invocations, both read-only:

```bash
nh gate                        # current branch vs. its merge base with origin's default branch
nh gate --pr <github-pr-url>   # a GitHub pull request's head vs. its merge base
```

Optional flags: `--repo <path>` to point at a checkout other than the
current directory, `--base <ref>` to override the comparison base.

The comparison actually used, for example `working tree branch ... against
merge base with origin/<default>` or `pull request #N head ... against merge
base with origin/<default>`, is always printed at the top of the output. If
the working tree has uncommitted changes, the output says so explicitly and
those files are **not** reviewed; commit them first if they should be.

## Reading the result: the exit-code contract

| Exit code | Meaning |
|---|---|
| `0` | Gate passed: reviewer found no blocking findings **and** the tamper guard found no test-weakening. |
| `1` | Gate failed: a blocking review finding or a tamper-guard finding (deleted/weakened tests). |
| `2` | Gate **refused to run**. A named precondition failed (no credential, no upstream, not a git repo, PR fetch failed). |

**On exit `2`, never report a pass.** Relay the exact refusal message back to
the human (it names the missing credential, missing upstream, or fetch
failure) instead of guessing at a verdict. Only exit `0` is a pass; treat
exit `1` and exit `2` identically as "cannot say this is fine" until the
human has read the printed detail.

Relay the full Markdown checklist `nh gate` prints back to the human,
including every `file:line` citation and the tamper guard's before/after
counts. Do not summarize away the citations or the counts.

## Product boundary: read and report only, never write

**This skill only reads and reports. It never commits, pushes, merges, or
edits a file in your checkout, and it must never be followed by a commit, a
push, an approval, or a merge of the pull request on the agent's behalf.**
`nh gate` itself only runs read-only git plumbing against your checkout:
`rev-parse`, `merge-base`, `diff`, `status --porcelain`, `symbolic-ref`
(reads the locally recorded default branch; never a network call), `config
--get remote.origin.url` (to check a `--pr` URL names your own repo), and,
in PR mode, one additive `git fetch` of the PR's ref. Every invocation,
default and `--pr` alike, also makes one local `git clone --local --shared`
of your own checkout into a throwaway temp directory (see below) and a
`git checkout` inside that temp directory only. It also uses the tamper
guard's own read-only calls (`ls-tree`, `show`) and a read-only reviewer
backend. This list describes what the current implementation does, not a
promise that it will never grow; it never becomes a write.

In both modes it also makes a throwaway local clone of your checkout in a
temp directory, read-only against your checkout and deleted before the
command exits, so the review reads the exact committed tree it diffed
instead of your live working tree (which may be dirty) or, in PR mode,
whatever branch you happen to have checked out. Merge is always the human's
action. After running this skill, your job is to relay the checklist, not to
act on it.
