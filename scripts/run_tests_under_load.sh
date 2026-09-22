#!/usr/bin/env bash
# Run the full test suite while the box is under synthetic CPU contention.
#
# This is the reproduction/acceptance harness for the wall-clock-assertion
# class of defect: a test that measures a duration and compares it to an
# absolute literal can go red under contended-CPU load even though the code
# under test is unchanged and correct. A green run on an idle box proves
# nothing about that class of flake — this script exists so a green run
# means something.
#
# Usage:
#   ./scripts/run_tests_under_load.sh              # full suite, under load
#   ./scripts/run_tests_under_load.sh [pytest args] # forwarded to the runner
#
# Spawns ceil(hw.ncpu / 2) `yes > /dev/null` spinners for the WHOLE run (one
# per core would starve the runner itself; half is enough to force real
# scheduling contention without making the box unusable), traps EXIT so they
# are always reaped even on failure/Ctrl-C, then runs the full suite via
# scripts/run_tests.sh (the same entry point CI uses). Prints core count,
# load level, and the pytest summary line at the end — the exact evidence
# the acceptance criteria ask for.
set -euo pipefail

cd "$(dirname "$0")/.."

cores="$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4)"
load=$(( (cores + 1) / 2 ))

spinner_pids=()

reap() {
  if [ "${#spinner_pids[@]}" -gt 0 ]; then
    kill "${spinner_pids[@]}" >/dev/null 2>&1 || true
    wait "${spinner_pids[@]}" 2>/dev/null || true
  fi
}
trap reap EXIT

echo "=== spawning ${load} CPU spinners (cores=${cores}) ==="
for _ in $(seq 1 "$load"); do
  yes >/dev/null 2>&1 &
  spinner_pids+=("$!")
done

log="$(mktemp -t run_tests_under_load.XXXXXX.log)"
echo "=== running full suite under load (tee -> ${log}) ==="
set +e
./scripts/run_tests.sh full "$@" 2>&1 | tee "$log"
status="${PIPESTATUS[0]}"
set -e

summary_line="$(grep -E '^[0-9]+ (passed|failed)' "$log" | tail -1)"

echo "=== contended-box run summary ==="
echo "cores: ${cores}"
echo "load (spinners): ${load}"
echo "pytest summary: ${summary_line}"

exit "$status"
