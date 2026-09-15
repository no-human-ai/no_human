# Git gc/maintenance bookkeeping in the shared common dir

`reviewer_worktree.py`'s integrity guard (`snapshot`/`compare`) walks the
whole `.git` tree — admin dir (this worktree's own) and common dir (SHARED
across up to four coder worktrees plus the operator checkout, via
`--git-common-dir`) — and discards a completed review verdict on ANY
unexplained change. `git gc --auto`, triggered by ordinary `commit`/`fetch`/
`merge` traffic in ANY of those other worktrees, writes its own bookkeeping
into that shared common dir — never into the worktree under review — and
until this change every one of those writes discarded the verdict of the
review that happened to be running at the time.

## Incident table

All five recorded on 2026-09-14, all zero tracked-path changes, all a
`.git/common/gc.pid` path in the discard:

| task | attempt | shape | cost |
|---|---|---|---|
| `f2dea6f3` | 4 | deleted | — |
| `a5beea7d` | 11 | deleted | task then failed `BUDGET_EXHAUSTED` |
| `7606f734` | 9 | created | — |
| `6e7eb947` | 3 | created | 8.2M cache-read tokens re-reviewing |
| `1cbc1c65` | 14 | rewritten | — |

~25M raw tokens across the five, zero tracked-path changes in any of them.

## Root cause

`git gc --auto` creates `$GIT_COMMON_DIR/gc.pid` (hostname+pid) at the start
of its run and removes it at the end, so a second concurrent `git gc` can
detect the first is already running by comparing the two — never executing
it, never resolving it to a path. `_is_volatile_git_path`'s common-scoped
excuse list already carried exactly two names for this same shape of problem
(`COMMIT_EDITMSG`, `info/refs`); `gc.pid` and its siblings were simply never
added to it.

## Research method

No web access was available while investigating this task (`WebFetch`/
`WebSearch` were both denied), so the candidate list below was confirmed or
corrected empirically against a real, local git repository (git 2.50.1,
Apple Git-155) rather than by reading git's C source, plus `man git-gc`/
`man git-maintenance` on this machine. Two techniques were used:

1. A tight, zero-sleep `os.listdir()` polling loop against a scratch repo's
   `.git` directory, running concurrently with a real `git maintenance run
   --quiet` (all tasks enabled: `commit-graph`, `loose-objects`,
   `incremental-repack`, `pack-refs`, `prefetch`) across 200 commits. This
   caught every transient name below, including two not in the original
   candidate list.
2. `GIT_TRACE2_EVENT` tracing and a pre-planted `maintenance.lock` stub,
   specifically to chase down the fourth candidate below.

## Disposition table

| common-dir path | writer | git's own use | disposition |
|---|---|---|---|
| `gc.pid` | `git gc --auto` (created at start, removed at end) | read only to compare against this host's own hostname+pid; never executed, never resolved to a path | **excused** — the measured incident |
| `gc.pid.lock` | git's generic lockfile API, renamed onto `gc.pid` on commit or deleted on rollback | transient, never read back | **excused** — same class as the pre-existing `index.lock`; confirmed empirically |
| `gc.log` | left behind when `git gc --auto` FAILS; aged out per `gc.logExpiry` (default 1 day) | contents only re-printed to stderr on the next attempt; presence/mtime suppress the next auto-gc (`man git-gc`) | **excused** |
| `packed-refs.lock` | `git pack-refs` (run by gc/maintenance), renamed onto `packed-refs` or deleted on rollback | transient, never read back — `packed-refs` itself is already excused, unscoped, in `_VOLATILE_GIT_EXACT` for exactly this reason | **excused**; confirmed empirically |
| `packed-refs.new` | `git pack-refs`'s tempfile for the NEW `packed-refs` content, renamed onto `packed-refs` | never read back as a ref source itself, never executed | **excused**; discovered empirically (not named by any manual page consulted), same rename-staging shape as `packed-refs.lock` |
| `HEAD.lock` | git's generic lockfile API, taken by any ref update touching `HEAD`, observed during `git maintenance run` (not only `git commit`) | transient, never read back | **excused**; discovered empirically. Excusing the LOCK does not weaken `common/HEAD`'s protection — the lock carries no content this guard adjudicates, and `common/HEAD`'s actual content stays watched, unexcused, via the content-shape check in `compare()` |
| `maintenance.lock` | `git maintenance run`, per `man git-maintenance`'s description of a lock "on the repository's object database" | — | **NOT excused, and NOT added to `_VOLATILE_COMMON_EXACT`** — see below |
| `config`, `config.worktree`, `config.lock` | `git maintenance register/start` writes `maintenance.*` keys into `config` | `config` is the exec-on-checkout surface (`include.path`, `alias.*`, `core.hooksPath`); `config.lock`'s bytes *become* `config` on rename | **kept watched** — `config` is already key-adjudicated (and `maintenance.*`/`gc.*` keys are already on `_BENIGN_CONFIG_KEY_PATTERNS`, a separate, pre-existing mechanism); `config.lock` has no measured incident and sits on the exec surface. This is the concrete argument against any `*.lock` glob: excusing `*.lock` blanket-wide would silently excuse `config.lock` too. |
| `shallow`, `shallow.lock` | gc/repack on shallow clones | read as the graft/depth boundary — changes what git considers reachable | **kept watched** — no measured incident; these worktrees are not shallow clones |
| `hooks/**`, `info/attributes`, `commondir`, `HEAD` | not written by gc | execute / filter-exec / pointer surfaces | **kept watched** — unrelated to this task, per `_is_volatile_git_path`'s docstring |
| `objects/**` (packs, idx, commit-graph, multi-pack-index and their locks, `info/alternates`), `refs/**` incl. `*.lock`, `worktrees/**` | gc/maintenance | — | **already walk-pruned** by the pre-existing `_SKIPPED_GIT_DIR_PREFIXES`; recorded as pre-existing, not re-decided by this task — see the `objects/info/alternates` conflict below |
| `logs/**` (reflog expiry) | gc | — | already excused via the pre-existing `_VOLATILE_GIT_PREFIX` |
| `info/refs`, `COMMIT_EDITMSG`, `index`, `index.lock`, `FETCH_HEAD`, `ORIG_HEAD`, `packed-refs` | — | — | already excused, unchanged by this task |

