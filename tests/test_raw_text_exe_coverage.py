r"""Every raw-text matcher that names a guarded binary strips the executable
suffix, and no legitimate command is newly denied.

Issue #305, raw-text half. The argv resolver (`exec_names.command_name`) strips
`.exe`/`.cmd`/... for `argv[0]`, but the guard also decides through RAW-TEXT
matchers that name a binary literally -- `\brm\s+`, `\bgit\s+push`,
`(?:gh|glab)\s+`, `(?:nh|no-human)\s+`. Those never see a basename, so
`rm.exe -rf /` and `gh.exe pr merge 7` walked past them. Each now carries
`exec_names.EXE_SUFFIX_RE` after the binary name.

The guard here is BEHAVIORAL rather than a scan of the regex source: for each
guarded binary and a representative deny-worthy command, the `.exe`/`.cmd`
spelling must deny wherever the bare one does. A new raw-text matcher for a
known binary that forgets the suffix reopens one of these and goes red -- which
is the "enumerate by measurement, not by eye" lesson (a source scan of the
patterns turned into a regex-of-regex that mis-read grouped alternations, so
it was dropped for this).
"""
from __future__ import annotations

import pytest

from no_human.agent import exec_names, guard
from no_human.agent.guard import evaluate


def _decide(command: str, *, is_windows: bool = False) -> bool:
    saved = guard._IS_WINDOWS
    guard._IS_WINDOWS = is_windows
    try:
        return not evaluate(
            "Bash", {"command": command},
            forbidden_paths=[], never_push_to=["main"], cwd=".", env={"PATH": ""}).allow
    finally:
        guard._IS_WINDOWS = saved


def test_the_fragment_is_derived_from_the_one_suffix_source():
    """`EXE_SUFFIX_RE` must stay in lockstep with the argv-side suffix set, or a
    suffix could be stripped for argv[0] and missed in raw text, or vice versa.
    """
    for suffix in exec_names._EXECUTABLE_SUFFIXES:
        assert suffix.lstrip(".") in exec_names.EXE_SUFFIX_RE
    # optional and non-capturing, so it splices into any pattern cleanly
    assert exec_names.EXE_SUFFIX_RE.endswith(")?")
    import re
    assert re.compile(r"\bgit" + exec_names.EXE_SUFFIX_RE + r"\s").search("git.cmd ")
    assert not re.compile(r"\bgit" + exec_names.EXE_SUFFIX_RE + r"\s").search("github ")


# One deny-worthy command per raw-text matcher, in its bare spelling. The test
# asserts the bare form denies (so the row is not vacuous) AND that each
# executable spelling of the same command denies too.
_RAW_TEXT_DENIALS = [
    "rm -rf /",                       # _RM_RF
    "gh pr merge 7",                  # _FORGE_MERGE
    "glab mr merge 12",               # _FORGE_MERGE (glab)
    "git push --force origin main",   # _GIT_DESTRUCTIVE
    "git reset --hard HEAD",          # _GIT_DESTRUCTIVE
    "git push origin main",           # whole-string never-push fallback
    "nh merge-stack run",             # _LEXICAL_MERGE_STACK
    "nh serve",                       # _LEXICAL_LIVE_SERVER
]

_SUFFIXES = [".exe", ".cmd", ".bat", ".ps1", ".com"]


@pytest.mark.parametrize("bare", _RAW_TEXT_DENIALS)
def test_every_executable_spelling_denies_wherever_the_bare_one_does(bare):
    assert _decide(bare), f"control: bare {bare!r} must deny or the row is vacuous"
    head, _, tail = bare.partition(" ")
    for suffix in _SUFFIXES:
        spelled = head + suffix + (" " + tail if tail else "")
        assert _decide(spelled), (
            f"{spelled!r} walks past the gate that {bare!r} hits -- a raw-text "
            f"matcher is missing exec_names.EXE_SUFFIX_RE")


@pytest.mark.parametrize("wrapper", ["timeout 30", "nice -n 10", "chrt 10", "setsid"])
def test_a_scheduling_wrapper_plus_exe_still_denies_the_push(wrapper):
    """The push cell reached through the whole-string fallback: a scheduling
    wrapper AND an `.exe` spelling together, which was the last-open row."""
    assert _decide(f"{wrapper} git push origin main")
    assert _decide(f"{wrapper} git.exe push origin main")


@pytest.mark.parametrize("command", [
    "git status",
    "git log --oneline -5",
    "git add .",
    "git diff HEAD~1",
    "rm build/stale.o",                 # rm, but not -rf and not tests/
    "cp dist/tool.exe /tmp/",           # a .exe operand, not the command
    "ls -la dist/git.exe",
    "cat notes-about-rm.txt",
    "python build.py --exe",
    "gh pr view 7",                     # viewing is not merging
    "gh pr list",
])
def test_ordinary_commands_are_not_newly_denied(command):
    assert not _decide(command), f"{command!r} was newly denied by the suffix widening"
