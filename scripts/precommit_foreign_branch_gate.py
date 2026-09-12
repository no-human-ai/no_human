#!/usr/bin/env python3
"""Pre-commit gate: refuse to add a commit of YOUR identity to someone else's branch.

The defect this catches, measured 2026-09-12
--------------------------------------------
The product checks, before one of its own attempts commits, that every commit on
the task's branch was authored AND committed as the agent identity. A
supervising session that hand-finishes a task branch -- fixing a stale comment,
adding the test a review asked for -- leaves a commit under a different identity,
and the task's NEXT attempt dies:

    commit attribution mismatch -- one or more commits on this branch are not
    authored/committed as the agent identity: 248eb...

Task 7ee2d939 lost attempt 5 exactly that way and went straight to
BUDGET_EXHAUSTED, having already passed review on attempt 4. The hand-finish
felt free; it permanently removed the option of another coder round, and nothing
said so at the time. Three more live task branches were contaminated the same
night before anyone noticed, and only because a DIFFERENT task had failed.

What it checks, and why this instrument
---------------------------------------
Not the branch NAME. The supervising session does this work in DETACHED
worktrees, so `no-human/<task>-<n>` never appears -- the one shape that actually
causes the damage is the one a name check cannot see.

Instead: if the commits on HEAD that are not yet on the upstream trunk were
authored by somebody else, and you are about to add one as YOU, this branch is
someone else's work and your commit changes its attribution. That is config-free
(the agent identity is configurable -- `git.agent_identity_name`/`_email` -- so
it must not be hardcoded here), and it fires in a detached worktree.

It fires exactly ONCE per branch, on the commit that closes the door: afterwards
your identity is already among the authors. That is the moment the warning is
worth having.

Deliberately NOT blocked: a fresh branch off the trunk (nobody else's commits in
range), a branch where you are already an author, and the external-contributor
squash flow (which lands onto the trunk, so the range is empty).

Allow on uncertainty, on purpose. No upstream ref, a git that errors, a shallow
clone: ALLOW. A false refusal wedges the repo for everyone, while a miss costs
one attempt -- and unlike the manifest gate this guards a workflow mistake, not
a red main. Override a true positive deliberately with
NH_ALLOW_FOREIGN_BRANCH_COMMIT=1 (you mean to land it by hand; close the task so
nothing tries to resume it).
"""

from __future__ import annotations

import os
import subprocess
import sys

OVERRIDE_ENV = "NH_ALLOW_FOREIGN_BRANCH_COMMIT"
#: Trunk candidates, in order. The first that resolves wins.
UPSTREAMS = ("origin/main", "origin/master", "main", "master")


def _git(*args: str) -> str | None:
    """stdout, or None when git cannot answer. Never raises."""
    try:
        out = subprocess.run(("git",) + args, capture_output=True, text=True,
                             check=False)
    except OSError:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _my_email() -> str | None:
    """The address THIS commit will carry, honouring `-c user.email=...`."""
    ident = _git("var", "GIT_AUTHOR_IDENT")
    if not ident or "<" not in ident or ">" not in ident:
        return None
    return ident.split("<", 1)[1].split(">", 1)[0].strip().lower()


def _upstream() -> str | None:
    for ref in UPSTREAMS:
        if _git("rev-parse", "--verify", "--quiet", ref):
            return ref
    return None


def foreign_authors() -> tuple[set[str], str | None]:
    """(other people's author emails on this branch, the upstream used)."""
    up = _upstream()
    if up is None:
        return set(), None
    mine = _my_email()
    if mine is None:
        return set(), up
    log = _git("log", "--format=%ae", f"{up}..HEAD")
    if log is None:
        return set(), up
    authors = {a.strip().lower() for a in log.splitlines() if a.strip()}
    return {a for a in authors if a != mine}, up


def main(argv: list[str] | None = None) -> int:
    if os.environ.get(OVERRIDE_ENV):
        return 0
    others, up = foreign_authors()
    if not others:
        return 0
    mine = _my_email() or "(unknown)"
    who = ", ".join(sorted(others))
    print(
        "no_human pre-commit gate: REFUSED -- this branch is not yours.\n\n"
        f"  commits on {up}..HEAD were authored by: {who}\n"
        f"  you are committing as:                  {mine}\n\n"
        "Adding your commit changes this branch's attribution. If these are a\n"
        "no_human task's own commits, its NEXT attempt will die on\n"
        "\"commit attribution mismatch\" and the attempt is lost -- that is how\n"
        "task 7ee2d939 burned attempt 5 and hit BUDGET_EXHAUSTED after already\n"
        "passing review.\n\n"
        "  * needs another coder round?  Do NOT commit. Send it back:\n"
        "        nh reject <task-id> --reason '...'\n"
        "  * landing it by hand?         That is fine, and it is a ONE-WAY door:\n"
        f"        {OVERRIDE_ENV}=1 git commit ...\n"
        "    then record the landing and close the task so nothing resumes it.\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
