# no_human on Windows

The Windows client, its installer, and every place Windows forced a divergence
from the macOS build. The Mac and Windows apps are ONE product: this file exists
to record where they differ and why, so a future reader can tell a deliberate
divergence from drift.

Companion documents: `INSTALLER.md` (the install/verify story, both platforms)
and `quickstart.md` (first run). The macOS packaging is the reference
implementation; this file never restates it, only departs from it.

> **Status honesty.** Every claim below is marked with what backs it.
> **[verified]** — a command was run on real Windows and its output is quoted.
> **[unverified]** — reasoned from source, not executed. Nothing is marked
> verified on the strength of a code reading alone.

---

## 1. Audit — darwin/POSIX assumptions outside `src/`

The `src/` portability layer landed ahead of this work (platform-dispatch
process management, `icacls` fail-closed credential files, env handling, 67
tests across `tests/test_windows_portability.py` and
`tests/test_windows_credential_file.py`). This audit therefore **verifies**
`src/` rather than redoing it, and concentrates on `desktop/` and `packaging/`,
which had no Windows story at all.

Verified 2026-08-04 against the tree at branch point `d2687c8`.

### 1.1 `src/` — verified, not redone

| Check | Result |
| --- | --- |
| `tests/test_windows_portability.py` test count | 48 `def test_` |
| `tests/test_windows_credential_file.py` test count | 19 `def test_` |
| Total | **67**, matching the brief |
| `_IS_WINDOWS` branches in `src/` | 23 occurrences across 8 modules |

Modules carrying `_IS_WINDOWS`: `config.py` (5), `testing/runner.py` (4),
`cli/commands.py` (3), `agent/backend_check.py` (3), `api/app.py` (2),
`integrations/__init__.py` (2), `agent/guard.py` (2), `testing/repro_gate.py` (2).

The two patterns this work mirrors rather than reinvents:

* **Process-tree kill** — `testing/runner.py::_kill_process_tree` dispatches to
  `taskkill /F /T /PID <pid>` on Windows because `os.killpg`/`os.getpgid` do not
  exist there, and a Windows process group is not a kill target even with
  `CREATE_NEW_PROCESS_GROUP`. `cli/commands.py::_windows_try_kill` and
  `api/app.py` carry the same shape.
* **Credential file permissions** — `config.py::windows_restrict_to_owner`
  replaces the ACL via `icacls /inheritance:r /grant:r`, then **reads the ACL
  back and verifies it**, raising `CredentialPermissionError` (fail-closed) if
  any unexpected principal remains. The module comment states the reason
  plainly: on Windows `os.chmod` only toggles the read-only file attribute, so
  every `0o600` is a silent no-op.

Several of these carry an explicit `UNTESTED ON WINDOWS` marker in their
docstrings. Executing them on real Windows is part of this work — see §6.

### 1.2 `desktop/` — POSIX assumptions found

| # | Site | Assumption | Consequence on Windows |
| --- | --- | --- | --- |
| D1 | `server.mjs:96-100` `bundledNhPath()` | joins `resourcesPath/nh-server/nh` | the frozen binary is `nh.exe`; the bundled server is never found, so a packaged app silently falls back to a developer's own `nh` — or fails |
| D2 | `server.mjs:83-87` `DEFAULT_NH_PATHS` | `~/.local/bin/nh`, `/opt/homebrew/bin/nh`, `/usr/local/bin/nh` | none of these exist on Windows |
| D3 | `server.mjs:117-121` `resolveNhBin()` | `execFile(env.SHELL \|\| "/bin/zsh", ["-lc", "command -v nh"])` | there is no `/bin/zsh` and no `-lc`; the lookup throws and yields `""` |
| D4 | `server.mjs:140-147` `CLI_HINT_DIRS` | Homebrew / POSIX dirs, to make `claude` findable | the real Windows install locations are absent, so the Agent SDK's `shutil.which("claude")` can miss and every task dies while the board looks healthy |
| D5 | `server.mjs:156,161` `mergePath()` | splits and joins `PATH` on `":"` | Windows uses `;`, and `C:\...` contains a colon — splitting on `:` **corrupts** the PATH rather than merely failing |
| D6 | `server.mjs:272-274` `ensureServer()` | `detached: true` to get a process group | on Windows this detaches the console but creates nothing killable as a group |
| D7 | `server.mjs:364-369` `stopServer()` | `process.kill(-child.pid, signal)` | negative PIDs are POSIX-only; Node throws `EINVAL`. `SIGTERM`/`SIGKILL` have no Windows equivalent for a non-console child |
| D8 | `tokenStore.mjs:152-153` `writeEnvVar()` | `{ mode: 0o600 }` + `fs.chmodSync(p, 0o600)` | **silent no-op.** The credential inherits the directory ACL |

**D8 is the most serious finding in this audit**, and it is not a packaging
concern — it is a security one. `src/no_human/config.py` fail-closes on exactly
this problem for the file written by the Python side. `desktop/tokenStore.mjs`
writes **the same file** (`~/.no_human/.env`) from the Electron shell and, on
Windows, protects it with a call that does nothing. The two halves of one
product disagreed about whether that file is protected.

D5 deserves a note too: it is the one item here that is actively destructive
rather than merely inert. `"C:\\Windows;C:\\Users\\x".split(":")` yields
`["C", "\\Windows;C", "\\Users\\x"]` — a PATH rebuilt from those fragments is
garbage, so this had to be fixed before anything downstream could be trusted.

### 1.3 `desktop/` — already correct, verified not redone

| Site | Note |
| --- | --- |
| `main.mjs:598-601` | a `win32` branch already sets `titleBarStyle: "hidden"` + a themed `titleBarOverlay`, with a comment explaining that `hiddenInset` degrades to a frameless window with no controls. Left untouched |
| `electron-builder.config.cjs:75` | `adhocSeal` returns early unless `electronPlatformName === "darwin"`. Already correctly gated; no Windows `afterPack` is needed (see §4) |

Two rows that stood here in the first pass — "close-to-tray is darwin-only,
Windows gets a real close" and "`window-all-closed` quits on non-darwin,
correct Windows convention" — were **withdrawn on 2026-08-05 and the behavior
changed instead**. They described a real feature divergence, not a convention:
closing the window stopped the board polling and the notifications arriving on
exactly one platform. See §2.3.

### 1.4 `packaging/` — POSIX assumptions found

