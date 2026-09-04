# no_human on Linux

The Linux client (`.deb` primary, AppImage secondary, x86_64 — files named `linux-amd64.deb` and `linux-x86_64.AppImage`, each format's own arch convention), how it is
built, and every place Linux forced a divergence from the macOS build. The
Mac, Windows and Linux apps are ONE product: this file exists to record where
the Linux one differs and why, so a future reader can tell a deliberate
divergence from drift. `WINDOWS.md` is its sibling and its shape.

Companion documents: `INSTALLER.md` (the install/verify story, all platforms),
`quickstart.md` (first run), `DISTRIBUTION.md` (release channels). The macOS
packaging is the reference implementation; this file never restates it, only
departs from it.

> **Status honesty.** Every claim below is marked with what backs it.
> **[verified]** — a command was run on real Linux (or, where stated, on the
> macOS host for a platform-neutral fact) and its output is quoted.
> **[unverified]** — reasoned from source, not executed. Nothing is marked
> verified on the strength of a code reading alone. Sections 6 and 7 were
> tables to be FILLED by runs, and the rule while they were empty was that the
> Linux app is not OFFERED FOR DOWNLOAD anywhere. **That condition was met on
> 2026-08-19**: §6 carries a green CI run and §7 a real Ubuntu 24.04 desktop
> walk, and the app is now a release asset (`v0.1.0`), a pointer object each
> for the .deb and the AppImage, and a site button routed to the signed-in
> download.

---

## 1. Audit — what already holds on Linux, and what does not

Verified 2026-08-18 against main `eda12c9d3` by reading the code on the macOS
host and running the desktop suite there (`node --test`, 331 → 340 tests as
this work landed). Nothing in this section has run on Linux yet.

### 1.1 `src/` — the POSIX path, verified not redone

`docs/WINDOWS.md §1.1` established the `_IS_WINDOWS` split (23 branches, 67
tests). Linux takes the POSIX side of every one of them: the credential file is
`chmod 600`'d by `config.py`, process trees are killed by process group in
`testing/runner.py`, and `integrations/__init__.py` honours `XDG_CONFIG_HOME`
for the `gh` config. **[verified on macOS host, by grep — not on Linux]**:
`grep -rn darwin src/` → one line, `src/no_human/eval/northstar.py:430` (a
Mac-only bench helper); `grep -rn "/opt/homebrew" src/` → the Codex CLI hint
paths in `agent/codex_backend.py:115` only.

### 1.2 `desktop/` — Linux was the explicit "else" branch

| # | Site | Behaviour on Linux | Status |
| --- | --- | --- | --- |
| L1 | `server.mjs:97-101` `POSIX_NH_PATHS`, `:207-214` `POSIX_CLI_HINT_DIRS` | `/usr/local/bin`, `~/.local/bin`, `~/.npm-global/bin`, `~/.claude/local`, `~/.yarn/bin` are appended to the spawned server's PATH when they exist AND are not already on it (`mergePath` skips both); `/opt/homebrew/bin` is skipped because `mergePath` drops non-existent dirs | [unverified] |
| L2 | `server.mjs` `bundledNhPath` / `resolveNhBin` | the PACKAGED app resolves the frozen `nh` inside `resources/nh-server` first (`bundledNhPath`), so the login-shell lookup (`$SHELL -lc "command -v nh"`) is only reached by an unpackaged dev run — the same as macOS | [unverified] |
| L3 | `tokenStore.mjs:36,201` | POSIX branch: `~/.no_human/.env` written at mode 0600 | [unverified] — the CI driver asserts it (§6) |
| L4 | `main.mjs:828` close handler | closing the window **quits** (darwin/win32 hide to tray; Linux does not) | [unverified] — see §3 #1 |
| L5 | `main.mjs:841` `setBadgeCount` | wrapped in try/catch ("linux without libunity") — no badge | [unverified] |
| L6 | `main.mjs:87` `trayIcon()` (routing at `:95`) | was left on the macOS template mask ("unverified there"); **now routed by `!== "darwin"`** so Linux gets the real-colour glyph — see §2.1 | [verified on macOS host: `trayIconRouting.test.mjs` 6/6] |
| L7 | `electron-builder.config.cjs` | had NO `linux` block — the app was never packaged for Linux; the icns freshness check was already darwin-gated so the config loaded on ubuntu runners | [verified on macOS host: config had `mac`, `win` only] |

### 1.3 `packaging/` — platform-neutral, verified not redone

| Site | Note | Status |
| --- | --- | --- |
| `build-installer.sh` | bash; `.venv/bin/pyinstaller`; `shasum` OR `sha256sum` (the stamp block names "the Linux/Windows parity this stamp is meant to cover"); the build-path leak gate greps `${HOME}/`, which on a runner is `/home/runner/` — a real form on Linux, unlike Git Bash's `/c/Users` on Windows (`WINDOWS.md §1.4 P2`), so the gate can fire | [unverified] until the first CI run |
| `nh-server.spec` | `EXE(name="nh")` emits `nh` on Linux; no platform branch (`WINDOWS.md §3 #12`) | [unverified] |
| `derive-icons.mjs` | derives `.ico` and (new) `.png` on every platform; `.icns` SKIPped off macOS | [verified on macOS host: `deriveIcons.test.mjs` 11/11] |

---

## 2. What was added, and what it cost

### 2.1 The Linux package (2026-08-18, this change)

* `desktop/electron-builder.config.cjs`: a `linux` block — `deb` + `AppImage`,
  `arch: ["x64"]`, `icon: "build/icon.png"`, `category: "Development"`,
  `executableName: "no_human"`, `maintainer: "no_human <support@getnohuman.com>"`,
  `artifactName: "${productName}-${version}-linux-${arch}.${ext}"`, and
  no `desktop.entry` — electron-builder writes Comment from
  `linux.description` and Categories from `linux.category` (an entry value
  for either is silently discarded — read in `LinuxTargetHelper.js`), and
  StartupWMClass is left to `desktopName` + `syncDesktopName` because an entry
  value would WIN and could then disagree with the app_id Electron sets on
  the running window.
  Shares the top-level `files`,
  `extraResources` and `extraMetadata` with `mac` and `win`.
* `packaging/derive-icons.mjs`: also writes `desktop/build/icon.png` — a
  byte-copy of `web/public/nh-mark-512.png`, never re-encoded (a re-encode
  rolls new 3-byte scanner coincidences — the reason the icons stopped being
  committed). `--verify` refuses when it is missing, stale, or not the master.
* `desktop/main.mjs` `trayIcon()`: `process.platform !== "darwin"` gets the
  real-colour glyph (Windows and Linux); only macOS keeps the template mask.
* `desktop/packagedFiles.test.mjs`: the four `parity:` tests iterate
  `["mac", "win", "linux"]`; two new tests pin the Linux icon bytes, targets,
  arch, artefact name, executable name, maintainer shape and menu category.
* `desktop/package.json`: `dist:linux`, `dist:linux:bundled`; `homepage`
  (the `.deb` target refuses to build without one) and `desktopName:
  "no_human.desktop"` (Electron's app_id / WM_CLASS on Linux, matched by
  `linux.syncDesktopName: true` so the launcher entry and the running window
  are associated).
* `packaging/linux-acceptance.mjs` + `desktop/linuxAcceptance.test.mjs`: the
  Lane-A driver (§6) and the unit tests over its argument/token contract.
* `.github/workflows/ci.yml`: the `linux` job (§4).

**Findings on the way, worth recording.** A dry run of `npm run dist:linux`
on the macOS host with a stub server payload (throwaway, never shipped)
**[verified on macOS host, 2026-08-18]** established: (1) `${arch}` renders
per format's convention — the AppImage builds as
`no_human-0.1.0-linux-x86_64.AppImage`, the deb resolves to
`no_human-0.1.0-linux-amd64.deb` (`builder-util/arch.js
getArtifactArchName`), so every glob and doc spells them that way; (2) the deb
target aborts with *"Please specify project homepage"* without a `homepage` in
`package.json`; (3) electron-builder warns that without `desktopName` +
`syncDesktopName` "desktop environments may not link running windows to this
.desktop entry"; (4) the AppImage tool cannot run on an arm64 Mac (`spawn
Unknown system error -86`) and `fpm` needs `xz` for the deb's control tarball
— both host limitations that the ubuntu runner does not have, which is the
reason the build lives in CI (§3 #8). The deb's declared `Depends` (from
the same run): `libgtk-3-0 libnotify4 libnss3 libxss1 libxtst6 xdg-utils
libatspi2.0-0 libuuid1 libsecret-1-0`, Recommends `libappindicator3-1`.
Also: The first `maintainer:` value was
the founder's personal address; the repo's own scrub gate (`export_guard.py
approve`) REFUSED the file — a personal mailbox inside a config that ships in
every `.deb` is exactly what the gate exists to catch. It became a role address.
**[verified on macOS host: gate output quoted in the branch's commit trail]**

Cost: 9 desktop tests added (331 → 340; the parity file alone 26 → 29 test blocks), 0 relaxed, 0 deleted; the manifest
and export classification re-pinned in the same commits (packaging `*.mjs`
2 → 3, desktop `*.mjs` 50 → 51).

---

## 3. Divergences from macOS, and why each one exists

Divergence only where an OS convention forces it. Everything else is shared.

| # | Divergence | Why it is forced | Status |
| --- | --- | --- | --- |
| 1 | Close QUITS; there is no tray-resident close | Electron's tray on GNOME needs an AppIndicator extension and is unreliable across desktops; a close that hides into a tray the user cannot see would strand a running server. Kept until the tray is walked on GNOME **and** KDE (§7), then re-decided — the Windows history (`WINDOWS.md §2.3`) shows this can flip once measured | [unverified] |
| 2 | Tray icon is the real-colour glyph, not the template mask | `setTemplateImage` is a no-op off macOS; the mask paints as literal black pixels (the same defect Windows had) | [verified on macOS host: routing test] |
| 3 | No dock/taskbar badge | `app.setBadgeCount` renders on Unity only; there is no `setOverlayIcon` equivalent outside Windows | [unverified] |
| 4 | Install path `/opt/no_human`, binary `no_human`, package name `no-human-desktop`, `.desktop` file `no_human.desktop` | electron-builder derives the package name from `name` in `package.json` (`WINDOWS.md §3 #10`) and installs Linux apps under `/opt/<productName>` | [unverified] |
| 5 | Two formats, `.deb` first | `.deb` installs from a double-click, registers a menu entry with the brand icon, and its postinst does the sandbox work an AppImage cannot (row 5a). AppImage needs `libfuse2`/`libfuse2t64` on Ubuntu ≥ 22.04 and can hit 24.04's unprivileged-userns rule; electron-builder's answer is split — the generated `.desktop` Exec is `AppRun --no-sandbox %U` (`AppImageTarget.js`; only while no `toolsets.appimage` pin is set), while `AppRun` itself adds the flag only when its `unshare -Ur true` probe fails (`appImageUtil.js`) — see §5 | [unverified] — Lane B row 11 measures both |
| 5a | The `.deb` postinst installs an **AppArmor profile** to `/etc/apparmor.d/no_human` (Ubuntu 24.04+; skipped where the parser rejects the abi/4.0 profile, e.g. 22.04, "the app runs fine without it" there) and chmods `chrome-sandbox` **4755 only where unprivileged user namespaces are unavailable, 0755 otherwise** | `app-builder-lib/templates/linux/after-install.tpl` (read 2026-08-18) — the mechanism that makes Electron's sandbox work on 24.04 without SUID; no macOS/Windows counterpart. An operator debugging the sandbox message should look for the profile, not for a 4755 mode | [unverified] |
| 5b | Both Linux artefacts carry `LICENSE.electron.txt` and `LICENSES.chromium.html` (~14 MB) TWICE — once at the app root (electron-builder keeps them off macOS) and once under `resources/` from the shared `extraResources` entry that exists because the macOS packager DELETES them | The entry is shared on purpose (parity test: `extraResources` is never per-platform); the cost is size, not correctness. Recorded, and a candidate for a mac-only carve-out ONLY if the parity test is taught the exception | [unverified] size on a real build |
| 5c | The `.deb` **declares** its runtime deps (`deb.depends` = electron-builder's default nine + `libgbm1` + `libasound2t64 \| libasound2`); the **AppImage does not** — it relies on the host already having the GTK/ATK/ALSA stack (`libgtk-3-0`, `libatk1.0-0`, `libasound2t64`), which every Linux desktop has but a minimal/headless box does not | electron-builder's default `deb.depends` omitted `libgbm1` and ALSA (both `DT_NEEDED` by the Electron binary, neither pulled transitively by `libgtk-3-0`), and bare `libasound2` is a **virtual-only** name on Ubuntu 24.04 that resolved to a stub missing `snd_device_name_get_hint@ALSA_0.9` — fixed to the alternative form. So `apt install ./no_human.deb` works on a clean 24.04 box; the AppImage there dies on the first missing host lib (`libatk-1.0.so.0`) and needs the desktop lib set. **This is a core reason the `.deb` is the primary format and the AppImage the fallback.** | **[verified on Linux, AWS EC2 Ubuntu 24.04, 2026-08-27]**: `.deb` clean-install (apt auto-pulled `libasound2t64`+`libgbm1`) + launch GREEN; AppImage failed on `libatk` on the bare box, launched and rendered the onboarding once the desktop libs were present |
| 6 | Not signed, and the filename does not say `-UNSIGNED` | "signed" is not a property of a `.deb`/AppImage (apt repositories are signed, packages are not); the tag would import a Gatekeeper/SmartScreen meaning. `SHA256SUMS-linux.txt` on the release + the site pointer's sha256 carry integrity, exactly as for Windows | decision (§2.1) |
| 7 | No auto-update | `nhCanAutoUpdate` is `false` on every platform today; electron-updater on Linux supports AppImage only; `latest-linux.yml` is produced for inspection and NEVER uploaded to a release | decision (§2.1) |
| 8 | Built on a GitHub `ubuntu-22.04` runner, not on a machine we own | PyInstaller freezes for the OS it runs on and there is no Linux machine on the desk (the Windows build needed a second person and a hand-written prompt). The runner is reproducible and is the glibc floor: 2.35 → Ubuntu 22.04+, Debian 12+, Fedora 36+ | decision (§2.1) |
| 9 | `.icns` is not derived on the runner | `sips`/`iconutil` are macOS-only; the Linux block never asks for it | [unverified] until the first CI run |
| 10 | x86_64 only | arm64 is one matrix line once x64 is proven (`ubuntu-24.04-arm`) and is an explicit optional phase, not a silent half-ship | decision (§2.1) |

### 3.1 Parity that is enforced by tests, not convention

`desktop/packagedFiles.test.mjs` — the same four `parity:` tests that keep
mac and win one product now iterate three platforms: shared `extraResources`
and `files` (a per-platform copy REPLACES the shared list), a non-empty target
list per platform, the updater feed emitted for each (`zip` → `latest-mac.yml`,
`nsis` → `latest.yml`, `AppImage` → `latest-linux.yml`), one version source.
Plus: the Linux icon is the master's bytes; the Linux targets are exactly
`[deb, AppImage]` on `x64`; the artefact name carries `linux-${arch}`.
**[verified on macOS host: 36/36 in `packagedFiles.test.mjs`]**

---

## 4. Build — the CI job

`.github/workflows/ci.yml` job `linux` (`ubuntu-22.04`, 45-minute timeout;
every push to `main`, opt-in `linux` label on PRs, and `workflow_dispatch`
ONLY with the `linux_release` input — a bare dispatch is the nightly guards'
handle and neither runs this job nor produces packages):

1. `uv sync --frozen` (15-minute step bound; a cache-cold sync measured
   7.32s locally on 2026-09-04, and `uv lock --check` resolved in 3ms,
   ruling out lock drift — the real failures are a GitHub-runner registry
   stall this sandbox cannot reproduce). The first attempt gets nearly the
   whole 15-minute budget (~14 minutes) so it alone can absorb a diagnosed
   6-12 minute sustained stall, instead of being capped below it by a small
   per-attempt timeout and retried into an identical failure — a contended
   registry does not get faster on retry. A second attempt only fires, with
   whatever budget remains, if the first attempt failed FAST with a real
   (non-timeout) `uv` error; the uv cache itself is keyed by `uv.lock` via
   `setup-uv`'s `enable-cache`. On final failure the step prints the last 60
   log lines and a `uv lock --check` drift probe before exiting non-zero, so
   the real error is never hidden behind a bare timeout. Then
   `npm ci` runs in `web/` and `desktop/` (a FULL desktop
   install — electron-builder needs the Electron binary).
2. `bash packaging/build-installer.sh` — the frozen server, with every gate
   the macOS build has (0 `.py`, no `ci_gate`, no private term inventory, no
   build path, `BUILD_STAMP`).
3. `npm run dist:linux` → `desktop/dist/no_human-0.1.0-linux-amd64.deb`,
   `….AppImage`, `latest-linux.yml`, `linux-unpacked/`.
4. `scripts/verify_artefact.py desktop/dist/linux-unpacked/resources/nh-server
   --repo . --repo-built-this-artefact` on the unpacked tree — the stamp and
   the board digest are checked against the checkout that built them. That
   comparison is self-referential by construction, so the script downgrades
   the verdict to rc=3 ("provenance NOT verified — every check that ran
   passed"); the job accepts exactly 3 and fails on any other non-zero, the
   same rule `packaging/make-dmg.sh` applies to every unsigned build.
   (`--expect-commit` together with `--repo-built-this-artefact` is a usage
   error, rc=2 — `INSTALLER.md`'s exit-code table.)
5. `sudo apt-get update && sudo apt-get install -y xvfb` (the deb's Depends
   are fetched from apt, and the runner image's lists are stale), then
   `sudo apt-get install -y ./…deb`; `/opt/no_human/resources/nh-server/nh --version`
   from `/`.
6. `npm install -g @anthropic-ai/claude-code` — the REAL Claude Code CLI,
   installed the way §5 tells a user to. `nh start` refuses to serve without
   it (`_assert_backend_usable`), so a job that skipped this step could never
   reach the board; a stub would only prove the refusal is bypassable. No
   token is validated and no task is started in this job.
7. `packaging/linux-acceptance.mjs` on `/opt/no_human/no_human` under
   `xvfb-run` with ONE throwaway `HOME` (§6), launched SANDBOXED
   (`chromiumSandbox: true` — Playwright would otherwise inject `--no-sandbox`
   on Linux), then again on the extracted AppImage's `AppRun`, whose own
   userns probe therefore really runs.
8. `apt-get remove no-human-desktop` keeps that same HOME's
   `~/.no_human/.env` and `no_human.db` — the files the app actually wrote,
   not a fresh directory the package never touched.
9. `sha256sum` → `SHA256SUMS-linux.txt`. Two run artefacts: the EVIDENCE
   (`linux-x86_64-evidence-<sha>`: sums, `BUILD_STAMP`, screenshots; every
   run, 14 days) and the PACKAGES (`linux-x86_64-<sha>`: `.deb`, AppImage,
   `latest-linux.yml`; ONLY on a `workflow_dispatch` run with `linux_release=true`, 7 days). The split
   is deliberate: this workflow ships to the public repo, where a run artefact
   is downloadable by any signed-in GitHub user, and a `.deb` from a random
   main push would be an unsigned, never-walked app on offer — the exception
   the status block promises does not exist. Nothing is published from CI.

Cost: the heaviest ubuntu job in the file (`uv sync`, two `npm ci`, a
PyInstaller freeze, an Electron package, two Xvfb app runs) — expected in the
15–25 billed-minute range per run **[unverified until §6's "billed minutes"
row is filled]**; a warm `uv sync` (cache hit, keyed by `uv.lock`) does not
change this, it only raises the ceiling a stalled/cold sync can survive
before the step fails loudly instead of exhausting the job's 45-minute
budget. It runs on main pushes and opt-in PRs, like `windows`, for
the same billing reason. `ubuntu-22.04` is a pinned hosted-image label:
if GitHub retires that image the job stops scheduling rather than failing —
re-check the runner-image retirement schedule before this becomes the
release path, and move the floor deliberately (it is a glibc decision).

A **release** build is the same job triggered by `workflow_dispatch` with
`linux_release=true` on the merged `main` sha; the operator downloads the artefact, re-verifies the sums,
and attaches the three files to the GitHub release by hand (Linux joins the
existing `v0.1.0` release the way Windows did). Dispatch it when `main` is
quiet: the workflow-level `concurrency` group (`cancel-in-progress: true`)
cancels an in-flight run on any push to `main`, and the same dispatch also
runs the python job's `slow or nightly` guards, which bill in the same run.

**Run, and green.** The first release run was the public repo's own CI on
2026-08-19 (run 32191761089, commit `671b26ae`, `linux_release=true`): all six
jobs green, including this one. It answered the named first-run risk in the
affirmative — **PyInstaller froze against the uv-managed Python on the runner
with no `setup-python` step and no `--python` override**, so the fallback that
line contemplated was never needed. The run produced
`no_human-0.1.0-linux-amd64.deb` (122,048,572 B) and
`no_human-0.1.0-linux-x86_64.AppImage` (151,011,154 B), installed the .deb,
drove the installed app first-run → board → quit under Xvfb, did the same for
the extracted AppImage, and proved `apt-get remove` keeps `~/.no_human`.
`BUILD_STAMP`: `commit=671b26ae…`, `dirty=no`. **[verified]**

---

## 5. Install, verify, and the Claude CLI on Linux

**`.deb`** (Ubuntu, Debian, Mint, Pop!_OS, …):

```bash
sha256sum -c SHA256SUMS-linux.txt          # against the file you downloaded
sudo apt install ./no_human-0.1.0-linux-amd64.deb
```
Then launch **no_human** from the application menu. **[unverified]**

**AppImage** (any distro):

```bash
chmod +x no_human-0.1.0-linux-x86_64.AppImage
./no_human-0.1.0-linux-x86_64.AppImage
```
Two frictions are expected and honest, not bugs to hide **[unverified until
Lane B row 11 quotes them]**:
* Ubuntu 22.04+ no longer ships FUSE 2. If the file refuses to start with a
  `libfuse.so.2` message: `sudo apt install libfuse2t64` (24.04) or
  `libfuse2` (22.04) — or run it extracted:
  `./no_human-…AppImage --appimage-extract && ./squashfs-root/AppRun --no-sandbox`
  (`AppRun` alone adds `--no-sandbox` only when its own userns probe fails, so
  pass it explicitly). **[unverified]** until Lane B row 11.
* Ubuntu 24.04 restricts unprivileged user namespaces; an Electron AppImage
  launched without `--no-sandbox` may print *"The SUID sandbox helper binary
  was found, but is not configured correctly"*. The double-click path goes
  through the `.desktop` Exec, which does carry the flag; a hand-run `AppRun`
  may not. Prefer the `.deb` — its postinst installs an AppArmor profile that
  allows the sandbox (§3 #5a). The exact message and remedy observed on a real
  24.04 desktop go here after Lane B.

**Verify the install is real** — the same `nh doctor` as the Mac and Windows
apps, from the binary the app bundles:

```bash
/opt/no_human/resources/nh-server/nh doctor      # .deb
./squashfs-root/resources/nh-server/nh doctor    # extracted AppImage
```
Expect a `coding backend` line, the mechanism-liveness table and
`no contradictions, no evidence gaps`, exit code 0. **[unverified]**

**The Claude Code CLI is not bundled** (as on the other platforms). Install it
once, then create the token the app asks for:

```bash
curl -fsSL https://claude.ai/install.sh | bash     # lands in ~/.local/bin
#   or: npm install -g @anthropic-ai/claude-code
claude setup-token
```
`~/.local/bin` is in `POSIX_CLI_HINT_DIRS`, so the spawned server finds it even
when a `.desktop` launch did not inherit it. An `nvm`-managed `npm -g` install
is NOT covered by the hint dirs; if Lane B shows a `.desktop` launch missing it,
that is a finding for a login-shell resolution, not a doc note. **[unverified]**

**Uninstall:** `sudo apt remove no-human-desktop`. `~/.no_human` (config,
credential, task history) is deliberately left behind. **[unverified]**

---

## 6. Lane A — results from the CI job

**The CI job has not run yet** — the org's GitHub Actions were blocked by
billing on 2026-08-18 (*"The job was not started because recent account
payments have failed or your spending limit needs to be increased"*, every
job, PR #416). The SAME steps were therefore run, in the same order, by
`~/lane-a.sh` on a real Ubuntu 24.04 desktop (EC2 `m7i-flex.large`, the Lane B
machine) as user `tester` on `DISPLAY=:1` — **[verified 2026-08-18, log
`lane-a.log` kept with the screenshots]**. Two caveats, stated plainly: this
box is 24.04 (glibc 2.39), NOT the 22.04 release runner, so it says nothing
about the glibc floor; and the driver ran on the real X display, not Xvfb.
The CI row set below is what that run produced; the CI-runner column stays
empty until billing is fixed and the job runs.

| Check | Result (Ubuntu 24.04 desktop, 2026-08-18) | CI runner (22.04) |
| --- | --- | --- |
| Runner / glibc | Ubuntu 24.04 LTS x86_64, `m7i-flex.large`; Python `3.12.3` (uv-managed) | not run |
| PyInstaller against uv-managed Python | **works** — `libpython3.12.so` (`Py_ENABLE_SHARED=1`), PyInstaller: "Using Python shared library: /lib/x86_64-linux-gnu/libpython3.12.so.1.0" (first-run risk 1 answered) | not run |
| Bundle gates (0 .py, no ci_gate, no build path) | `OK: …/packaging/dist/nh-server (63M), 0 .py files, no ci_gate` | not run |
| `.deb` size / sha256 | 119,927,100 B — `c7cfe19b5964b1629945d048bba68007167f187668bd440ad7ef348687746659` (this box's build, NOT the release artefact) | not run |
| AppImage size / sha256 | 148,153,131 B — `c92eb1dc0985eaaf000c9cee7c4b6a7432c1557b4f8ed973355b57f0eb7454b0` (same caveat) | not run |
| `BUILD_STAMP` commit == HEAD | `commit=d0fd78e579d5cfa2cf52e578e2f5b47bc4f506ec dirty=no` (the branch tip at the time) | not run |
| `verify_artefact.py` | rc=3 (provenance tautological, every check that ran passed) — accepted as documented | not run |
| `apt install ./…deb` | `Setting up no-human-desktop (0.1.0)`; `/opt/no_human/chrome-sandbox` **0755** (userns available); AppArmor profile file `/etc/apparmor.d/no_human` present **and loaded** (`aa-status`); the kernel's AppArmor unprivileged-user-namespace restriction sysctl reads 1 on this box — exactly the 24.04 condition §3 #5a describes | not run |
| `nh --version` from `/` | `nh, version 0.1.0` | not run |
| First run opened `token.html` (screenshot `01-first-run.png`) | yes — the Connect Claude screen on an EMPTY board (fresh HOME). **Finding:** the copy said "stored only on this Mac" / "First, on this Mac:" — hardcoded; fixed on this branch to "this computer" (it had shipped to Windows users too) | not run |
| Board attached; `GET /api/tasks` 200; `nh` process alive (screenshot `02-board.png`) | yes — the onboarding wizard's Welcome step rendered; `1 nh process` alive; driver `OK` | not run |
| `~/.no_human/.env` mode 0600 in the throwaway HOME | yes (asserted by the driver) | not run |
| Quit reaped `no_human` and `nh` | yes (`1 nh process reaped`) | not run |
| AppImage (extracted) — same run | `AppRun` on the real display, sandboxed: driver `OK`, same assertions | not run |
| `apt remove` keeps `~/.no_human` | yes — `.env` and `no_human.db` survived; `/opt/no_human/no_human` gone | not run |
| Wall time / billed minutes | clone→artefacts ≈ 3 min, whole lane ≈ 6 min on this box (no CI minutes billed) | not run |
| **Finding on a REAL desktop only** | Playwright's launch failed once with *"Authorization required… Missing X server or $DISPLAY"*: the driver overrides `HOME`, so `XAUTHORITY` defaulted into the throwaway home. Under `xvfb-run` (CI) `XAUTHORITY` is set explicitly, so the class never appears there. Remedy on a real display: `export XAUTHORITY=$HOME/.Xauthority` before the driver (done in `lane-a.sh`; the driver itself is unchanged — a real user never runs it) | n/a |

## 7. Lane B — acceptance as a real user, Ubuntu 24.04 desktop

Walked 2026-08-18 on a REAL Ubuntu 24.04 desktop (EC2 `m7i-flex.large`,
XFCE 4 over TigerVNC/noVNC, user `tester` created fresh for the walk), driven
by the supervising session as a user would — clicks in the desktop through
the browser — with the OPERATOR typing the credential (row 5). Every row is
the OBSERVATION. Rig honesty: XFCE, not GNOME, so App-Center/GNOME-Shell
behaviours (row 2's double-click, GNOME's app grid) are only partly
reproducible; the artefacts came from the on-box build (§6), not a release,
because the CI job could not run (org billing).

> **Update 2026-08-19 — what closed afterwards, and what did not.** The public
> repo's own CI (free minutes) then built and published the release, so row 1's
> "no release/pointer exists" no longer holds: `v0.1.0` carries the .deb, the
> AppImage and `SHA256SUMS-linux.txt`, the release bucket carries a pointer per
> format whose sha256 equals the CI run's own checksum line, and the site's
> Linux button routes to the signed-in download (checked live). What this does
> NOT retroactively claim: the desktop walk below was performed against the
> ON-BOX build, and the download-from-the-site hop in row 1 has still never
> been walked end to end by a human — signing in requires the operator's own
> credential. Rows 7–8 (a real task to a real PR) likewise remain open.

| # | Step | Pass condition | Observed |
| --- | --- | --- | --- |
| 1 | getnohuman.com → "Download for Linux" → sign-in → download | `.deb` in `~/Downloads`; sha256 matches the pointer | **N/A yet** — no release/pointer exists (this branch is pre-merge). The three files were placed in `~/Downloads` from the on-box build; `sha256sum -c SHA256SUMS-linux.txt` → both `OK` (typed by hand in a terminal, screenshot) |
| 2 | Double-click the `.deb` in Files → install | installs without a terminal; `dpkg -l no-human-desktop` = 0.1.0 | Thunar → double-click opened **Package Installer (gdebi)**: package `no-human-desktop`, "All dependencies are satisfied", description shows the synopsis + **"Stop babysitting Claude"** (so `linux.description` really lands). Install Package then closed silently: gdebi's `pkexec` prompt cannot complete in this VNC/XFCE session (polkit) — a RIG limitation, not the package (stock Ubuntu's App Center path is untestable on XFCE). Installed the way Ubuntu's docs say instead: `sudo apt install ./no_human-0.1.0-linux-amd64.deb` in a terminal → installed, `dpkg -l` shows `0.1.0` |
| 3 | Applications menu → launch | brand icon, window opens | Under **Development**. Icon file `/usr/share/icons/hicolor/512x512/apps/no_human.png` (184 KB, the brand mark). **Observation:** the XFCE menu label renders as **"nohuman"** — GTK menus treat `_` as a mnemonic (underlined `h`) even though the `.desktop` file says `Name=no_human`; a GNOME app grid does not do this. Cosmetic; recorded, not "fixed" (renaming the product for one menu toolkit is the wrong trade) |
| 4 | First-run credential screen | appears; board behind it EMPTY (fresh user) | **Yes** — "no_human — connect Claude" window from the menu launch, on a user whose `~/.no_human` never existed. **Finding (fixed on this branch):** the copy said "stored only on this Mac" / "First, on this Mac:" — hardcoded in `token.html`/`setupUi.mjs`, now "this computer", test-pinned; the SAME copy had shipped to Windows users in v0.1.0 |
| 5 | Install the Claude CLI per §5; token pasted by the OPERATOR | "Save and start" → board | CLI: `npm install -g @anthropic-ai/claude-code` (§6, `claude --version` OK). A shape-valid DUMMY token was saved to reach the board for rows 6/9/10 (a real one replaces it via File → Re-enter Claude Token); **with that invalid token, the first task landed in "waits for quota / paused_quota"** and the intake fell back to a generic scoping question — a bad credential is indistinguishable from a quota pause for the user (finding, all platforms; to file). Real-token run: *(operator step — pending)* |
| 6 | Onboarding, all 8 steps; add a repo by PATH under `/home/<user>/…` | repo found, profile derived, proof step runs on Linux | **All 8 steps walked.** **Finding (fixed on this branch):** the automatic scan and Re-scan found NOTHING for `~/code/calc` — `CONVENTIONAL_ROOTS` spells `"Code"` and Linux filesystems are case-sensitive (APFS/NTFS fold, which is why nobody saw it) → `repo_discovery.py` now takes every on-disk case variant (test `test_conventional_roots_match_case_variants_on_case_sensitive_filesystems`). "Search another folder" `~/code` found `calc` (badge "branch and status not checked"); Profile 1 repo → `python-pytest · pytest -q`; **Prove test command** honestly FAILED first (`ModuleNotFoundError: No module named 'calc'` — bare `pytest` vs the repo's `python3 -m pytest`; the gate showed the output), passed after a root `conftest.py` → "proven & confirmed"; Launch summary `Repos 1 · proven test command 1 of 1 · Integrations 0 of 5`. Cosmetic: "Proven and confirmed`pytest -q`" missing space (known). Copy finding (all platforms): AI-history step says "Claude Code and ide-agent/agent-a conversations" — a scrub placeholder in user-facing copy |
| 7 | New Task on the demo repo | intake → planning → implementing; git/gh spawns work from a `.desktop` launch | Composer opened from Launch with `~/code/calc` prefilled (the absolute path, home included); task created (`a69e2ec0`). With the dummy token it sits in `paused_quota` (see row 5). Real run: *(pending the operator's token)* |
| 8 | Task reaches a PR; verdict visible | PR opens in the browser via `xdg-open` | *(pending row 5)* |
| 9 | Settings, every tab; Integrations → Jira "Test connection" | renders; result on the card | Settings reached via **View → Settings (Ctrl+4)** — at this 1300×750 window the sidebar showed no Settings link (About sits at the bottom; the sub-1080px tightening from 08-17 may not cover this size — to check). Projects/Integrations (9 cards, all Unconfigured)/Updates rendered. **Finding (fixed on this branch, ALL platforms):** Updates card read **"no_human dev"** — the preload is sandboxed and its `require("./package.json")` throws, so every packaged app DISPLAYED version `dev` (the update check itself runs in main on `app.getVersion()` and was unaffected); the version now travels via `webPreferences.additionalArguments`, measured in a real sandboxed renderer with a negative control (`uiPages.test.mjs`). Jira test-connection: needs credentials — not run on this box |
| 10 | Close the window | app QUITS (§3 #1); `pgrep nh` empty; relaunch → same state | **Yes.** Window close → `pgrep -x no_human` 0, `pgrep -x nh` 0; `~/.no_human/` kept `.env` (0600), `config.yaml`, `no_human.db` (+wal); relaunch from the menu → straight to the board (no credential screen), same task `paused_quota · 5m` |
| 11 | AppImage: `chmod +x`, double-click | exact FUSE / sandbox behaviour and remedy | **Observed on 24.04 (with `libfuse2t64` installed on this rig — a default 24.04 desktop does not have it, and without it the AppImage prints the `libfuse.so.2` message):** double-click in Thunar → the AppImage FUSE-mounts under a temporary directory in `/tmp` and runs **with `--no-sandbox`** — `ps` shows `no_human --no-sandbox` and every zygote `--no-sandbox` — no sandbox error, straight to the credential screen. (`unshare -Ur true` succeeds for this user, so the flag came from the AppImage's own launch path, not the userns probe: the double-click path IS the flagged path.) While the `.deb` app was already running, the double-click just raised its window — the single-instance lock spans install types. Lane A's extracted `AppRun` ran sandboxed on the same display (§6) |
| 12 | `sudo apt remove no-human-desktop` | `~/.no_human` preserved | *(pending; Lane A's throwaway-HOME variant passed, §6)* |

### 7.1 The walk's fixes, re-verified on the same desktop

The branch tip after the walk (`c457a946a`) was rebuilt on the box
(`bash packaging/build-installer.sh` + `npm run dist:linux`, stamp
`commit=c457a946a… dirty=no`) and installed OVER the running app with
`apt-get install --reinstall ./…deb` — a real in-place upgrade; the running
window kept working until quit, relaunch came up on the board with the same
task. Then, as a user **[verified 2026-08-18]**:

| Fix | Where a user sees it | Observed |
| --- | --- | --- |
| version handed to the sandboxed preload | Settings → Updates | **"no_human 0.1.0"** (was "no_human dev") |
| conventional roots match on-disk case | the wizard's automatic scan (`GET /api/repos/discover` on the running server) | `roots_scanned: ["…/code"]`, `calc` found with branch `main` (was: nothing found, `~/code` never scanned) |
| platform-neutral credential copy | File → Re-enter Claude Token | "stored only on this **computer**" / "First, on this **computer**:" |

## 8. What was NOT verified

Stated plainly rather than implied by omission, and updated as §6/§7 fill:
everything above marked **[unverified]**; the tray on GNOME and KDE (so §3 #1
is a hold, not a verdict); auto-update; arm64; `rpm`/snap/flatpak; any distro
other than Ubuntu 24.04 (Lane B) and the 22.04 runner (Lane A); HiDPI/Wayland
scaling; a `.desktop` launch under a display manager other than GDM; an
`nvm`-installed CLI; non-ASCII home paths.
