"""Consent-gated installer for visual-proof walks (playwright + chromium).

`nh doctor --fix-walks` owns *consent* (the ~120MB size disclosure and the
`[y/n]` prompt); this module owns *execution* only. It runs the plan
`doctor.walks_install_plan()` computes, through an injectable `runner` seam
so tests never spawn a real subprocess or download a real 120MB chromium
binary — see `tests/test_doctor_walks_provision.py`.
"""

from __future__ import annotations

import subprocess

from .doctor import walks_install_plan, walks_plan_description

#: How much of a failing step's stderr to surface — enough to diagnose,
#: short enough to never dump a whole traceback into the CLI.
_STDERR_TAIL_CHARS = 300


def install_walks(
    *,
    runner=subprocess.run,
    dry_run: bool = False,
    timeout: float = 900,
) -> tuple[bool, list[str]]:
    """Run :func:`doctor.walks_install_plan`'s steps, in order, through
    ``runner``. Returns ``(ok, messages)``; never raises.

    ``dry_run=True`` returns ``(True, description_lines)`` *without
    invoking* ``runner`` at all — this is the load-bearing guarantee behind
    both the CLI's ``--dry-run`` flag and this suite's "no real download in
    CI" requirement; it does not merely rely on a fake ``runner``.

    A failing step — ``FileNotFoundError`` (missing package manager /
    playwright entry point), ``subprocess.TimeoutExpired``, or a nonzero
    return code — stops the plan immediately and reports ``(False,
    messages)`` naming the failing command and (for a nonzero rc) a tail of
    its stderr. Per the ticket's human-gated default: no rollback of an
    earlier successful step — partial state (e.g. the package installed but
    the browser binary missing) is left in place and reported, not undone;
    a re-run of ``--fix-walks`` is idempotent and simply finishes the job.
    """
    plan = walks_install_plan()
    if dry_run:
        return True, walks_plan_description().splitlines()

    messages: list[str] = []
    for step in plan:
        cmd_str = " ".join(step)
        try:
            result = runner(
                step,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError:
            messages.append(f"FAILED: `{cmd_str}` — command not found")
            return False, messages
        except subprocess.TimeoutExpired:
            messages.append(f"FAILED: `{cmd_str}` — timed out after {timeout}s")
            return False, messages

        if result.returncode != 0:
            stderr = (getattr(result, "stderr", "") or "")[-_STDERR_TAIL_CHARS:]
            messages.append(
                f"FAILED: `{cmd_str}` (rc={result.returncode}) — {stderr}"
            )
            return False, messages

        messages.append(f"OK: `{cmd_str}`")

    return True, messages