| # | Site | Assumption |
| --- | --- | --- |
| P1 | `build-installer.sh` | `#!/usr/bin/env bash`, `set -euo pipefail`, `find`/`grep`/`wc`/`du`, and `.venv/bin/pyinstaller` (Windows puts it at `.venv/Scripts/pyinstaller.exe`) |
| P2 | `build-installer.sh:100` | the build-path leak gate greps for `${HOME}`. Under Git Bash `$HOME` is `/c/Users/<user>` — a form that appears **nowhere** in a Windows-built binary, so the gate would pass vacuously while missing a real leak |
| P3 | `make-dmg.sh` | `hdiutil`/`codesign`/`notarytool`/`stapler` — macOS-only by nature. NSIS is the Windows analogue and is produced by electron-builder, so this script has no Windows counterpart |
| P4 | `nh-server.spec` | reviewed and found **platform-neutral**: no `icon`, no `BUNDLE`, no POSIX paths. `EXE(name="nh")` yields `nh.exe` on Windows automatically |

P2 is the reason the Windows build gate is a real re-implementation rather than
a thin shim: a gate that cannot fire is precisely the failure mode
`build-installer.sh`'s own comments were written to prevent ("the identity guard
in the test suite SKIPS whenever desktop/dist is absent... so it was dark exactly
when it mattered"). The Windows gate searches for the Windows-native forms of
the build path.

### 1.5 `desktop/*.test.mjs` — POSIX test fixtures

The existing Node tests build their fixtures out of POSIX primitives: `#!/bin/sh`
and `#!/usr/bin/env node` shebangs, `fs.chmodSync(..., 0o755)` to make them
executable, `/usr/bin/pkill -9 -f`, and `/bin/ps -eo pid,command`. Windows has
no shebang mechanism, no execute bit, and neither of those binaries.

This is a test-harness portability problem, not a product defect. It is reported
honestly with per-file results in §6 rather than papered over: **no existing
test was weakened, relaxed, or deleted to make a Windows run go green.**

---

## 2. What was fixed, and what it cost

### 2.1 The blocking defect: the frozen server died printing its first warning

The Windows client could not work at all before this. `nh start` raised before
binding the port, so the packaged app spawned a server that exited instantly
and the board never loaded. From the installed artifact, with one intermediate
frame inside rich's private Windows renderer elided:

```
File "rich\console.py", line 2091, in _write_buffer
   [1 frame elided: rich's private Windows renderer]
File "rich\_win32_console.py", line 441, in write_styled
File "rich\_win32_console.py", line 402, in write_text
File "encodings\cp1255.py", line 19, in encode
UnicodeEncodeError: 'charmap' codec can't encode character '\u26a0' in position 0
[PYI-28844:ERROR] Failed to execute script 'nh_entry' due to unhandled exception!
```

The CLI prints a `⚠` (U+26A0) warning; a frozen build's stdio defaults to the
machine's ANSI codepage — `cp1255` on this host, and any non-Latin codepage
would do it — and encoding U+26A0 to that raises.

Three things about this are worth keeping:

* **It reproduces ONLY in the frozen binary.** The identical command run from a
  source `.venv` boots normally. No source-side test could have caught it; it
  had to be found by running the installed artifact, which is the whole argument
  for the acceptance run in §5.
* **The two obvious environment fixes do not work.** `PYTHONIOENCODING=utf-8`
  **[verified]** and `chcp 65001` **[verified]** were both tried against the
  frozen binary and it crashed identically each time. The reconfiguration has to
  happen in-process, which is why the fix is in `packaging/nh_entry.py`.
* **Placement is load-bearing.** It sits ABOVE the `no_human` imports because
  rich captures the stream's encoding when it constructs its `Console` at import
  time. Below them it would be inert.

Fixed in `packaging/nh_entry.py`; `errors="replace"` is set as well, so a
console that still cannot render a glyph degrades to `?` rather than taking the
server down. A crash while printing a warning is strictly worse than a warning
that prints imperfectly.

**[verified]** after the fix: the frozen server boots and `GET /api/tasks`
returns **HTTP 200**, with empty stderr.

### 2.2 The security defect: the credential was not protected on Windows

`desktop/tokenStore.mjs` hardened `~/.no_human/.env` with `fs.chmodSync(p, 0o600)`,
which on Windows only toggles the read-only file attribute. Measured on this host,
a freshly written credential file carried the **inherited** ACL:

```
NT AUTHORITY\SYSTEM:(I)(F)
BUILTIN\Administrators:(I)(F)
<user>:(I)(F)
```

So the OAuth token was readable by every administrator on the machine while the
module reported it had written a `0600` file. `src/no_human/config.py` already
fail-closes on exactly this for the same file; the Electron shell did not. After
`icacls /inheritance:r /grant:r`, the readback is a single grantee **[verified]**:

```
<user>:(R,W)
```

This is a mirror of `config.windows_restrict_to_owner`, not a fork: same command
shapes, same readback verification, same fail-closed error. The DIRECTORY is
deliberately left to `config.ensure_private_dir` (called on every `nh start` via
`_acquire_pid_lock`) so one owner keeps that policy.

Node reports `mode & 0o777 == 0o666` for any non-readonly file on Windows
**[verified]**, whatever the ACL says — which is why the three `0600` assertions
became an ACL readback there. That is strictly stronger than the mode check it
replaces: it is what detected the inherited-ACL defect in the first place.

### 2.3 The parity defects: close killed the product, and the count vanished (2026-08-05)

Two places where "the Windows code path existed" concealed "the Windows user
lost a feature":

* **Closing the window quit the app.** On macOS, close hides to the tray: the
  board keeps polling, notifications keep arriving, quit is explicit. On
  Windows, `window-all-closed` quit — so the product stopped working the moment
  its window closed, on the one platform where tray-resident apps are the norm
  for this shape of tool. The first pass recorded this as "correct Windows
  convention"; that record is withdrawn. Close now hides to the tray on Windows
  exactly as on macOS (the tray, its Quit item and File→Quit already existed
  here). Linux keeps close-quits. One consequence handled with it: with a
  hidden window, relaunching the exe fires `second-instance`, and the old
  handler only called `focus()` — which does not surface a hidden window. It
  now goes through `showWindow()`. That routing brought a second case with it:
  `showWindow()` falls through to `createWindow()` when the window is gone, and
  the window is gone during the deliberately DELAYED quit (the SIGTERM→SIGKILL
  escalation), while the single-instance lock is still held — so a relaunch in
  that window would have built a fresh window against a server being torn down.
  The handler now returns early on the existing `quitting` flag, and
  `mainSecondInstance.test.mjs` pins both directions.
* **The needs-you count did not render.** The badge mirrors the web app's own
  `(N) no_human` title via `page-title-updated` — but `app.setBadgeCount` draws
  only on macOS and Unity Linux. Windows' equivalent is a taskbar overlay icon,
  and Electron does not rasterize a number for you. `badge.mjs` now draws it —
  a red disc, white 3×5-font count, `9+` past nine — as raw BGRA bytes, pure
  and electron-free so the channel order and font bits are pinned by unit tests
  rather than discovered on a live taskbar. Same parsed title, no second truth;
  the exact count always reaches the taskbar via the overlay's accessibility
  description.

---

