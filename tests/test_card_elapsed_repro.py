"""Repro proof for the "long-running tasks are invisible" board finding.

Live finding: tasks ran 4-5.6h with nothing on the board saying so - the
operator found out by asking. The fix lives entirely in
``web/src/cardElapsed.js`` (elapsed-chip decision: which statuses get a chip,
the 2h/4h amber/red thresholds, "1h 42m" formatting) and its wiring into
``web/src/Board.jsx`` (a 60s client-side tick, no server polling change),
proved by ``web/src/cardElapsed.test.mjs``.

This repo's declared profile is python-pytest, so the reproduction-test gate
(``.no_human/repro_tests.json``) runs pytest node ids, not node ids of its
own - the established way to give JS-side behaviour a pytest identity is the
same one ``tests/test_ws_reconnect_repro.py`` uses: shell out to
``node --test`` and require success.

That also makes this an honest fails-before/passes-after proof of the fix
existing at all: on the pre-fix tree ``web/src/cardElapsed.js`` and its test
do not exist, so ``node --test`` cannot even find the file it's told to run
and exits non-zero - the sharpest possible "fails before" for a capability
introduced from nothing.

Node absence FAILS rather than skips, same reasoning as
test_ws_reconnect_repro.py: a skip here is indistinguishable from a pass, and
node is already a hard requirement of this repo's web test story.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "web"


def test_card_elapsed_js_suite_passes():
    node = shutil.which("node")
    assert node is not None, (
        "node is not on PATH, so the board elapsed-chip fix cannot be "
        "verified; this suite deliberately fails rather than skips."
    )
    proc = subprocess.run(
        [node, "--test", "src/cardElapsed.test.mjs"],
        cwd=WEB_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        "web/src/cardElapsed.js (elapsed chip: active-status allow-list, "
        "2h/4h amber/red thresholds, h+m formatting) and/or its Board.jsx "
        "wiring failed their own node --test suite, or the files are "
        "missing entirely (the finding's fix is absent):\n"
        f"STDOUT:\n{proc.stdout[-4000:]}\nSTDERR:\n{proc.stderr[-2000:]}"
    )
