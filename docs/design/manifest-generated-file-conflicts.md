# Design: every landing re-conflicts every open PR through the generated manifest

Status: decided. Approach (c) — a targeted, single-file tolerance inside
`land_task`'s own squash step — is implemented
(`src/no_human/vcs/approve_merge.py`, steps 3-4). This document is the
comparison that decision was made against.

## §1 The problem

`RELEASE_MANIFEST.txt` pins every shipped file's path to a SHA-256, one row
per path — a lockfile for the tree. Landing ANY PR through `nh approve`
rewrites at least one of its rows (the landed PR's own changed files), and
`land_task`'s step 4 (`_land_regenerate_manifest`) rebuilds it wholesale from
the post-squash tree. Every PR branch cut before that landing therefore
carries a stale copy of the WHOLE file, not just the rows its own change
touched — so the next PR's squash sees RELEASE_MANIFEST.txt as changed on
both sides and conflicts, whatever that PR actually edited. With N PRs open
and landings serialized one at a time, this is an O(N²) rediscovery of the
same fact ("the manifest moved") charged against every PR after the first.
Live incident: task 4135165f / PR #356, 2026-09-14 — a landing on an
unrelated file (PR #356) put PR #319/#313/#302 into conflict on
RELEASE_MANIFEST.txt (among other files); the conflict-resolution task that
followed hit 11 attempts, ~484k tokens, ~$69, and still went
BUDGET_EXHAUSTED.

The guarantee that must survive whatever is chosen: **the published tree
matches the approved file set, hash for hash** — enforced today by
`scripts/check_release_manifest.py` (`--strict` in `nh approve`'s own step
6a, and standalone in CI on the exported tree). Nothing below is allowed to
make that check pass on a tree it should fail on.

## §2 Candidates weighed

**(a) A merge driver / `.gitattributes` union strategy for
`RELEASE_MANIFEST.txt`.** Register `merge=union` (or a custom driver that
re-runs `check_release_manifest.py --write` on conflict) in
`.gitattributes`, so git resolves the file mechanically instead of raising a
conflict. Rejected: a custom merge driver only runs where the invoking git
process has it configured — `.gitattributes` names the driver, but the
driver itself is registered in `.git/config` (`git config merge.<name>
.driver`), which is per-clone, not shipped with the repo. Two of the three
ways this repo's PRs actually land never run that local config at all:
GitHub's own "Squash and merge" button merges server-side through GitHub's
merge service, not a local git invocation with anyone's `.gitconfig`; and a
human running a bare `git merge --squash` in a fresh clone has never run the
one-time `git config` step either. It would only ever fire for a contributor
who separately provisioned the driver, which is not how this repo's
squash-lands happen. It also does not compose with the two-backend rule
(export-guard pin-approve vs. wholesale `--write`) `_land_regenerate_manifest`
already encodes — a driver script would have to reimplement that branching
outside Python, duplicating logic that already exists in
`scripts/check_release_manifest.py` and `scripts/export_guard.py`.

**(b) Regenerate the manifest at merge time (a hook), instead of storing a
row per file that every branch edits — general form.** A generic
`post-merge`/`pre-commit` git hook that reruns `--write` after every merge.
Same delivery problem as (a): git hooks live in `.git/hooks/`, are not
tracked by the repository, and do not run on GitHub's server-side squash
merge at all. A hook-based fix would help only the local-clone squash path,
and even there only for contributors who had the hook installed — the repo
has no committed hook-installation step today (`nh approve`'s own worktree
is a fresh, ephemeral clone per landing, so a hook would have to be
provisioned into it explicitly regardless).

**(c) — CHOSEN — the same idea as (b), but pushed into the one landing path
this repo actually controls: `land_task` itself, not a git-level hook.**
`land_task`'s step 3 (`_land_in_worktree`, squash) already runs
`git merge --squash` inside a worktree this process controls end to end. The
fix: when that squash conflicts, check whether the conflict is confined to
`RELEASE_MANIFEST.txt` alone (`_unmerged_paths(worktree_path) ==
{"RELEASE_MANIFEST.txt"}`); if so, take the tip's copy of that one file and
continue instead of refusing. Step 4 (`_land_regenerate_manifest`) already
rebuilds the manifest wholesale from the squashed tree on either backend
(export-guard pin-approve or `check_release_manifest.py --write`), so the
tip's copy is immediately superseded by a correct one — the branch's stale
copy of the ledger contributes nothing that step 4 doesn't already
recompute from the branch's real file changes. Step 6a's `--strict` check
(unchanged) then verifies the regenerated manifest against the landed tree
before it is ever pushed, so a genuine mismatch still fails the landing
exactly as before. This is (b)'s idea — rebuild instead of hand-merge — but
placed where it is actually enforceable: inside the one Python-level landing
procedure this repo runs for every merge, with no per-clone provisioning
and no dependence on git's own hook/driver plumbing.

**(d) Do not track the manifest at all; derive it in CI from the tree.**
Rejected on the stated guarantee: `check_release_manifest.py`'s own
docstring (and `scripts/build_public_export.py`, which gates the export on
this file) treats the manifest as the **approval ledger**, not just an
inventory — a row publishes that a specific hash of a specific path was
reviewed and is approved to ship. A derived-in-CI artifact answers "does the
tree match itself" (always yes, trivially) but throws away "was THIS hash of
THIS path ever approved" — the actual property the ledger exists to
freeze. Regenerating it on every CI run silently re-approves whatever is in
the tree at that moment, which is the guarantee being asked to hold, not a
way to keep it while sidestepping merge conflicts. Also flagged at intake as
requiring sign-off from whoever owns the validation system that consumes
this file (human-gated, not self-answerable) — a reason to not choose it
even before the guarantee argument, not a reason on top of it.

**(e) A serialized landing queue — at most one PR landing at a time.** This
already describes today's `nh approve` (each call runs to completion before
the next); it does not change *whether* PR B's branch conflicts with the
manifest PR A just rewrote, only guarantees the conflict is discovered once
per PR rather than re-discovered by a second concurrent landing racing the
same window. It does not reduce the O(N²) cost — each of the N-1 PRs still
independently hits the stale-manifest conflict the first time IT tries to
land, queue or no queue. Not a fix for the conflict itself; already in
effect and orthogonal to (a)-(d).

## §3 Recommendation

(c). It keeps the guarantee exactly as strict as before — step 6a's
`--strict` `check_release_manifest.py` run is untouched, still fails closed
on a hash mismatch or an unlisted ship file, and now also runs after every
tolerated manifest-only conflict, not just after a clean squash — and it is
the only candidate that actually executes on every landing path `nh
approve` is responsible for, rather than depending on per-clone git
configuration a driver/hook approach cannot guarantee is present.

## §4 What (c) does NOT close — read narrowly

- **Scope of the tolerance itself.** Step 3 only takes this path when
  `RELEASE_MANIFEST.txt` is the SOLE unmerged path. A conflict that spans
  the manifest AND a hand-authored file still refuses at step 3, same as
  before every PR in this incident. Of the three PRs open when this was
  written — #319, #313, #302 — **none is manifest-only**; each also
  conflicts on other files, so this change does not by itself make any of
  the three land without a coder round. It removes RELEASE_MANIFEST.txt
  from what that round has to resolve; it does not remove the round.
- **Landing path covered.** This lives entirely inside `land_task` (the
  `nh approve` code path). A human running `git merge --squash` by hand, or
  GitHub's own "Squash and merge" button, still hits the raw multi-way
  conflict with no help from this change — see (a)/(b) above for why a
  git-level fix for those two paths was rejected rather than silently
  assumed to also be covered.
- **The other two cross-cutting files from the same incident,
  `docs/security.md` and `tests/test_readme_claims.py` — deliberately
  ruled out**, not merely un-addressed. Both are hand-authored: their
  content on a branch IS the information (a security-policy edit, a new
  README-claims check), with no tree-derived value to regenerate it from
  the way a manifest row is derived from a file's bytes. Auto-resolving a
  conflict on either by preferring the tip's copy would silently discard
  the branch's own edit forever — exactly the class of quiet data loss this
  module's "never weaken a check to make merging easier" rule forbids.
  Their conflicts are left as real coder-round conflicts, same as any other
  hand-authored path, because they ARE real: two branches' prose actually
  disagreeing, not an artifact of a generated ledger moving under every
  landing the way RELEASE_MANIFEST.txt's rows do.

## §5 Evidence this closes the manifest-only case

`tests/test_approve_merge.py::
test_two_independent_prs_from_the_same_base_both_land_without_manual_conflict_resolution`
cuts two branches from the SAME base commit in two separate clones (neither
sees the other's commit — the actual shape of two independently opened
PRs), regenerates each branch's own `RELEASE_MANIFEST.txt` via
`check_release_manifest.py --write` after adding its file (so each branch's
ledger genuinely diverges from the other, not just from base), lands both
through `land_task`, and asserts both return `ok=True`, that the landed
manifest carries both branches' pins with no leftover conflict markers, and
that a fresh checkout of the result passes `check_release_manifest.py
--strict`. Confirmed red against `origin/main`'s `approve_merge.py` (fails
at `step=squash` with a real `CONFLICT (content): Merge conflict in
RELEASE_MANIFEST.txt`) and green with this change.
