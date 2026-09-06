#!/usr/bin/env bash
# Run no_human test suite.
#
# Usage:
#   ./scripts/run_tests.sh              # full suite, parallel
#   ./scripts/run_tests.sh fast          # the PR lane: not slow, not nightly
#   ./scripts/run_tests.sh slow          # slow tests only (~5min)
#   ./scripts/run_tests.sh nightly       # the nightly lane: slow or nightly
#   ./scripts/run_tests.sh full          # all tests, parallel
#
# `fast` and `nightly` are the two halves of the CI split in
# .github/workflows/ci.yml, spelled identically on purpose: if the selectors
# here and there disagree, a green local run stops predicting the CI result,
# and a node can fall out of both lanes without anything going red.
# tests/test_test_lanes.py asserts the two files agree.
#
# Designed to work with any runner: local, agent-a, CI.
# Exit code 0 = all tests passed.
set -euo pipefail

cd "$(dirname "$0")/.."

MODE="${1:-full}"
shift 2>/dev/null || true

# The board's role-attribution logic (web/src/eventRoles.js) decides which agent
# each event is drawn as. It is pure JS, so node's built-in runner exercises it —
# no extra dependency. Skipped when node isn't installed.
run_web_tests() {
  if command -v node >/dev/null 2>&1; then
    echo "=== Running web tests (node --test) ==="
    (cd web && node --test src/)
  else
    echo "=== Skipping web tests (node not installed) ==="
  fi
}

case "$MODE" in
  fast)
    echo "=== Running the PR lane (not slow, not nightly) ==="
    uv run pytest -q --tb=short -n auto -m "not slow and not nightly" "$@"
    run_web_tests
    ;;
  slow)
    echo "=== Running slow tests only ==="
    uv run pytest -q --tb=short -n auto -m slow "$@"
    ;;
  nightly)
    # -n 4, not -n auto: this one is run unattended by the 03:00 launchd job on
    # a machine that is also serving the live queue, and -n auto has wedged
    # this repo before. The other modes are left as they were.
    #
    # THIS LANE DELIBERATELY DESELECTS ONE MORE TEST THAN ci.yml DOES, and the
    # difference is temporary. Keep the two lists in step apart from this entry;
    # tests/test_deselect_lists_agree.py enforces that and names this exception.
    #
    # KI-1 (docs/KNOWN_ISSUES.md) used to fail
    # test_two_repos_run_concurrently_in_worktrees in roughly a third of runs.
    # It was re-measured on 2026-09-06 after the serialized_write lock at 0
    # failures in 400 serial runs and 13 whole-suite -n 4 runs, so ci.yml
    # selects it again. It stays deselected HERE because this lane is the one
    # with teeth: scripts/nightly_eval.sh propagates this mode's exit code into
    # its own verdict ("Exit code IS the verdict", its header), so a residual
    # rate that CI would show as one red job would instead redden the nightly
    # verdict and mask the eval signal. The measurement bounds the residual near
    # 1%, not at zero, so this waits for push-to-main history rather than for a
    # better number.
    #
    # REMOVE THIS DESELECT once main has run it clean for a while; that is a
    # one-line change here plus deleting the entry from the test above.
    #
    # The other two are not `slow` and so collect nothing here; they are listed
    # anyway because the list that drifts is the list nobody reconciles.
    echo "=== Running the nightly lane (slow or nightly) ==="
    uv run pytest -q --tb=short -n 4 -m "slow or nightly" \
      --deselect tests/test_scheduler.py::test_reanalysis_maybe_run_produces_result \
      --deselect tests/test_scheduler.py::test_reanalysis_dedup_across_runs \
      --deselect tests/test_scheduler.py::test_two_repos_run_concurrently_in_worktrees \
      "$@"
    ;;
  full|*)
    echo "=== Running full test suite ==="
    uv run pytest -q --tb=short -n auto "$@"
    run_web_tests
    ;;
esac

echo ""
echo "✓ Tests passed (mode=$MODE)"
