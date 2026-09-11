"""The installed wheel must be able to serve the board.

`README` calls `nh start` the primary entrypoint and `nh start` serves the React
board. `web/dist` is a gitignored build artifact, so for as long as it was not
declared as package data, `pip install no-human` produced a CLI whose main
command came up with no UI: the API answered, `GET /` returned FastAPI's bare
`{"detail":"Not Found"}`, and nothing said why.

`tests/test_api.py::test_web_dist_path_points_at_repo_web_dir` cannot catch that
— it asserts a path CONSTANT, computed inside a source checkout, and the
constant was always right there. The defect only exists once the code has been
copied somewhere else. So the test below builds the real wheel, installs it into
a genuinely clean virtualenv, starts a real HTTP server out of that venv, and
reads the bytes off a socket.

PRESENT IS NOT THE SAME AS VALID, and neither is the same as REACHABLE. Every
content check in this repo passed while the installed app answered
`/nh-mark-64.png` with 601 bytes of index.html: the favicon was broken for every
user because the file was present, built, bundled — and outside the only mounted
directory, so the SPA catch-all swallowed it. It was found by RUNNING the app,
not by inspecting it. The BYTE TIER at the bottom of this file closes the half
of that a static reader can close (`89 50 4E 47` really is at the front of every
shipped PNG, in both artefacts), and the probe's root-asset arm closes the other
half by comparing what the socket returns against what is on disk.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Fast tier — runs everywhere, including CI (which has no npm in the py job)    #
# --------------------------------------------------------------------------- #

def test_pyproject_force_includes_the_board_in_wheel_and_sdist():
    """The declaration that puts the board in the wheel.

    A forced include, not an `include`/`artifacts` glob: absence of the source
    must FAIL a release built without `npm run build`, rather than silently
    shipping the broken product again. A glob would match nothing and build a
    quiet, boardless wheel — which is the bug this file exists for.

    The board's entry moved OUT of the static `force-include` table and into
    `hatch_build.py` (2026-08-01), because a static entry is evaluated for the
    EDITABLE wheel too and made a clean clone uninstallable — `uv sync` died in
    `build_editable`. What is asserted here is that the entry still exists, with
    the same source and the same destination; what makes it version-aware is
    asserted in `tests/test_clean_clone_installable.py`.
    """
    cfg = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    targets = cfg["tool"]["hatch"]["build"]["targets"]

    # Asserted as an EXACT mapping, not per-entry. Relaxing it to per-entry
    # lookups (as this file briefly did when `migrations` was added) leaves the
    # wheel with no membership guard anywhere in the suite: a rogue entry such
    # as `"src/no_human/eval" = "no_human/leaked_eval"` becomes invisible, and
    # shipping the private eval vendor-terms supplement in every wheel is a leak
    # this repo has already had once. The sdist has a separate exact-membership
    # test (`test_sdist_members_are_exactly_the_allowlist`); the wheel has only
    # this. The cost is one line here per legitimate addition, which is the
    # guard working rather than the guard being in the way. The hook below is
    # held to the same exactness for the same reason: it is the other way a path
    # now reaches the artifact.
    assert targets["wheel"]["force-include"] == {
        "migrations": "no_human/migrations",
    }
    assert targets["wheel"]["hooks"]["custom"] == {
        "path": "hatch_build.py",
        "source": "web/dist",
        "target": "no_human/web_dist",
    }
    # The sdist keeps repo-relative paths, so a wheel built FROM the sdist still
    # finds them where the wheel target expects to read them. `uv build` does
    # exactly that, and `pip install` does it whenever it cannot use a prebuilt
    # wheel.
    assert targets["sdist"]["force-include"] == {
        "migrations": "migrations",
    }
    assert targets["sdist"]["hooks"]["custom"] == {
        "path": "hatch_build.py",
        "source": "web/dist",
        "target": "web/dist",
    }


def test_resolve_web_dist_prefers_checkout_then_package_data(tmp_path, monkeypatch):
    """Both layouts resolve, and neither can shadow the other.

    Driven against real directory trees rather than mocks: the function is
    `is_file()` on a path, so a mock would only assert that the code calls the
    mock. Each layout is built on disk and the resolver is asked to find it.

    The module is fetched with importlib, not named in a monkeypatch string:
    `no_human.api` re-exports the FastAPI instance as `app`, and that attribute
    shadows the submodule when the path is resolved attribute-by-attribute.
    """
    app_module = importlib.import_module("no_human.api.app")
    _resolve_web_dist = app_module._resolve_web_dist

    # Layout A: source checkout / frozen bundle — <root>/web/dist, reached from
    # <root>/src/no_human/api/app.py by parents[3].
    checkout = tmp_path / "checkout"
    api_dir = checkout / "src" / "no_human" / "api"
    api_dir.mkdir(parents=True)
    (checkout / "web" / "dist").mkdir(parents=True)
    (checkout / "web" / "dist" / "index.html").write_text("<html>checkout</html>")

    monkeypatch.setattr(app_module, "__file__", str(api_dir / "app.py"))
    assert _resolve_web_dist() == checkout / "web" / "dist"

    # Layout B: installed wheel — <site-packages>/no_human/web_dist. parents[3]
    # points at <venv>/lib/python3.X, outside the package, and holds nothing.
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    wheel_api = site / "no_human" / "api"
    wheel_api.mkdir(parents=True)
    (site / "no_human" / "web_dist").mkdir()
    (site / "no_human" / "web_dist" / "index.html").write_text("<html>wheel</html>")

    monkeypatch.setattr(app_module, "__file__", str(wheel_api / "app.py"))
    assert _resolve_web_dist() == site / "no_human" / "web_dist"

    # Neither present: fall back to the checkout path, so the "board was never
    # built" message names the directory a developer is expecting to see.
    bare = tmp_path / "bare"
    bare_api = bare / "src" / "no_human" / "api"
    bare_api.mkdir(parents=True)
    monkeypatch.setattr(app_module, "__file__", str(bare_api / "app.py"))
    assert _resolve_web_dist() == bare / "web" / "dist"


def test_resolve_web_dist_ignores_a_directory_with_no_index(tmp_path, monkeypatch):
    """An empty `web/dist` must not count as a board.

    `npm run build` writes into an existing directory, and an interrupted or
    cleaned build leaves the directory behind with no index.html. The old check
    was `_WEB_DIST.exists()`, which was true for that husk and mounted a board
    whose every request 404s at the filesystem layer.
    """
    app_module = importlib.import_module("no_human.api.app")
    _resolve_web_dist = app_module._resolve_web_dist

    root = tmp_path / "checkout"
    api_dir = root / "src" / "no_human" / "api"
    api_dir.mkdir(parents=True)
    (root / "web" / "dist").mkdir(parents=True)  # husk: exists, but empty

    site_pkg = root / "src" / "no_human" / "web_dist"
    site_pkg.mkdir(parents=True)
    (site_pkg / "index.html").write_text("<html>real</html>")

    monkeypatch.setattr(app_module, "__file__", str(api_dir / "app.py"))
    assert _resolve_web_dist() == site_pkg


# --------------------------------------------------------------------------- #
# Real-artifact tier — builds and installs the actual wheel                     #
# --------------------------------------------------------------------------- #

# Runs `uv build` + `uv venv` + `uv pip install` + boots a server: ~1 minute on
# a warm uv cache. Deselect with -m "not slow".
pytestmark_reason_uv = "uv is not on PATH — cannot build the wheel"
pytestmark_reason_board = (
    "web/dist is not built in this checkout, so there is no board to package. "
    "It is a gitignored build artifact; run `cd web && npm install && npm run "
    "build` first. (CI's python job has no npm, so this is expected there — "
    "the packaging claim is verified on the machine that cuts releases.)"
)

# The script that runs INSIDE the clean venv. It must not import anything from
# the repo — that is the whole point — so it is passed as source text and talks
# back over stdout as JSON.
_PROBE = r"""
import json, pathlib, re, sys, threading, time, urllib.request, socket
import uvicorn
import no_human
import no_human.config
from no_human.api.app import app, _WEB_DIST

