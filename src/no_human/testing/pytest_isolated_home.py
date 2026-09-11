"""Session-scoped pytest plugin: redirect ``HOME`` before ``no_human.config``
can bind ``DB_PATH``/``CONFIG_PATH`` to it.

THE GAP THIS CLOSES. ``config.NO_HUMAN_HOME = Path.home() / ".no_human"`` is
evaluated once, at import time (``src/no_human/config.py``). A per-test
fixture — even an autouse one — runs its body inside a test, which is always
after collection has already imported every test module and, transitively,
``no_human.config``. By then ``DB_PATH``/``CONFIG_PATH`` are already bound to
the operator's real ``~/.no_human``, and no ``monkeypatch`` in a fixture body
can un-bind them; ``tests/conftest.py``'s ``isolated_env_file`` fixture can
only redirect ``ENV_PATH`` because ``load_env_var`` re-reads that particular
path at CALL time, not import time. Nothing else about the module works that
way. A branch carrying a migration then replays it into the operator's live
database the moment the suite opens any ``Store`` bound to ``config.DB_PATH``
— this happened for real on 2026-08-10 (an additive column; the next one may
not be additive).

The only lever that is early enough is the ``HOME`` environment variable,
because ``Path.home()`` resolves it via ``os.path.expanduser("~")`` at CALL
time — but ``config.py`` calls it at MODULE level, so ``HOME`` must be set
before ``import no_human.config`` runs anywhere, including transitively
through some other test module's imports. pytest plugins registered via a
``pytest11`` entry point (or ``-p``) load before test collection begins and
before ``pythonpath`` from ``[tool.pytest.ini_options]`` is even applied
(``pytest_load_initial_conftests`` runs after plugin import) — this is the
earliest hook pytest offers, which is why this lives here and not in a
fixture.

SELF-GUARD. This module ships inside the installed wheel (it is not excluded
from the build — see ``pyproject.toml``), and a ``pytest11`` entry point
auto-loads for EVERY pytest invocation in any environment where ``no-human``
is installed, including a third party's own project. Redirecting a
stranger's ``HOME`` out from under their own suite would be unacceptable, so
the redirect only ever activates when ``_repo_root_or_none()`` finds this
exact repository above the current working directory. Everywhere else the
module registers as a normal (empty) plugin and does nothing — verified by
``pytest_report_header`` naming its own inactivity.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# The operator's actual home, captured before anything below can redirect
# HOME out from under it. Once the redirect has run, `Path.home()` can no
# longer answer "what was the real HOME" — tests that need the truth (to
# prove the operator's live db was left alone) read this constant instead.
#
# Deliberately NOT `pwd.getpwuid(...).pw_dir`: `pwd` resolves through NSS,
# which on a directory-service-backed machine can genuinely query a remote
# server — a network-capable import has no place in this table's "opens and
# accepts nothing" guarantee. `Path.home()` is also exactly what config.py
# itself uses, so REAL_HOME and the value config would have bound are
# derived identically. This still runs before `_activate()` can redirect
# HOME below, so it still captures the real value.
REAL_HOME = Path.home()

# Set below, by `_activate()`, iff this run is this repo's own suite. `None`
# means the plugin loaded (it always does) but chose to do nothing.
ISOLATED_HOME: Path | None = None

# Explicit escape hatch (`=0` disables the redirect entirely) and pin (a
# developer-supplied HOME to reuse across runs instead of a fresh temp dir).
_ENABLED_VAR = "NH_TEST_HOME_ISOLATION"
_PINNED_HOME_VAR = "NH_TEST_HOME"


def _repo_root_or_none() -> Path | None:
    """Return this repo's root iff it is findable above the current cwd.

    Walks `Path.cwd()` and up to 4 parents looking for a `pyproject.toml`
    that declares `name = "no-human"` with a sibling `tests/conftest.py` —
    both together, so an unrelated project that happens to depend on
    `no-human` and also has a `tests/conftest.py` of its own does not match.
    """
    here = Path.cwd()
    for candidate in (here, *here.parents[:4]):
        pyproject = candidate / "pyproject.toml"
        conftest = candidate / "tests" / "conftest.py"
        if not (pyproject.is_file() and conftest.is_file()):
            continue
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            continue
        if 'name = "no-human"' in text:
            return candidate
    return None


def _seed(home: Path) -> None:
    """Give the isolated HOME what the suite already assumes it has.

    `config.ensure_private_dir` rejects a `.no_human` directory looser than
    0700 (config.py), so tests that reach it before `Store.connect()` calls
    that helper themselves need it pre-existing and private. A `.gitconfig`
    with a committer identity is seeded because several tests shell out to
    `git commit` and rely on a global identity when they don't set one
    locally themselves (e.g. tests/test_build_info.py,
    tests/test_resume_branch_point.py) — a HOME with no identity at all would
    make those fail with "Please tell me who you are" regardless of what
    they're actually testing.
    """
    no_human_dir = home / ".no_human"
    no_human_dir.mkdir(mode=0o700, exist_ok=True)
    no_human_dir.chmod(0o700)
    gitconfig = home / ".gitconfig"
    if not gitconfig.exists():
        gitconfig.write_text(
            "[user]\n"
            "\tname = no_human test suite\n"
            "\temail = test-suite@no-human.invalid\n"
            "[init]\n"
            "\tdefaultBranch = main\n"
        )


def _activate() -> Path | None:
    if os.environ.get(_ENABLED_VAR) == "0":
        return None
    if _repo_root_or_none() is None:
        return None

    # Only past the self-guard is a late redirect a real bug rather than an
    # unrelated pytest session that never wanted one. Ordered AFTER the
    # repo-root check on purpose: a foreign suite that happens to have
    # imported some other package's `no_human.config`-alike first must never
    # crash on this plugin's own guard — it would already have been a no-op
    # one line earlier.
    if "no_human.config" in sys.modules:
        raise RuntimeError(
            "no_human.testing.pytest_isolated_home imported after "
            "no_human.config was already imported. HOME must be redirected "
            "before config.py binds DB_PATH/CONFIG_PATH at import time, or "
            "this suite can write into the operator's real ~/.no_human. "
            "Something imported no_human.config before this plugin — check "
            "pytest11/-p plugin ordering."
        )

    pinned = os.environ.get(_PINNED_HOME_VAR)
    if pinned:
        home = Path(pinned)
        home.mkdir(parents=True, exist_ok=True)
    else:
        home = Path(tempfile.mkdtemp(prefix="nh-test-home-"))

    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    if os.name == "nt":
        os.environ["HOMEDRIVE"] = home.drive
        os.environ["HOMEPATH"] = str(home)[len(home.drive):]

    _seed(home)

    if not pinned:
        atexit.register(shutil.rmtree, home, ignore_errors=True)

    return home


ISOLATED_HOME = _activate()


def pytest_report_header(config):  # noqa: ARG001 - pytest hook signature
    if ISOLATED_HOME is not None:
        return f"no_human isolated HOME: {ISOLATED_HOME}"
    return (
        "no_human isolated HOME: inactive (not running no_human's own "
        "suite — see no_human.testing.pytest_isolated_home)"
    )


def pytest_unconfigure(config):  # noqa: ARG001 - pytest hook signature
    if ISOLATED_HOME is not None and not os.environ.get(_PINNED_HOME_VAR):
        shutil.rmtree(ISOLATED_HOME, ignore_errors=True)
