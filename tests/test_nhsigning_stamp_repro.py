"""Repro proof: nhSigning was stamped from the macOS plan into all three apps.

Bug: ``desktop/electron-builder.config.cjs`` stamped
``extraMetadata.nhSigning = plan.mode``, where ``plan`` is the macOS signing
plan (``signingPlan()`` over ``CSC_*``/``APPLE_*`` vars). A Windows or Linux
build run with Apple credentials in its environment therefore carried
``nhSigning: "signed"`` (or ``"notarized"``) although nothing signed that
artifact — the sibling of the ``nhCanAutoUpdate`` bug (task 3fd594f5, PR
#403). The fix lives in ``desktop/signing.cjs`` (``windowsSigningPlan()``
gains a ``mode``; new ``platformSigningMode()``/``signingStamp()``/
``assertSigningStampMatchesPlatform()``) and
``desktop/electron-builder.config.cjs`` (stamps ``signing.mode`` — the
per-platform-set stamp — instead of the bare macOS ``plan.mode``), proved by
``desktop/signing.test.mjs`` and ``desktop/updateStamp.test.mjs``.

This repo's declared profile is python-pytest, so the reproduction-test gate
(``.no_human/repro_tests.json``) runs pytest node ids, not node ids of its
own - the established way to give JS-side behaviour a pytest identity is the
same one ``tests/test_ws_reconnect_repro.py`` and
``tests/test_card_elapsed_repro.py`` use: shell out to ``node --test`` and
require success.

This is an honest fails-before/passes-after proof of the fix: on the pre-fix
tree this wrapper test file itself does not exist, so its pytest node id
cannot even be collected - the sharpest possible "fails before" for a
regression test introduced alongside its fix. Independently, running the
CURRENT ``desktop/updateStamp.test.mjs`` against a tree with the stamping
line reverted to ``nhSigning: plan.mode`` (the exact bug) also turns the
Windows/Linux ``nhSigning`` assertions red - see the "mutation: reverting
nhSigning to plan.mode" test inside that same file, which performs that
mutation on a temp copy and asserts the bug reappears.

Node absence FAILS rather than skips, same reasoning as
test_ws_reconnect_repro.py: a skip here is indistinguishable from a pass, and
node is already a hard requirement of this repo's desktop test story.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DESKTOP_DIR = REPO_ROOT / "desktop"


def test_nhsigning_stamp_js_suite_passes():
    node = shutil.which("node")
    assert node is not None, (
        "node is not on PATH, so the nhSigning per-platform-stamp fix cannot "
        "be verified; this suite deliberately fails rather than skips."
    )
    proc = subprocess.run(
        [node, "--test", "signing.test.mjs", "updateStamp.test.mjs"],
        cwd=DESKTOP_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        "desktop/signing.cjs (windowsSigningPlan().mode, platformSigningMode, "
        "signingStamp, assertSigningStampMatchesPlatform) and/or "
        "desktop/electron-builder.config.cjs (stamping signing.mode instead "
        "of the bare macOS plan.mode) failed their own node --test suite, or "
        "still stamp nhSigning from the macOS plan alone:\n"
        f"STDOUT:\n{proc.stdout[-4000:]}\nSTDERR:\n{proc.stderr[-2000:]}"
    )