## 3. Divergences from macOS, and why each one exists

Divergence only where an OS convention forces it. Everything else is shared.

| # | Divergence | Why it is forced |
| --- | --- | --- |
| 1 | `packaging/build-installer.ps1` is a re-implementation, not `build-installer.sh` under Git Bash | The build-path leak gate greps `$HOME`. Under Git Bash that is `/c/Users/<user>`, a form that appears NOWHERE in a Windows-built binary, so the gate would report clean while a real `C:\Users\<user>` path sat in the bundle. A gate that cannot fire is the exact defect the macOS script's own comments were written to prevent |
| 2 | The Windows leak gate also checks the REPO ROOT, and both slash spellings | pip writes `file:///C:/...` with forward slashes while compiled-in paths use backslashes; and a checkout outside the user profile (`C:\dev\...`, a build agent workspace) leaks a path a `$HOME`-only check cannot see. Strictly stronger than the macOS gate |
| 3 | `stopServer` dispatches `taskkill /F /T`, not `process.kill(-pid)` | A negative pid is POSIX-only; Node rejects it. A Windows process group is not a kill target even with `CREATE_NEW_PROCESS_GROUP`, so `/T` (walk the tree) is the only way to reap `nh`'s workers. Mirrors `testing/runner.py::_kill_process_tree` |
| 4 | The graceful stop escalates immediately on an OS liveness check | **[verified]** an un-forced `taskkill` CANNOT terminate a console process: Windows answers *"This process can only be terminated forcefully (with /F option)"*. There is no grace period actually elapsing, so waiting the caller's 10s grace would add a 10-second hang to every quit and still send exactly the same `/F` |
| 5 | The escalation trigger is a liveness probe, not the message text | `taskkill` exits **128** for BOTH "not found" and "could not terminate" **[verified]**, so the exit code cannot distinguish them, and the message is localised — a German or Japanese Windows would defeat any text match. `process.kill(pid, 0)` is language-independent **[verified]** |
| 6 | `resolveNhBin` reads `env.PATH` instead of spawning `where.exe` | The macOS login-shell trick exists because a GUI app inherits launchd's PATH and genuinely cannot see the user's. A Windows GUI process DOES inherit the user PATH, so there is nothing to recover — and reading `env` is deterministic and unit-testable, which `where.exe` (which consults the live process PATH, ignoring the injected `env`) could not be |
| 7 | `spawn(..., spawnOptionsFor(process.platform))` — win32 gets `{ detached: false, windowsHide: true }` | Covers **nh's OWN console only**. `nh` is a console-subsystem binary; on win32 `detached: true` mapped to `DETACHED_PROCESS`, which makes Windows IGNORE `CREATE_NO_WINDOW` (what `windowsHide` sets) — nh then had no console and every console-subsystem grandchild (`claude.exe`, `git.exe`) got its own visible one. Dropping `detached` on win32 lets `windowsHide` apply and the hidden console is inherited. This row's earlier claim — that `windowsHide` alone hid the console — was the defect; see `desktop/server.mjs` `spawnOptionsFor`. The grandchild-console count is a separate check that requires a running task: **§5.4** (NOT YET RUN on Windows) |
| 8 | `.ico` and `.icns` are both DERIVED, neither committed | Both are build outputs of `packaging/derive-icons.mjs`, generated at package time from `web/public/nh-mark-512.png` (the brief's `mark-dark` PNGs are **not in this repo** — verified, no file matching `mark-dark*` exists anywhere in the tree). `.icns` needs `sips`/`iconutil`, so it's macOS-only; `.ico` is dependency-free Node and runs on every platform. `packaging/make-win-icon.ps1` (deriving `.ico` from `.icns`) still exists as a Windows-native fallback for a box with no Node, but is superseded as the actual build path |
| 9 | Neither icon is git-tracked any more | `.gitignore` ignores `desktop/build/*` with no carve-out for either icon (the earlier `icon.icns` force-add is gone). `electron-builder.config.cjs` refuses to load (`process.exit(1)`) if a fresh icon isn't already on disk, so a build that skipped derivation fails loudly instead of silently shipping Electron's stock atom icon |
| 10 | Install directory is `no-human-desktop`, not `no_human` | electron-builder derives the per-user install directory from package `name`, while `productName` drives the app and registry display name. Cosmetic, recorded so it is not mistaken for a mis-install. The registry entry reads `no_human 0.1.0` |
| 11 | No `afterPack` hook for Windows | The macOS `adhocSeal` exists to repair a signature electron-builder INVALIDATES by injecting into `Contents/`. Windows has no equivalent seal to break — an unsigned `.exe` is simply unsigned, not "damaged". `adhocSeal` is already darwin-gated and needed no change |
| 12 | `nh-server.spec` is UNCHANGED | Worth recording as a NON-divergence. The brief anticipated a platform-conditional spec; **[verified]** none is needed — the unmodified spec freezes on Windows and `EXE(name="nh")` emits `nh.exe` automatically |
| 13 | The taskbar badge is a hand-rasterized overlay icon | Windows has no dock badge and `app.setBadgeCount` draws nothing there; `win.setOverlayIcon` is the platform's API and Electron does not render a count into it for you. Same title-derived source of truth as the dock badge — only the last rendering step diverges (§2.3) |
| 14 | `build-installer.ps1` refuses a stale checkout, like `build-installer.sh` | A NON-divergence, recorded because the two must refuse identically: the guard the macOS pipeline gained after the stale-DMG incident (checkout behind `origin/main` ⇒ refuse; `NH_ALLOW_STALE_BUILD=1` only for a deliberate point-in-time artefact) is mirrored, with one Windows-only wrinkle — it runs under `ErrorActionPreference: Continue` because PS 5.1 turns redirected native stderr into a terminating error, and an offline `git fetch` must not fail a build |
| 15 | Close-to-tray is now SHARED, not a divergence | Recorded because the first pass documented the opposite. See §2.3 |

### 3.1 Parity that is now enforced by tests, not convention

`desktop/packagedFiles.test.mjs` gained four parity tests, because the realistic
way these two stop being one product is not a decision — it is someone fixing a
Windows problem by adding a `win.extraResources`, which works, ships, and
silently gives the platforms different payloads:

* `extraResources` and `files` must stay TOP-LEVEL; a platform-scoped copy
  REPLACES the shared list for that platform and is asserted absent.
* Both `mac.target` and `win.target` must be non-empty, or "parity" is vacuous.
* The updater feed must be emitted for both: `zip` for `latest-mac.yml`,
  `nsis` for `latest.yml`.
* ONE version source — no `version`/`buildVersion` on either platform block and
  no `extraMetadata.version`; and `pyproject` and `desktop/package.json` must
  agree, so `nh --version` inside the installer cannot drift from the installer.

Plus a test that `win.icon` exists and is a REAL `.ico` — parsed as bytes
(ICONDIR reserved/type/count) with a 256×256 entry required by NSIS, because a
0-byte or PNG-named-`.ico` placeholder would satisfy a path check and still
produce a broken installer.

---

## 4. Test results on real Windows

### 4.1 `desktop/` — `npm test`

**224 → 243 tests. No test was removed, weakened, or relaxed.**

| | Before | After |
| --- | --- | --- |
| Total | 224 | **243** |
| Passing | 196 | **230** |
| Failing | 28 | **12** |
| Skipped | 0 | 1 |

The 12 remaining failures are all in `mainWiring.test.mjs`, and they are a
TEST-HARNESS limit rather than a product defect: that file builds its fixtures
from `#!/usr/bin/env node` shebangs, `chmod 0o755`, `/usr/bin/pkill -9 -f` and
`/bin/ps -eo pid,command`. Windows has no shebang mechanism, no execute bit, and
neither binary. It is reported here rather than exempted, and §5's acceptance run
covers the same territory (spawn, credential save, quit, orphan check) against
the REAL app instead of a fake `nh`.

What was ported rather than exempted, so the same assertions now run on Windows:

* The fixture helper delivers one JS body per platform — POSIX keeps the shebang
  script, Windows writes a `.js` and launches `node` with it. `ensureServer`
  already takes `nhArgs`, so nothing in the code under test changed. Node 22
  refuses to spawn `.cmd`/`.bat` without `shell: true` (the CVE-2024-27980
  mitigation), which is why a `.cmd` shim was not used.
* `uiPages.test.mjs` resolved `node_modules/.bin/electron`, an extensionless
  shell script — `ENOENT` on Windows took the whole FILE out. Now
  `require("electron")`, which returns the real binary path on every platform.
* `setupGate.test.mjs` used `path.join("/apps", ...)`, absolute on POSIX but
  DRIVE-RELATIVE on Windows, so `pathToFileURL` anchored it to the current drive
  and the round-trip could never match. The predicate itself was proven correct
  on Windows independently **[verified]** — it accepts the real setup file with
  and without a query string and rejects board URLs, siblings and strict-prefix
  lookalikes. This one mattered: `isSetupUrl` is the gate on the credential IPC.
* `updateState.test.mjs` made a directory unwritable with `chmodSync(dir, 0o500)`,
  a no-op on Windows, so the write SUCCEEDED and the test failed on the very
  platform it was checking robustness for. It now puts a FILE where the directory
  must be, which is genuinely unwritable on both.

**One test is skipped on Windows, with its reason recorded in the file:** the
EPIPE/`capture.stop()` test. Its guarantee rests on SIGPIPE and it must use a
SHELL fixture — a shell dies on the next write to a broken pipe, while Node
silently swallows EPIPE. Windows has neither SIGPIPE nor shell scripts, so the
only available Windows fixture is the node one that test's own comment rejects
as unable to detect the defect. A ported version would be a test that cannot
fail, which is worse than an honest skip. The code it guards has no Windows
branch, so the macOS run governs it on both.

### 4.2 `pytest` — the four named suites, complete

**86 tests: 72 passed, 13 failed, 1 skipped** on the first full real-Windows run.
Five of the thirteen were then fixed; the rest are analysed below rather than
suppressed.

This is the first time the portability layer has actually EXECUTED on Windows,
and it found that several of its own tests could not run there. Each docstring
in `config.py` and `testing/runner.py` carrying `UNTESTED ON WINDOWS` was
accurate in a stronger sense than intended.

#### Fixed here (5)

| Test | Why it could not run on Windows |
| --- | --- |
| `test_kill_process_tree_uses_taskkill_on_windows` | Monkeypatched `os.killpg` as a TRIPWIRE, but `os.killpg` does not exist on Windows and `monkeypatch.setattr` refuses a missing attribute — so **the one test proving the Windows branch reaches `taskkill` was the one test that errored on Windows**, during setup, before its body ran. Fixed with `raising=False`; the tripwire stays armed |
| `test_kill_process_tree_still_killpgs_on_posix` | Same, for `os.getpgid`/`os.killpg`, plus `signal.SIGKILL` |
| `test_try_kill_still_signals_on_posix` | `signal.SIGKILL` missing on Windows → `AttributeError` at `cli/commands.py:7118` — the exact shape the neighbouring `test_stop_path_never_names_sigkill_at_module_scope` exists to fence |
| `test_atomic_write_0600_posix_does_not_shell_out` ×2 (both suites) | Named `_posix` but never pinned `_IS_WINDOWS = False`, so on a Windows host it took the WINDOWS branch, called `_run_icacls`, and **tripped its own tripwire**. It asserted a POSIX property while letting the host decide the branch |
| `test_wheel_installed_in_a_clean_venv_serves_the_board` | The `Scripts`/`bin` split was handled, but the interpreter on disk is `python.exe` and `uv pip install --python <path>` does a FILE-EXISTENCE check, so the extensionless path failed with *"No virtual environment or system Python installation found"*. Spawning would have masked it; an explicit path argument does not. **[fix unverified]** — this test builds a wheel and a clean venv and was too long to re-run here |

The common shape: the file gates Windows branches behind a patchable
`_IS_WINDOWS` so "the Windows branches are reachable (and therefore testable)
from any platform". That symmetry only ever held in ONE direction. Nothing was
weakened — every assertion is unchanged; only constants the platform does not
define are supplied, and branch selection is pinned instead of inherited from
the host.

#### A real product finding, NOT from this branch (3)

`backend_check` on Windows resolves the Agent SDK's BUNDLED CLI ahead of the
documented fallback list:

```
expected: <tmp>\AppData\Roaming\npm\claude.cmd
actual:   .venv\Lib\site-packages\claude_agent_sdk\_bundled\claude.exe
```

`shutil.which` is never consulted at all —
`test_which_is_asked_for_the_bare_name_on_windows` asserts `[] == ['claude']`.
The packaged product may therefore run a bundled `claude` rather than the
operator's own. **This needs a decision, not a test edit**, so it was left
failing and is reported here.

#### Genuinely host-dependent, left failing (4)

`test_atomic_write_0600_posix_still_chmods` and
`test_secure_credential_file_chmods_on_posix`, in both suites. They assert
`stat().st_mode & 0o777 == 0o600`. Windows does not model POSIX permission bits
at all — Node and Python both report `0o666` for any non-readonly file
**[verified]** — so no amount of branch-pinning makes this pass on this
filesystem. Faking the assertion would be weakening it; they are left failing
and explained instead. They pass on macOS and in CI, which is where the POSIX
path actually ships.

#### Result after the fixes

**86 tests: 77 passed, 8 failed, 1 skipped** — down from 13 failures. Measured
per suite, because the combined four-suite invocation is unreliable in this
harness (see below):

| Suite | Result |
| --- | --- |
| `test_wheel_ships_board.py` | **5 passed, 1 skipped — 0 failed** |
| `test_onboard.py` | **13 passed — 0 failed** |
| `test_windows_credential_file.py` | 17 passed, 2 failed (both chmod) |
| `test_windows_portability.py` | 42 passed, 6 failed |

`test_wheel_ships_board.py` went from failing to fully green, and it alone
surfaced THREE distinct Windows bugs, each hidden behind the previous one:

1. `uv pip install --python <path>` does a file-existence check and the
   interpreter is `python.exe` — the extensionless path failed outright.
2. The temp-`HOME` override did not take, because `Path.home()` on Windows reads
   `USERPROFILE`, not `$HOME`. **This is the one that mattered**: the probe was
   resolving the operator's REAL `~/.no_human` and would have booted a server
   against a live database. The test's own assertion caught it — it asserts the
   override "per run rather than assumed", and that design is what stopped it.
3. `<venv>\Lib\site-packages` has no `python3.x` level, so the POSIX glob matched
   nothing and `next()` raised a bare `StopIteration` naming neither path nor
   reason.

#### The remaining 8, none of them silent

* **4 × chmod** (`..._posix_still_chmods`, `test_secure_credential_file_chmods_on_posix`,
  in both suites) — assert `st_mode & 0o777 == 0o600`. Windows does not model
  POSIX permission bits; faking the assertion would weaken it. Left failing.
* **3 × bundled CLI** — the `backend_check` finding above. Needs a decision.
* **1 × `test_gitlab_probe_does_not_name_usr_bin_true_on_windows`** — asserts
  `GIT_ASKPASS not in env`. It IS in the environment here, because the harness
  running the suite injects it (`CLAUDECODE=1`, `AI_AGENT=claude-code_…`). An
  ambient-environment artifact of how this run was driven, not a Windows issue
  and not a product defect; it would pass from an ordinary terminal. Recorded
  rather than "fixed", because the test is right and the environment is unusual.

#### A harness caveat worth recording — RESOLVED 2026-08-05, see §4.3

Invoking `test_windows_portability.py` as a whole file reproducibly stopped
after ~40 of its 48 tests with a `KeyboardInterrupt` raised inside
`threading.py`'s `Thread.join`, and it repeatedly killed the session driving it.
The first pass recorded it as NOT understood. It is now fully understood, fixed,
and proven fixed — the interrupt was a **console Ctrl-C the suite delivered to
itself**, arriving asynchronously so it surfaced tests later at a misleading
location. Root cause, fix and the proof run are in §4.3; the mechanism took
down two more sessions before it was traced.

---

### 4.3 Second pass on the rebased tree (2026-08-05)

The branch was rebased onto main `2de485a` (34 commits: Backlog page, Linear
listing route, Monday integration, the "In progress" rename, the Jira Forge
DROP, the two-input export gate) and everything below was re-measured on this
machine. Toolchain note: the system Node was v18.12.1 — below the repo's
`>=22.12` engines floor and old enough that `node:module.register` does not
exist, so every `main*.test.mjs` file errored at import. **Installed: Node
v22.23.2 win-x64** (portable zip, SHA-256 verified against nodejs.org's
`SHASUMS256.txt`, at `~/.local/node22`); every build and test below ran under
it.

#### The suite that was killing its own session — root cause

`test_probe_pid_still_uses_signal_zero_on_posix` pins `_IS_WINDOWS = False` and
then ran the REAL `os.kill(os.getpid(), 0)`. On a Windows host, signal 0 IS
`signal.CTRL_C_EVENT` (their values are both 0), and `os.kill` implements it
with `GenerateConsoleCtrlEvent` — **a Ctrl-C broadcast to the console process
group, the caller's own console included**. Delivery is asynchronous, which is
why the `KeyboardInterrupt` landed tests later inside `Thread.join` and the
first pass could not line the crash up with its cause. Attached to a terminal,
the broadcast also killed everything else on that console: the session driving
the suite died this way **three times** (once in the first pass, twice here),
and one of those broadcasts SIGINT-cancelled an electron-builder run that
happened to share the console.

The fix keeps both halves of the test honest: the mocked half (branch wiring,
`(pid, 0)` argument shape) runs on every host; the real-signal half — an OS
guarantee, not product code — runs only where signal 0 means "probe": POSIX.
The product's `_probe_pid` docstring, which attributed Windows `os.kill` to
`TerminateProcess` for ALL signals, is corrected too; that wrong belief is why
no reader suspected a "liveness probe" could broadcast Ctrl-C.

**[verified]** after the fix, the whole file runs attached to the session's own
console: `6 failed, 42 passed in 3.05s`, session intact — the first time this
suite has ever completed as a single invocation on Windows.

#### pytest, the four named suites, re-measured

| Suite | Result | Notes |
| --- | --- | --- |
| `test_onboard.py` + `test_windows_credential_file.py` | 30 passed, 2 failed | the 2 are the documented host-dependent POSIX-chmod assertions |
| `test_wheel_ships_board.py` | **5 passed, 1 skipped, 0 failed** | the first pass's `[fix unverified]` wheel fix is now verified on Windows |
| `test_windows_portability.py` | 42 passed, 6 failed | 2 chmod + 3 bundled-CLI (needs a product decision, unchanged) + 1 `GIT_ASKPASS` injected by the driving harness |

#### `desktop/` — `npm test`: 243 → 250 tests, 247 passing

Movement against the first pass, none of it by weakening: **+7 tests** (4 pin
the overlay badge's pixels down to BGRA channel order, 1 pins the off-macOS
File menu carrying every recovery path, 1 pins that every `dist` script names
the real electron-builder config, 1 arrived with main's menu changes), and two
harness defects fixed that the first pass's tally had been absorbing:

* **The electron stub recorded `loadFile` paths with `split("/")`** — a POSIX
  basename. On Windows the full backslashed path passed through, so every
  basename assertion in `main*` failed. `path.basename` instead. Five tests
  recovered.
* **Every `main*` fixture redirected `HOME`; `os.homedir()` on Windows reads
  `USERPROFILE`.** The fixtures' isolation silently did not take, and
  `nh:save-token` tests **wrote through to the operator's real `~/.no_human`**
  — a fixture token landed in the real `.env` and the real `config.yaml` was
  flipped to `api_key` mode. Verified as fixture literals and removed with the
  operator's explicit authorization. The fixtures now set both variables. This
  is the JS twin of the wheel-suite's `Path.home()` finding in §4.2.

The two remaining failures are the POSIX-fixture class: `mainWiring.test.mjs`
(its fake-`nh` fixtures are shebang scripts; the module now dies at import with
"The system cannot find the path specified") and `mainSupersede`'s
spawn-counting test (its fixture must be directly spawnable AND self-logging;
Windows offers no such fixture — a `.cmd` is refused by Node 22's
CVE-2024-27980 mitigation without `shell: true`, which the product rightly does
not use). Both are reported, not exempted; §5's acceptance run covers the same
territory against the real binaries.

#### The shadow-config incident, and its guard

`package.json` held a minimal `build` key — the single source for `files`,
`require`d by `electron-builder.config.cjs`. But under that NAME the list is
itself a config: electron-builder reads a `build` key out of package.json
whenever `--config` is absent, and this one has no `extraResources`, no platform
blocks and no `artifactName`. A bare `npx electron-builder --win` produced a
default-named installer **22 MB lighter than the real one — an app with no
server in it — and every step reported success**. The same shape as the
stale-DMG incident: a clean build of the wrong config.

Fencing the npm scripts does not close it, because `npx electron-builder`, an
IDE task and a future CI step never go through a script. **The key is therefore
renamed `nhPackagedFiles`** — a name electron-builder does not look for. The
list is byte-identical and `electron-builder.config.cjs` reads it from the new
key, so the packaged file set is unchanged; what changes is that an
unconfigured invocation now finds no config at all and fails outright instead of
half-building. Two guards back it: `packagedFiles.test.mjs` fails if a `build`
key reappears in `desktop/package.json`, and the existing check that every
`dist` script's electron-builder invocation names the real config.

---

## 5. Acceptance run — the installed artifact on this machine

Every line below is from a real run of the SHIPPED installer on Windows 11
(26200), not from a dev build. **[verified]** throughout.

### 5.1 Provenance — one commit, one payload

Built from commit `ca63399`, board and frozen server from the same tree:

| Artifact | SHA-256 |
| --- | --- |
| `nh.exe` (frozen server) | `55B550CBC739723C8B75DC29D2B358F3E3E8F4D0A2B5F4D8DA3F76E898D84A1E` |
| `web/dist` board (29 files, manifest digest) | `4DD25C79CD726DC3C72D2D5CAE8814EF55B3495C7E67183DE6A0B131B9FE255E` |
| `no_human-0.1.0-UNSIGNED.exe` (NSIS) | `C2BA6C82E99068FC3FB16FACF9E0C22F8CDFFF99C2E123E2B3FF58B16BA7A12C` |
| `no_human-0.1.0-UNSIGNED.zip` | `838257024694D8A7F8AA9225ED2ADD294B8D96F5A01A08426B79B7EC61733B29` |

The board digest is `sha256` over `"<relative path> <sha256>"` for all 29 files,
sorted by path — a directory-order-independent identity for the whole board.

### 5.2 Build gates

```
OK: packaging\dist\nh-server (42.9 MB), 0 .py files, no ci_gate, no build-path leak
  • building  target=zip   file=dist\no_human-0.1.0-UNSIGNED.zip
  • building  target=nsis  file=dist\no_human-0.1.0-UNSIGNED.exe  oneClick=true perMachine=false
  • building  block map    blockMapFile=dist\no_human-0.1.0-UNSIGNED.exe.blockmap
```

`latest.yml` is emitted, satisfying the updater contract the macOS block meets
with `latest-mac.yml`:

```yaml
version: 0.1.0
files:
  - url: no_human-0.1.0-UNSIGNED.exe
    sha512: AZxOkv11lcgGEtLlbcpHQjvUD4cBA2fxl4RANnpW4lXAn15AI44HQBmM+l/1jJedUjsVfTKnC0LuZ+dFk9dJaQ==
path: no_human-0.1.0-UNSIGNED.exe
```

### 5.3 The frozen binary, outside the repo, with no toolchain

Run from `C:\` with the bundle copied outside the checkout, no `uv`, no venv:

```
nh --version   -> nh, version 0.1.0
nh --help      -> Usage: nh.exe [OPTIONS] COMMAND [ARGS]...
nh doctor      -> coding backend - claude CLI: ...\.local\bin\claude.EXE
                  mechanism liveness (lifetime firings)   [all 17 mechanisms listed]
                  no contradictions, no evidence gaps
                  EXIT CODE: 0
```

On a first run every mechanism reads `0 ... last: never` with its hint attached
(`zero = ...`), which is doctor's form of "skipped, with a reason" — it reports
liveness, it does not fabricate activity. Advisories never affect the exit code.

### 5.4 Install → launch → board → quit → uninstall

| Step | Result |
| --- | --- |
| Fresh install (`/S`) | installs to `%LOCALAPPDATA%\Programs\no-human-desktop`, no admin/UAC (`perMachine=false`) |
| Registry | `no_human 0.1.0` |
| Version parity ON the installed artifact | shell `no_human.exe` = `0.1.0.0`; bundled `nh.exe --version` = `nh, version 0.1.0` |
| Payload present | `resources\nh-server\nh.exe`, `...\web\dist\index.html`, `...\migrations`, `resources\app.asar`, `resources\docs\quickstart.md`, `LICENSE.electron.txt`, `LICENSES.chromium.html` |
| Launch | 4 × `no_human.exe`, and **1 × `nh.exe` spawned from the bundle** — proving `bundledNhPath` resolves `nh.exe` and `windowsHide` suppresses the console window |
| Board | `GET /api/tasks` → **HTTP 200**; `GET /` → **HTTP 200**, 612 bytes, React root present |
| Clean quit (`WM_CLOSE`, not a kill) | `no_human.exe` remaining: **0**; `nh.exe` remaining: **0** |
| `tasklist` after quit | `INFO: No tasks are running which match the specified criteria.` for both images |
| Uninstall (`/S`) | install dir removed, registry entry removed, Start Menu shortcut removed |
| User data | `~/.no_human` **preserved** — correct; an uninstall must not delete the operator's config and task history |

**Grandchild consoles while a task runs — the step the table above never
exercised.** The rows above start the server and quit; they never add a task, so
the console-subsystem *grandchildren* nh spawns (`claude.exe`, `codex.exe`,
`git.exe`) were never on screen when the count was taken. That gap is exactly
where two real users saw "multiple empty claude terminals on top of other
screens". The verification is:

1. Launch the app **from Explorer** (double-click the shortcut), NOT from a
   terminal — a terminal-launched `nh` lends its own console to every child and
   masks the bug.
2. Add a task and let it reach the coder/reviewer stage so `claude.exe` (or
   `codex.exe`) is actually running.
3. While it runs: `tasklist /v | findstr /i "claude codex cmd"`.
4. Count the **visible** console windows on screen: **expected 0**. Before this
   fix (`detached: true` on win32, which makes Windows ignore `CREATE_NO_WINDOW`)
   there was **one empty `claude.exe` console per coder/reviewer session**, plus
   a brief flash from the SDK's `claude -v` probe.

**NOT YET RUN.** This step gates the 0.1.6 Windows walk and has not been executed
on a Windows machine. The mechanism is stated in row 7 of the defect table in
§2 — `DETACHED_PROCESS` makes Windows ignore `CREATE_NO_WINDOW`, so a detached
`nh` had no console and its console-subsystem grandchildren each got a visible
one; dropping `detached` on win32 lets `windowsHide` apply and the hidden
console is inherited. Microsoft's ["Process Creation
Flags"](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags)
is the upstream source for that interaction: `CREATE_NO_WINDOW` "is ignored if
the application is not a console application, or if it is used with either
CREATE_NEW_CONSOLE or DETACHED_PROCESS". That row covers `nh`'s own console,
which is what reaches `claude.exe`; nh's other subprocesses (`git`, `codex`, the
test runner) do not depend on it, because `proc.py:hidden_console_kwargs` passes
`CREATE_NO_WINDOW` on each of those spawns directly. The unit tests are
`desktop/server.test.mjs` / `tests/test_proc.py`; the decisive console-count
repro above is what actually establishes the pop-ups are gone, and until it runs
the fix is "mechanism-verified", not "confirmed on Windows".

### 5.5 Signing and SmartScreen — stated honestly

The installer is **not signed**. Verified directly rather than assumed:

```
Get-AuthenticodeSignature no_human-0.1.0-UNSIGNED.exe
  Status : NotSigned
  SignerCertificate : <none>
```

electron-builder logs `signing with signtool.exe` lines during the build; those
are attempts that produce no signature when no certificate is configured, and
the check above is what settles it. The artifact filename carries `-UNSIGNED`
for the same reason the DMG does: so nobody can upload it believing it is
shippable.

**What this means for a user, and the limit of what was tested.** An unsigned
executable that carries a Mark-of-the-Web — i.e. one DOWNLOADED from a browser
or received over a network — triggers the SmartScreen prompt *"Windows protected
your PC — Microsoft Defender SmartScreen prevented an unrecognised app from
starting"*, and the user must click **More info → Run anyway**. That prompt was
**NOT observed during this acceptance run**, and the reason is specific: the
installer was executed from a local build path with no Mark-of-the-Web, and
silently (`/S`). So SmartScreen behaviour on a genuinely downloaded copy is
**[unverified]** here and is stated as an expectation, not as an observation.
It should be confirmed once a real download path exists. Code signing plus
reputation is what removes it; that is a separate piece of work.

### 5.6 What was NOT verified

Stated plainly rather than implied by omission:

* **A trivial task reaching the intake screen.** The board loads and serves, but
  driving a task through intake needs a working Claude credential in the GUI.
  Handling the operator's credential is not something this work should do, so
  the acceptance chain stops at "board loads". The `nh.exe` process was observed
  spawning, serving and being reaped — the packaging-level guarantees — but the
  end-to-end task path on Windows remains **[unverified]**.
* **SmartScreen on a downloaded copy** — see §5.5.
* **Auto-update.** `latest.yml` is emitted and correct, but no update has been
  installed on Windows. `nhCanAutoUpdate` stays `false` for an unsigned build,
  so the shipped app will not offer one. NSIS differs from Squirrel.Mac in that
  it CAN install an unsigned update, but shipping an update path nobody has
  watched work is exactly the "guessing wrong" the config header rejects.
  `latest.yml` IS uploaded as a release asset (CI's "Upload the packages"
  step, next to the exe/zip): it enables in-app update checks to report available versions,
  even though the install path stays refused.
* **The full pytest suite** — see §4.2.

---

### 5.7 Second-pass acceptance on the rebased tree (2026-08-05) — SUPERSEDES §5.1–§5.5

Everything in §5.1–§5.5 was measured on the artifact built from `ca63399`,
BEFORE the rebase onto main `2de485a`; those artifacts no longer exist. The
shipped artifact is now built from `69fcb99` via `npm run dist:win:bundled`
(the sanctioned pipeline — staleness guard passed, all bundle gates green,
`SIGNING: UNSIGNED — NOT SHIPPABLE` stamped honestly).

#### Provenance — one commit, one payload

The walkthrough below found two shell defects and one board display defect,
fixed in the commits that follow `69fcb99`; the FINAL artifact carries them:

| Artifact | SHA-256 |
| --- | --- |
| `nh.exe` (frozen server) | `0CB7C0513D53A466E6486D3E6DE3B36585F6185D6913DB8B7FCF3AB34ECB16F0` |
| `web/dist` board (29 files, manifest digest) | `9607B89DB60434FA03D51138CB1270A6731CA137C25DE8116D130853B1E3E56A` |
| `no_human-0.1.0-UNSIGNED.exe` (NSIS) | `5248510406DC05D9EE98B0F59BA8C3876BD569B49DE90D2FFAC988893D63AAFD` |
| `no_human-0.1.0-UNSIGNED.zip` | `993EC970E59292996F5DBD91D5A01D772A5E03506B5CC00A44DD401C852D794A` |

The installed payload's stale-build greps were re-run on the final artifact:
`Backlog` present, `In progress` present, the removed banner absent.

#### The stale-build gate — the INSTALLED payload's own bytes

The check the stale-DMG incident mandates, run against the installed tree at
`%LOCALAPPDATA%\Programs\no-human-desktop\resources\nh-server\web\dist\assets`,
actual grep output:

```
installed board JS: index-BAwgcCMI.js
Backlog                             -> 4
In progress                         -> 1
need their test command proven      -> 0 (ABSENT)
```

The installed `nh.exe`'s SHA-256 equals the built bundle's hash above, its
`--version` reports `nh, version 0.1.0`, and the installed shell
`no_human.exe` carries FileVersion `0.1.0` — one version, one payload.
`scripts/verify_artefact.py` was NOT used: it lives on
`feat/artefact-build-stamp`, which has failed two reviews and is not on main;
the greps above are the same check done by hand.

#### `nh doctor` without a credential — a delta from §5.3, explained

§5.3 recorded `doctor` exiting 0. On this fresh install, with no
`CLAUDE_CODE_OAUTH_TOKEN` anywhere, it exits **1**:
`✗ CODING BACKEND UNUSABLE: no OAuth token on file — … Create one with:
claude setup-token`. Both records are correct: §5.3 ran with a credential
present; a doctor that reports an unusable backend as healthy would be the
lie. The mechanism-liveness advisories still print with their zero-hints
either way.

#### Live walkthrough on the installed app (operator present)

* **First-run gate**: launch with no token → 4 × `no_human.exe`, **zero**
  `nh.exe` — the shell refuses to spawn a server before a credential exists,
  exactly what `mainWiring`'s precondition pins. The window shows
  "no_human — connect Claude".
* **Title bar** **[verified visually]**: min/max/close render in the
  `titleBarOverlay`, dark-theme symbol colors, at 200 % DPI.
* **Onboarding**: Welcome → You → Repositories → … → Rules review → Launch all
  render and advance. The repository scan accepted a real Windows path
  (`C:\Users\<user>`) and found the repos — but a full-home scan is SLOW
  (minutes, `AppData`/`node_modules` are in the walk) with only a spinner; a
  user can reasonably believe it hung. Recorded as a UX note, not a defect.
* **Rules review fail-closed**: candidate rules surface UNTICKED and inactive,
  matching the screen's own contract.
* **Server, once connected**: `nh.exe` bound to `127.0.0.1:8420` (loopback
  only); `GET /api/tasks` → 200; `GET /` → 200; the NEW Linear listing route
  `GET /api/integrations/linear/issues` → **503**, which is its defined
  "not configured" answer — the rebased main's routes are live in the
  installed server.
* **Updater on an unsigned build**: the feed check runs, gets 404 from the
  private releases URL, logs it loudly, and does not disturb the app.
  `nhCanAutoUpdate` stays false; expected until signing + a real feed exist.
* The "first-launch anomaly" (a launch with no visible window) RESOLVED on a
  longer measurement: the window is revealed on first paint (`show: false` +
  `ready-to-show`), and the first paint waits for the server's cold boot —
  the window appeared **~12–13 s after launch**, twice measured. Correct
  behavior, real UX cost: a first-run user stares at nothing, with no splash
  and no taskbar presence, long enough to conclude the app is broken and
  launch it again. Recorded as a known issue; a pre-paint splash or an eager
  reveal with a loading state is the likely shape of a fix.

#### What the walkthrough caught — three defects, fixed and re-proven

The full-flow pass (operator present, driving alongside) earned its keep:

1. **`+ New Task` rendered UNDER the Windows window controls** — found by the
   operator. The board's shell accommodations were mac-sided only (top-left
   traffic-light clearance); Windows' `titleBarOverlay` occupies the top-RIGHT,
   where the board puts its New Task button — a clipped, unclickable sliver.
   Fixed sided: the preload now exposes `platform`, the board toggles
   `nh-shell-win32`, and the main bar clears the overlay via
   `env(titlebar-area-width)` (150 px fallback). Verified in pixels on the
   reinstalled artifact: the button renders fully left of min/max/close and
   opens the intake modal.
2. **Relaunching the app did not surface a tray-hidden window.** With
   close-to-tray, "launch it again" is how users reopen the app; the
   `second-instance` handler only called `focus()`, which does nothing for a
   hidden window — measured: process count moved, screen did not. It now goes
   through `showWindow()`. Verified: close → 0 windows, 4 processes,
   `nh.exe` alive, `/api/tasks` 200 → relaunch → window surfaces.
3. **The application menu bar is unreachable on Windows** — `titleBarStyle:
   "hidden"` renders no menu bar and Alt does not reveal one, so File→Quit and
   friends are mouse-unreachable. Not fixed here, documented instead: the
   accelerators all work (`Ctrl+N`, `Ctrl+1..4` verified live), quit is the
   tray menu's job (its native context menu renders outside the frameless
   window), and the board's own Settings modal carries Updates and Account.
   If a future pass wants the full menu on Windows, a hamburger in the
   overlay strip is the conventional shape.

Also verified live on the installed app: board columns and empty states,
Backlog page (unconfigured-tracker state matching its 503 API), Stats empty
state, the Settings modal (Projects/Rules/Skills/Learnings/Integrations/
Account/Updates), intake modal via both `Ctrl+N` and the button. The overlay
badge could not be observed live — it needs a needs-you count, which needs a
real task; its rendering is pinned by the BGRA pixel tests instead, and the
title-parse path it shares with the dock badge is exercised by the live board
title.

---

## 6. The export guard on a Windows checkout — read this before re-approving

`scripts/export_guard.py verify` reports **"content changed since approval"** for
several hundred files that this branch never touched. That is a Windows checkout
artifact, and **the correct response is to do nothing about it**.

Git for Windows ships `core.autocrlf=true` at SYSTEM level **[verified]** on this
host. Files are converted to CRLF on checkout, so a file that is byte-identical
in the repository has a different hash on disk here than the pin recorded on
macOS. Evidence:

* `git status` reports **0 modified files** under `web/src` while the guard
  reports dozens of them changed — git normalises, the guard hashes raw bytes.
* `web/src/titleCase.js` in the working tree: **8 CRLF, 0 bare LF**.
* The blob git actually stores for an edited file contains **no CR** — verified
  by reading the staged object back with `git cat-file`.
* `git diff --numstat` for the files this branch edits shows small, surgical
  counts (e.g. `222 / 19` for `server.mjs`), not the whole-file churn a real
  line-ending change would produce.

So the repository content is correct and the macOS build is unaffected.

**What was approved here: exactly the four new files this branch adds** —
`desktop/build/icon.ico`, `docs/WINDOWS.md`, `packaging/build-installer.ps1`,
`packaging/make-win-icon.ps1` — plus their classification rules in
`EXPORT_CLASSIFICATION.txt` with counts, in the same commit.
`RELEASE_MANIFEST.txt` went from 932 to 936 pins.

**What was deliberately NOT done: a bulk re-approve.** Running `approve` over the
CRLF-mismatched files on this host would rewrite hundreds of pins with
CRLF-derived hashes and break the manifest for macOS and for CI — turning a local
display artifact into a real, shipped defect. "A changed file loses its approval"
is a rule about CONTENT changing, and the content did not change. Re-approving
them would be the "acknowledge blindly" failure the guard exists to prevent.

If a future Windows contributor wants a clean `verify` locally, the fix is to
make the checkout match the repository rather than to move the pins:

```powershell
git config --local core.autocrlf false
git rm --cached -r .
git reset --hard        # re-checks-out every file with LF, discarding local edits
```

That was NOT done here, because it rewrites every file in the working tree and
this branch had uncommitted work in it at the time.

**Postscript, 2026-08-05: the recipe above was applied.** The tree was clean
after the first pass merged its work, so the checkout was switched to
`core.autocrlf false` and re-checked-out. Two things confirm it worked, both
run afterwards:

* `export_guard.py approve --all` re-pinned **exactly the files this branch
  adds or edits and nothing else** — every untouched file's disk hash matched
  the pin recorded on macOS, which is only possible on an LF-faithful checkout.
* `verify` on a clean worktree: `OK — 934 shipped file(s) == 933 pin(s) + the
  manifest, all hashes match, 114 dropped, 0 unclassified`, exit 0.

So `approve` is now safe to run on this host, and this branch's pins were
re-derived here rather than carried around the CRLF problem. One habit the
tooling still requires: `_write_pins` writes the manifest through Python text
mode, which emits CRLF on Windows — normalize the file to LF before committing
(`sed -i 's/\r$//' RELEASE_MANIFEST.txt`) or the byte-for-byte diff against
main becomes a whole-file rewrite.
