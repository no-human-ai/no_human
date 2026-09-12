"""`nh.exe approve` must reach the same verdict as `nh approve`.

Issue #305. `guard.py` resolves argv[0] with `PurePosixPath(argv[0]).name` and
compares it against bare names, so a four-character suffix walked past three
`GUARD_DESTRUCTIVE` checks -- including the two that enforce constraint #2,
the agent never merges, and the gate on pushing to a protected branch.

Not platform-gated, and that is the point: the suffix is simply never
stripped, so the bypass is spellable anywhere. On POSIX a coder would have to
write `nh.exe`, which does not exist there, so the guard fails open but the
action does not happen; on Windows it is both spellable and runnable.

The repo already knew: `_is_pkg_manager_name` routes through
`venv_install_guard._basename` precisely "so v1 and v2 agree on `pip.exe`
(issue #107)". That fix was never carried to the other name-matching sites.
"""

from __future__ import annotations

import pytest

from no_human.agent import guard

PROTECTED = ["main", "master", "release/*"]


def _verdict(cmd: str) -> bool:
    """True when the guard ALLOWS the command."""
    return guard.evaluate(
        "Bash", {"command": cmd},
        forbidden_paths=[], never_push_to=PROTECTED, env={"PATH": ""},
    ).allow


# Each pair is (bare spelling, .exe spelling) of the SAME command. The bare
# half is the control: it is a verdict this repo already commits to, so if it
# ever stops denying, these tests are measuring nothing.
# The spellings are not decoration: each cluster reaches a DIFFERENT
# name-matching site, established by reverting one site at a time and watching
# which row goes red. A bare argv[0] pair alone left 17 of 20 sites unpinned.
EQUIVALENT_PAIRS = [
    # argv[0], the approve/merge binaries — constraint #2.
    ("nh approve 7", "nh.exe approve 7"),
    ("gh pr merge 7", "gh.exe pr merge 7"),
    ("glab mr accept 12", "glab.exe mr accept 12"),
    # argv[0] with a path, and the protected-branch gate.
    ("git push origin main", "git.exe push origin main"),
    ("git push origin main", "/usr/bin/git.exe push origin main"),
    ("git push --force origin master", "git.exe push --force origin master"),
    # Wrapper-prefixed, where `timeout`/`nice` are NOT in `_WRAPPERS`, so the
    # verdict comes from the raw-text fallbacks rather than argv analysis.
    # Those name the binary directly and needed the suffix spelled out.
    ("timeout 30 git push origin main", "timeout 30 git.exe push origin main"),
    ("nice -n 10 git push origin main", "nice -n 10 git.exe push origin main"),
    # Wrappers the guard DOES peel, reaching the post-wrapper name scan.
    ("env -i git push origin main", "env -i git.exe push origin main"),
    ("sudo -H gh pr merge 7", "sudo -H gh.exe pr merge 7"),
    ("timeout 30 gh pr merge 7", "timeout 30 gh.exe pr merge 7"),
    ("xargs gh pr merge 7", "xargs gh.exe pr merge 7"),
    # A nested shell payload — the inner-command approve-binary check.
    ('sh -c "nh approve 7"', 'sh -c "nh.exe approve 7"'),
    # The filesystem-scan family, a different executable set entirely.
    ("grep -r secret /", "grep.exe -r secret /"),
    ("find / -delete", "find.exe / -delete"),
]


@pytest.mark.parametrize("bare, _exe", EQUIVALENT_PAIRS)
def test_the_bare_spelling_is_refused(bare, _exe):
    """The control. Without it a guard that denied everything would pass."""
    assert not _verdict(bare), bare


@pytest.mark.parametrize("_bare, exe", EQUIVALENT_PAIRS)
def test_the_exe_spelling_is_refused_too(_bare, exe):
    assert not _verdict(exe), exe


# The negative control for the whole change. Stripping `.exe` must not turn
# the guard into one that denies every command carrying the suffix -- these
# are ordinary read-only operations and stay allowed in both spellings.
HARMLESS = [
    "git status",
    "git.exe status",
    "gh pr view 7",
    "gh.exe pr view 7",
    "git log --oneline -5",
    "git.exe log --oneline -5",
]


@pytest.mark.parametrize("cmd", HARMLESS)
def test_an_ordinary_command_is_still_allowed(cmd):
    assert _verdict(cmd), cmd


# --- the invariant, and an honest note about what it does and does not prove -
#
# The behavioural cases above pin 5 of the 20 name-matching sites and 1 of the
# 4 raw-text fallbacks: measured by reverting each site individually and seeing
# which cases go red. The rest are not unreachable so much as REDUNDANT -- the
# guard checks the same command down several paths, so reverting one site still
# denies via another.
#
# That redundancy is a virtue in a guard and a problem for a test: it means
# per-site behavioural proof is not available for most sites. So the invariant
# itself is pinned instead. This proves UNIFORMITY -- that one helper is the
# single definition of "the executable name of a token" -- not that every site
# is individually exercised. Saying otherwise would be the kind of claim this
# repo's own docs warn about.

import re
from pathlib import Path

GUARD_SRC = Path(__file__).resolve().parent.parent / "src" / "no_human" / "agent" / "guard.py"


def test_no_site_extracts_an_executable_name_without_stripping_exe():
    """`PurePosixPath(x).name` keeps `.exe`; `_basename` strips it.

    One of them must be the only spelling in the file, or the next site added
    by copying its neighbour reintroduces issue #305.
    """
    offenders = [
        f"{i}: {line.strip()}"
        for i, line in enumerate(GUARD_SRC.read_text(encoding="utf-8").splitlines(), 1)
        if "PurePosixPath(" in line
    ]
    assert not offenders, (
        "these extract a name without stripping `.exe` — use "
        "`venv_install_guard._basename`:\n  " + "\n  ".join(offenders))


def test_no_raw_text_fallback_names_git_without_allowing_the_suffix():
    """The argv paths strip `.exe`; a regex naming the binary must spell it.

    `\\bgit\\s+push` does not match `git.exe push`, which is how
    `timeout 30 git.exe push origin main` walked past the protected-branch
    gate while its bare twin was refused.
    """
    text = GUARD_SRC.read_text(encoding="utf-8")
    offenders = [
        m.group(0) for m in re.finditer(r"\\bgit(?!\(\?:)\\s", text)
    ]
    assert not offenders, (
        "raw-text git matchers that miss `git.exe` — add "
        r"`(?:\.[Ee][Xx][Ee])?`: " + repr(offenders))