Uniform rule, in code and here: **excuse only names git never executes and
never follows to another path.**

**No prefix or glob exclusion was introduced; every entry added to
`_VOLATILE_COMMON_EXACT` is an exact, label-scoped name.** `config.lock`
(watched) and `packed-refs.lock`/`gc.pid.lock`/`HEAD.lock` (excused) sit
right next to each other in the disposition table above precisely to make
that point concrete: a `*.lock` glob would have swept all four together and
would have been wrong to do so.

### `maintenance.lock` — researched, not added

`man git-maintenance` describes `git maintenance run` as taking "a lock on
the repository's object database", explicitly distinguished from `git gc`'s
own locking ("`git gc` ... does not take the lock in the same way as `git
maintenance run`"). Despite that, no file literally named `maintenance.lock`
(or `maintenance.lock.lock`) was ever observed in `$GIT_COMMON_DIR` across:

- a pre-planted `maintenance.lock` / `maintenance.lock.lock` stub file
  ahead of a `git maintenance run` (neither blocked the run; both exited 0,
  unchanged on disk),
- a `GIT_TRACE2_EVENT` trace across a full multi-task `git maintenance run`
  (no `maintenance.lock`-named artifact in the event stream),
- the tight-loop `os.listdir()` capture described above, run twice — once
  as an ordinary `git maintenance run --quiet`, once as `git maintenance
  run --schedule=hourly` (the mode that actually guards against overlapping
  *scheduled* runs) — across a 200-commit repo with every maintenance task
  enabled.

Consistent with this module's own "measured, not assumed" discipline (see
`_git_dir_inventory`'s docstring: "NO COUNT AND NO TASK IDS ARE GIVEN ...
The MECHANISM below is reproducible; that is what belongs in permanent
source"), `maintenance.lock` is therefore **left out of
`_VOLATILE_COMMON_EXACT`** rather than added on the strength of the man
page's abstract description alone. It stays watched by default — the safe
direction for an unconfirmed name — and `test_the_gc_exclusions_are_exact_
label_scoped_names` pins that `_is_volatile_git_path("maintenance.lock",
"common")` returns `False`. If a future measurement confirms the literal
filename (a different git version, a different maintenance task
combination), it can be added the same way `gc.pid` was: with its own
comment and its own test.

## The `objects/info/alternates` criterion conflict

One of this task's acceptance criteria states that a change to
`objects/info/alternates` "still discards". **Against current `main` that is
false**, and this task does not make it true: `objects/` is pruned in its
entirety by the pre-existing `_SKIPPED_GIT_DIR_PREFIXES` (present on `main`,
untouched by this change — see the OUT OF SCOPE list in `.no_human/PLAN.md`
and `_is_volatile_git_path`'s docstring, which already records this exact
residual). A rewrite of `objects/info/alternates` is invisible to
`compare()` both before and after this commit.

`test_objects_info_alternates_is_unchanged_by_this_exclusion` in
`tests/test_reviewer_worktree.py` pins the actually-true property this task
owns: the delta for that path is identical before and after this commit
(empty in both cases) — a measured status quo, not a claim that the hole is
closed. Closing it is out of scope here (it would require resolving
`objects/`'s prune, which the OUT OF SCOPE list explicitly forbids touching
in this change) and remains a known, pre-existing gap.

## Mutation-check transcript

Manual check: strip the six new names (`gc.pid`, `gc.pid.lock`, `gc.log`,
`packed-refs.lock`, `packed-refs.new`, `HEAD.lock`) from
`_VOLATILE_COMMON_EXACT`, leaving only the original `COMMIT_EDITMSG`/
`info/refs`, and run the gc-focused tests:

```
$ uv run pytest tests/test_reviewer_worktree.py -q -k "gc or packed_refs_lock"
...
FAILED tests/test_reviewer_worktree.py::test_auto_gc_pidfile_in_the_common_dir_does_not_discard_the_verdict[created]
FAILED tests/test_reviewer_worktree.py::test_auto_gc_pidfile_in_the_common_dir_does_not_discard_the_verdict[rewritten]
FAILED tests/test_reviewer_worktree.py::test_auto_gc_pidfile_in_the_common_dir_does_not_discard_the_verdict[deleted]
FAILED tests/test_reviewer_worktree.py::test_gc_and_maintenance_bookkeeping_files_do_not_discard_the_verdict
FAILED tests/test_reviewer_worktree.py::test_a_real_write_alongside_gc_pid_churn_still_discards
5 failed, 3 passed, 35 deselected in 5.94s
```

Restore the six names and re-run the same selection:

```
$ uv run pytest tests/test_reviewer_worktree.py -q -k "gc or packed_refs_lock"
........
8 passed, 35 deselected in 5.97s
```

Full scoped suite, green on the restored (fixed) code:

```
$ uv run pytest tests/test_reviewer_worktree.py tests/test_reviewer_worktree_identity.py \
    tests/test_reviewer_worktree_wiring.py -q
........................................................................ [ 91%]
.......                                                                  [100%]
79 passed in 48.49s
```