# Reported so the caller can PROVE the temp-HOME override took. The operator's
# live worker is on the real ~/.no_human; a probe that silently fell back to it
# would be touching a database in use.
out = {"pkg": no_human.__file__, "web_dist": str(_WEB_DIST),
       "home": str(pathlib.Path.home()),
       "nh_home": str(no_human.config.NO_HUMAN_HOME)}

sock = socket.socket()
sock.bind(("127.0.0.1", 0))
port = sock.getsockname()[1]
sock.close()

cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
server = uvicorn.Server(cfg)
threading.Thread(target=server.run, daemon=True).start()

deadline = time.time() + 30
while not server.started and time.time() < deadline:
    time.sleep(0.05)
out["started"] = server.started

def get(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

status, body = get("/")
out["root_status"] = status
out["root_body"] = body.decode("utf-8", "replace")[:4000]

m = re.search(r'/assets/(index-[\w-]+\.js)', out["root_body"])
out["asset_name"] = m.group(1) if m else None
if m:
    st, data = get("/assets/" + m.group(1))
    out["asset_status"], out["asset_bytes"] = st, len(data)

out["api_404_status"] = get("/api/definitely-not-a-route")[0]

# Root-level board files, fetched over the socket and compared against the bytes
# on disk. Vite copies web/public to the ROOT of dist, not under /assets, so
# these live outside the one mounted directory; when only /assets was mounted
# they fell through to the SPA catch-all and every one of them answered with 601
# bytes of index.html. Enumerated from the installed package rather than named,
# so a root asset added later is covered the day it is added.
roots = {}
if _WEB_DIST.is_dir():
    for p in sorted(_WEB_DIST.iterdir()):
        if not p.is_file() or p.name == "index.html":
            continue
        disk = p.read_bytes()
        st, served = get("/" + p.name)
        roots[p.name] = {"status": st, "disk_len": len(disk),
                         "served_len": len(served),
                         "disk_head": disk[:8].hex(),
                         "served_head": served[:8].hex(),
                         "identical": served == disk}
out["root_assets"] = roots

server.should_exit = True
print("PROBE_JSON:" + json.dumps(out))
"""


def _run_probe(venv_python: Path, home: Path) -> dict:
    # The whole point is a CLEAN import. The developer loop this repo documents
    # (`PYTHONPATH=<checkout>/src pytest …`) would leak the checkout into the
    # probe via inherited env and make it import the repo, not the wheel — the
    # exact failure mode this test exists to catch, reported backwards.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    # `config.NO_HUMAN_HOME` is `Path.home() / ".no_human"`, and `Path.home()`
    # is `$HOME` on POSIX — so this redirects the whole no_human state directory
    # into tmp for the life of the probe. Without it the probe boots a server
    # against the REAL ~/.no_human, which on a maintainer's machine is a live
    # database another process is using. The probe reports the home it actually
    # resolved and the caller asserts on it, so this is proven per run rather
    # than assumed.
    home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    # On Windows `Path.home()` does NOT consult $HOME: ntpath.expanduser prefers
    # USERPROFILE and falls back to HOMEDRIVE+HOMEPATH, so setting HOME alone
    # left the probe resolving the REAL profile directory — precisely the
    # "boots a server against the operator's live ~/.no_human" outcome this
    # override exists to prevent. The assertion below caught it rather than
    # letting it through, which is why it is asserted per run and not assumed.
    # HOMEDRIVE/HOMEPATH are cleared as well so no fallback can reintroduce the
    # real profile if USERPROFILE is ever absent.
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
        env.pop("HOMEDRIVE", None)
        env.pop("HOMEPATH", None)
    env.pop("XDG_CONFIG_HOME", None)
    env.pop("XDG_DATA_HOME", None)
    proc = subprocess.run([str(venv_python), "-c", _PROBE],
                          capture_output=True, text=True, timeout=180, env=env,
                          cwd=tempfile.gettempdir())  # never run from the repo
    line = next((ln for ln in proc.stdout.splitlines()
                 if ln.startswith("PROBE_JSON:")), None)
    assert line, (f"probe produced no result\n--- stdout ---\n{proc.stdout}\n"
                  f"--- stderr ---\n{proc.stderr}")
    return json.loads(line[len("PROBE_JSON:"):])


@pytest.mark.slow
def test_wheel_installed_in_a_clean_venv_serves_the_board(tmp_path):
    """Build the real wheel, install it clean, and read the board off a socket.

    This is the test that would have failed before the packaging fix: every
    in-repo test passed while `pip install no-human` shipped a UI-less CLI.
    """
    if shutil.which("uv") is None:
        pytest.skip(pytestmark_reason_uv)
    if not (REPO_ROOT / "web" / "dist" / "index.html").is_file():
        pytest.skip(pytestmark_reason_board)

    dist = tmp_path / "dist"
    build = subprocess.run(
        ["uv", "build", "--wheel", "-o", str(dist)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=600,
    )
    assert build.returncode == 0, f"uv build failed:\n{build.stderr}"
    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"

    venv = tmp_path / "venv"
    mk = subprocess.run(["uv", "venv", str(venv), "--python", "3.12"],
                        capture_output=True, text=True, timeout=300)
    assert mk.returncode == 0, f"uv venv failed:\n{mk.stderr}"
    # ".exe" is required on Windows, not cosmetic. The Scripts/bin split was
    # already handled here, but the interpreter on disk is `python.exe` and
    # `uv pip install --python <path>` does a FILE-EXISTENCE check on the value
    # it is given — so the extensionless path failed with "No virtual
    # environment or system Python installation found for path ...\Scripts\python"
    # and this test could never pass on Windows. Spawning would have masked it
    # (CreateProcess resolves the extension itself); an explicit path argument
    # does not.
    venv_python = (venv / ("Scripts" if os.name == "nt" else "bin")
                   / ("python.exe" if os.name == "nt" else "python"))

    inst = subprocess.run(
        ["uv", "pip", "install", "--python", str(venv_python), str(wheels[0])],
        capture_output=True, text=True, timeout=900,
        # Do not let the repo's own venv leak in and make a boardless wheel pass.
        env={**os.environ, "VIRTUAL_ENV": str(venv)},
    )
    assert inst.returncode == 0, f"install failed:\n{inst.stderr}"

    fake_home = tmp_path / "home"
    result = _run_probe(venv_python, fake_home)

    # Really the installed copy, not the repo.
    assert str(REPO_ROOT) not in result["pkg"], (
        f"probe imported the repo, not the wheel: {result['pkg']}")
    assert "site-packages" in result["pkg"], result["pkg"]
    assert result["started"], "server never came up"

    # The temp-home override took. Asserted, not assumed: if `$HOME` stopped
    # reaching `config.NO_HUMAN_HOME` this test would start booting a server
    # against the developer's real, possibly in-use, state directory — and would
    # do it silently.
    assert result["home"] == str(fake_home), (
        f"HOME override did not take: probe resolved home to {result['home']!r}")
    assert result["nh_home"] == str(fake_home / ".no_human"), (
        f"NO_HUMAN_HOME escaped the temp home: {result['nh_home']!r}")

    # The board itself.
    assert result["root_status"] == 200, (
        f"GET / returned {result['root_status']}, body: {result['root_body'][:400]}")
    assert 'id="root"' in result["root_body"], (
        "GET / did not return the board's app shell:\n" + result["root_body"][:400])

    # index.html is worthless without the bundle it references — that is a
    # separate directory in the package and was the likelier thing to be
    # dropped, so it is fetched rather than assumed.
    assert result["asset_name"], "index.html referenced no JS bundle"
    assert result["asset_status"] == 200, (
        f"/assets/{result['asset_name']} returned {result['asset_status']}")
    assert result["asset_bytes"] > 10_000, (
        f"JS bundle was {result['asset_bytes']} bytes — truncated?")

    # The SPA catch-all must still not swallow unknown API routes.
    assert result["api_404_status"] == 404, result["api_404_status"]

    _assert_root_assets_are_served_verbatim(result)

    _assert_missing_board_is_honest(venv_python, tmp_path, fake_home)


def _assert_root_assets_are_served_verbatim(result: dict) -> None:
    """Every root-level board file must come back off the socket byte-identical.

    THE INCIDENT THIS IS FOR. `/nh-mark-64.png` answered 200 with 601 bytes of
    index.html: correct status, plausible size, and the app shell instead of the
    brand mark, because only `/assets` was mounted and Vite puts `web/public` at
    the ROOT of `dist`. A status check passes on that. A "does the file exist in
    the package" check passes on that. Comparing the served bytes with the bytes
    on disk is what does not.

    Byte equality is asserted rather than magic numbers, because it subsumes
    them: a served PNG cannot start `89504e47` and still be a different file.
    """
    roots = result["root_assets"]
    # A board with no root-level asset at all would make every loop below
    # vacuous — the classic way this guard would rot into a no-op.
    assert roots, (
        "no root-level files in the installed board — the brand marks Vite "
        "copies from web/public are gone, or the enumeration broke. Either way "
        "the root-asset arm just checked nothing.")

    broken = {
        name: info for name, info in roots.items()
        if info["status"] != 200 or not info["identical"]
    }
    assert not broken, (
        "root-level board asset(s) are not served as themselves:\n  - "
        + "\n  - ".join(
            f"/{name}: HTTP {i['status']}, served {i['served_len']} bytes "
            f"starting {i['served_head']}, but the file on disk is "
            f"{i['disk_len']} bytes starting {i['disk_head']}"
            for name, i in sorted(broken.items()))
        + "\n(the original defect: the SPA catch-all answered with index.html)")


def _assert_missing_board_is_honest(venv_python: Path, tmp_path: Path,
                                    home: Path) -> None:
    """With the board removed, `GET /` must explain itself, not just 404.

    Folded into the slow test rather than given its own, because it needs the
    same expensive clean install and simply deletes the board from it.
    """
    # A Windows venv is `<venv>\Lib\site-packages` — no `python3.x` level at all
    # — so the POSIX glob matched nothing and `next()` raised a bare
    # StopIteration that named neither the path nor the reason. Verified against
    # a real venv on this host: `.venv\Lib\site-packages\...`.
    venv_root = venv_python.parent.parent
    if os.name == "nt":
        site = venv_root / "Lib" / "site-packages"
    else:
        site = next((venv_root / "lib").glob("python3.*/site-packages"))
    assert site.is_dir(), f"no site-packages under {venv_root}"
    board = site / "no_human" / "web_dist"
    assert board.is_dir(), board
    shutil.rmtree(board)

    result = _run_probe(venv_python, home)

    assert result["root_status"] == 503, (
        "a missing board must not answer 200 or a bare 404; got "
        f"{result['root_status']}: {result['root_body'][:300]}")
    body = result["root_body"]
    # The two things a user needs: what is wrong, and that the CLI still works.
    assert "web board is not installed" in body, body[:300]
    assert "npm run build" in body, body[:300]
    assert "nh task" in body, body[:300]
    # And a real API 404 must stay a 404 rather than become the notice.
    assert result["api_404_status"] == 404, result["api_404_status"]


# --------------------------------------------------------------------------- #
# Byte tier — the shipped board's BYTES, in both artefacts that carry it        #
# --------------------------------------------------------------------------- #
#
# WHY A SEPARATE TIER AND NOT MORE ASSERTIONS IN THE SLOW TEST. The slow test
# above needs `uv`, builds a wheel and boots a server; it costs about a minute
# and is deselected by `-m "not slow"`. These read bytes off disk and cost
# milliseconds, so they run in places the slow test is skipped out of. They also
# reach an artefact the wheel path cannot see at all — see below.
#
# WHAT EACH ARTEFACT CAN AND CANNOT SEE, stated so neither is trusted for the
# other's blind spot:
#
#   web/dist (the build OUTPUT, in this checkout)
#       CAN see: `npm run build` emitting a truncated, zero-length or
#       wrong-format asset. This is the common ancestor — `hatch_build.py` copies
#       it into the wheel and `packaging/build-installer.sh` copies it into the
#       frozen bundle — so a fault here is a fault in everything downstream, and
#       naming it here says WHICH step broke.
#       CANNOT see: anything either packaging step does. A wheel that ships no
#       board at all leaves this arm green.
#
#   the .app payload (what the DMG actually hands a user)
#       CAN see: the frozen-bundle copy — `cp -R` into `packaging/dist/nh-server`,
#       electron-builder's `extraResources` copy into `Contents/Resources`, and
#       codesigning walking the bundle afterwards. This is the last place the
#       bytes exist before a user has them.
#       CANNOT see: reachability. Constraint: this tier does NOT boot the frozen
#       server (it would write to the operator's live `~/.no_human`), so it can
#       prove the PNG in the bundle is a PNG and cannot prove a request for it
#       returns it. That is exactly the half the original incident lived in, and
#       it is covered instead by `_assert_root_assets_are_served_verbatim`, which
#       compares socket bytes against disk bytes in the clean-venv probe.
#
#   the wheel's `no_human/web_dist`
#       Deliberately NOT given its own static arm. The slow test already installs
#       that wheel and fetches its assets over HTTP, which is strictly stronger
#       than reading them off disk — it sees byte validity AND routing. A static
#       arm over the same tree would only duplicate what the probe already proves.
#
# Both static arms SKIP in CI and in a fresh clone (`web/dist` and `desktop/dist`
# are gitignored). The skips name what did not run and how many files were read,
# so an artefact that quietly stopped being produced cannot read as a pass.

#: First bytes every file of this suffix must begin with. A board asset whose
#: magic number is wrong is a file the browser will refuse, however plausible its
#: name and size — the 601-byte index.html served as a PNG had a perfectly
#: sensible name and a perfectly sensible size.
_MAGIC_BY_SUFFIX: dict[str, tuple[bytes, str]] = {
    ".png": (b"\x89PNG\r\n\x1a\n", "PNG"),
    ".ico": (b"\x00\x00\x01\x00", "ICO"),
    ".gif": (b"GIF8", "GIF"),
    ".jpg": (b"\xff\xd8\xff", "JPEG"),
    ".jpeg": (b"\xff\xd8\xff", "JPEG"),
    ".woff": (b"wOFF", "WOFF"),
    ".woff2": (b"wOF2", "WOFF2"),
}

#: Suffixes the build is known to emit as text. Listed rather than inferred so
#: that a NEW binary kind appearing in the board is a red test and a human
#: decision, not a file that silently goes unvalidated. See `_UNKNOWN` below.
_TEXT_SUFFIXES = frozenset({".html", ".js", ".css", ".map", ".json", ".svg", ".txt"})

#: A JS bundle under this is a truncated or stubbed build, not a React app. The
#: real one is ~600 kB; the floor is deliberately far below that so ordinary
#: growth and ordinary trimming never touch it, and far above the few hundred
#: bytes a broken build emits.
_JS_BUNDLE_FLOOR = 100_000


def _board_problems(board: Path) -> tuple[list[str], int]:
    """``(problems, files read)`` for one built board directory.

    Returns rather than asserts so the caller can report every fault at once and
    name each file. A guard that stops at the first bad byte makes a caller fix
    one asset per run.
    """
    files = sorted(p for p in board.rglob("*") if p.is_file())
    problems: list[str] = []

    for path in files:
        rel = path.relative_to(board)
        raw = path.read_bytes()
        if not raw:
            problems.append(f"{rel}: 0 bytes — nothing shipped in this file")
            continue
        magic = _MAGIC_BY_SUFFIX.get(path.suffix.lower())
        if magic is not None:
            expect, kind = magic
            if not raw.startswith(expect):
                problems.append(
                    f"{rel}: not a {kind} — expected magic {expect.hex()}, "
                    f"file starts {raw[:len(expect)].hex()} "
                    f"({len(raw)} bytes total)")
        elif path.suffix.lower() not in _TEXT_SUFFIXES:
            problems.append(
                f"{rel}: suffix {path.suffix!r} is neither a known binary kind "
                "nor known text, so nothing here validated its bytes. Add it to "
                "_MAGIC_BY_SUFFIX with its magic number, or to _TEXT_SUFFIXES, "
                "and say which in the commit — what must not happen is a new "
                "asset kind shipping unchecked.")

    index = board / "index.html"
    if not index.is_file():
        problems.append("index.html: missing — this is not a built board")
    else:
        head = index.read_bytes()[:512].decode("utf-8", "replace")
        if not head.lstrip().lower().startswith("<!doctype html"):
            problems.append(
                "index.html: no HTML doctype — starts " + repr(head[:80]))
        if 'id="root"' not in index.read_text(encoding="utf-8", errors="replace"):
            problems.append('index.html: no id="root" mount point for the SPA')

    scripts = [p for p in files if p.suffix == ".js"]
    if not scripts:
        problems.append("no .js bundle anywhere in the board")
    else:
        biggest = max(scripts, key=lambda p: p.stat().st_size)
        size = biggest.stat().st_size
        if size < _JS_BUNDLE_FLOOR:
            problems.append(
                f"{biggest.relative_to(board)}: largest JS bundle is {size} "
                f"bytes, under the {_JS_BUNDLE_FLOOR}-byte floor — truncated or "
                "stubbed build")

    return problems, len(files)


def _assert_board_bytes(board: Path, what: str) -> int:
    problems, read = _board_problems(board)
    assert not problems, (
        f"the shipped board at {what} has {len(problems)} byte-level "
        f"fault(s) across {read} file(s):\n  - " + "\n  - ".join(problems))
    return read


def test_built_board_assets_have_valid_bytes():
    """`web/dist` — the build output every packaging step copies from."""
    board = REPO_ROOT / "web" / "dist"
    if not (board / "index.html").is_file():
        pytest.skip(
            "the board byte check DID NOT RUN over web/dist — absent from this "
            "checkout. It is a gitignored build artifact; run `cd web && npm "
            "install && npm run build` to make this check live. Reported as a "
            "skip and not a pass because 0 file(s) were actually read.")
    read = _assert_board_bytes(board, "web/dist")
    # A tripwire against the scan silently going empty: the board has always
    # carried at least the shell, a stylesheet, a bundle and the brand mark. A
    # walk that breaks, or a root that resolves somewhere with one stray file,
    # must not read as "found no faults".
    assert read >= 4, f"only {read} file(s) under {board} — that is not a board"


def _app_board() -> tuple[Path | None, str]:
    """``(board dir or None, the .app path as the packaging script declares it)``.

    The .app path is READ FROM `packaging/make-dmg.sh` rather than hardcoded, so
    a rename of the build output turns this red instead of quietly scanning a
    directory that no longer ships. Fails closed on every step.
    """
    script = REPO_ROOT / "packaging" / "make-dmg.sh"
    assert script.is_file(), (
        "packaging/make-dmg.sh is gone. Either the DMG is no longer a "
        "deliverable — delete this arm in the same commit and say so — or it "
        "moved and this reader is now checking nothing at all.")
    found = re.findall(r'^APP="\$\{ROOT\}/(?P<path>[^"$]+)"[ \t]*$',
                       script.read_text(encoding="utf-8"), re.M)
    assert len(found) == 1, (
        'expected exactly one `APP="${ROOT}/..."` declaration in '
        f"packaging/make-dmg.sh, found {len(found)}: {found}")
    rel = found[0].rstrip("/")
    assert not rel.startswith("/") and ".." not in rel.split("/"), rel
    app = REPO_ROOT / rel

    if not app.is_dir():
        return None, rel

    # electron-builder drops the frozen server under Contents/Resources as an
    # `extraResources` entry, and build-installer.sh puts the board at
    # <bundle>/web/dist inside it. Located by shape rather than by name: if the
    # resource is renamed the board is still found, and if the board has
    # genuinely stopped shipping this is RED — the .app is here, so "nothing to
    # scan" is a fault, not an absence.
    boards = sorted((app / "Contents" / "Resources").glob("*/web/dist"))
    boards = [b for b in boards if b.is_dir()]
    assert len(boards) == 1, (
        f"expected exactly one board under {rel}/Contents/Resources/*/web/dist, "
        f"found {len(boards)}: {[str(b) for b in boards]}. The .app exists, so "
        "this is a packaging change to look at and never a silent pass.")
    return boards[0], rel


def test_dmg_payload_board_assets_have_valid_bytes():
    """The board inside the .app that goes into the disk image.

    This is the copy a user actually receives, and it is three copies downstream
    of `web/dist` — `cp -R` into the PyInstaller bundle, electron-builder's
    `extraResources`, then codesigning walking the whole tree. Each of those has
    written to files in this bundle; none of them has any business changing a
    PNG's first eight bytes.
    """
    board, rel = _app_board()
    if board is None:
        pytest.skip(
            f"the board byte check DID NOT RUN over {rel} — absent from this "
            "checkout. `desktop/dist` is a gitignored build output; run `cd "
            "desktop && npm install && npm run dist:bundled` to make this check "
            "live. Reported as a skip and not a pass because 0 file(s) were "
            "actually read.")
    read = _assert_board_bytes(board, rel + "/Contents/Resources/*/web/dist")
    assert read >= 4, f"only {read} file(s) under {board} — that is not a board"
