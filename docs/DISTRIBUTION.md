# Distribution — where the artifacts live, and how they update

Two artifacts, two homes:

| Artifact | What it is | Where it goes | Why |
|---|---|---|---|
| `no_human-<version>.dmg` (~145 MB) | the macOS desktop app, with the frozen `nh` server inside it | **GitHub Releases** | free, unmetered bandwidth, `electron-updater` speaks it natively |
| `no_human-<version>-py3-none-any.whl` / `.tar.gz` (~1 MB) | the `nh` command line | **PyPI** (`pip install no-human`) | free, the only place `pip` looks |

Both artifacts have a real home today. Desktop releases are published on the
public [`no-human-ai/no_human`](https://github.com/no-human-ai/no_human)
GitHub Releases page — `README.md`'s release badge and download buttons point
there. Every `electron-builder` invocation still passes `--publish never`
(`desktop/packagedFiles.test.mjs`'s `"no script publishes anything"` test
fails the suite if one stops doing so), so a build never uploads anything by
itself — publishing is always the separate, deliberate step in
[§5](#5-the-commands-that-actually-publish).

---

## 1. Why GitHub Releases

The decision is **not** about cost — at the volumes this product will see, all
three candidates are free or nearly so. It is about operational surface.

Cost of serving a 145 MB artifact (0.1356 GiB), per month:

| Downloads/month | GitHub Releases | Cloudflare R2 | S3 + CloudFront | S3 direct |
|---|---|---|---|---|
| 100 | $0 | $0 | $0 | $0 |
| 1,000 | $0 | $0 | $0 | $3.20 |
| 10,000 | $0 | $0 | ~$28.34 | $113.04 |
| 100,000 | $0 | $0 | ~$1,049 | ~$1,211 |

Arithmetic for the two that are not zero:
- **S3 + CloudFront @ 10,000**: 1,356.04 GiB egress − 1,024 GiB always-free =
  332.04 GiB × $0.085 = $28.22, plus ~5 GB S3 storage × $0.023 = $0.12.
  Requests are inside the 10M/month free allowance.
- **S3 direct @ 10,000**: 1,256.04 GB × $0.09 = $113.04 (only 100 GB is free,
  and that 100 GB is shared across all AWS services).
- **R2** charges **$0 egress by policy**. Even outside every free tier the bill
  would be 5 GB × $0.015 + 10k GETs × $0.36/M = **under $0.08/month**.

GitHub Releases wins on everything that is not cost: no account to provision,
no DNS, no bucket policy, no credentials in CI beyond the `GITHUB_TOKEN` that
already exists, and `electron-updater` has a first-class GitHub provider — the
one actually configured today (`desktop/electron-builder.config.cjs:366`:
`publish: [{ provider: "github", owner: "no-human-ai", repo: "no_human" }]`).
Release assets are capped at **2 GiB** each (we are at 0.145 GiB) and GitHub
states there is *"no limit on the total size of a release, nor bandwidth
usage."* The only backstop is the Acceptable Use Policy's discretionary
"excessive bandwidth" clause, which is not a real constraint below six figures
of downloads.

### What we rejected, and the condition that would change it

- **Cloudflare R2** — genuinely free at any volume we will reach, and the site
  is already on Cloudflare. Rejected only because it adds a bucket, a custom
  domain, and a credential for zero benefit today. **Revisit if downloads pass
  ~100k/month**, where GitHub's AUP clause starts to matter and R2 stays $0.
  Migration is a config change, not a rearchitecture: `electron-updater`
  supports a `generic` provider (a plain static host) and a native `r2`
  provider. The client gains no dependency either way. Avoid the `r2.dev` URL
  if this is ever exercised — Cloudflare rate-limits it and documents it as
  development-only.
- **S3 + CloudFront** — the most expensive option and the most moving parts.
  Rejected. ⚠️ One number here is unverified: AWS now leads with flat-rate
  CloudFront plans (Free = 100 GB, Pro = $15/mo for 50 TB) and no fetchable AWS
  page states whether choosing a plan *replaces* the always-free 1 TB tier. If
  it does, the free tier is 10× smaller than assumed above. Verify before ever
  choosing AWS on cost grounds.
- The branch `installer/freeze-s3` is **not** about Amazon S3 — "S3" there is
  the internal program lane (installer/frontend/a11y). It is already merged and
  superseded by `main`; there is nothing to revive.

### Why the repo had to go public

GitHub Releases on a **private** repo require an authenticated token to
download an asset. There is no unauthenticated public URL, and
`electron-updater` supports private repos only by shipping a `GH_TOKEN` **to
every end user**, which its own documentation calls "not intended and not
suitable for all users". That was never a viable distribution channel — it is
the reason the repo could not stay private and still ship desktop releases
through GitHub.

**Resolved:** `no-human-ai/no_human` is the public repo. `README.md`'s release
badge and macOS download button both point at
`github.com/no-human-ai/no_human/releases/latest`, and that is exactly where
`electron-updater`'s configured `github` provider
(`desktop/electron-builder.config.cjs:366`) looks — no token required to
download a release or check for an update.

### PyPI

Free, no bandwidth terms, and the name `no-human` is **available** (verified:
`no-human`, `no_human` and `nohuman` all return HTTP 404 on PyPI). Limits are
**100 MiB per file** and **10 GiB per project**; the wheel is ~1 MB, so neither
binds. Note the DMG **cannot** ship via PyPI — it exceeds the per-file cap, and
it does not belong in a wheel regardless.

**The wheel does contain the web board.** `web/dist` is gitignored and built
separately, but `hatch_build.py`'s build hook force-includes it (plus
`migrations/*.sql`, via `pyproject.toml`'s
`[tool.hatch.build.targets.wheel.force-include]`) into every build, and a
standard (non-editable) wheel or sdist build refuses to complete without a
fresh board — `hatch_build.py`'s `BoardNotBuiltError`/`BoardStaleError`. An
editable install (`uv sync`) warns and proceeds instead of failing, so local
development stays installable without a browser build. What this repo cannot
confirm: which specific versions are currently live on pypi.org — the publish
path is `.github/workflows/publish-pypi.yml`, but the index's contents are not
tracked here; check pypi.org/project/no-human/ directly.

---

## 2. How updating works

Two independent halves, because they have different constraints.

### The CLI half — works today, needs no signing

`src/no_human/updates.py`. On every `nh <subcommand>` invocation:

1. Read `~/.no_human/cache/update-check.json`.
2. If a newer version is cached, print one line **after** the command's own
   output — on **stderr only**, and only when stdout is an interactive TTY.
   It is suppressed entirely for machine output (`--json` / `--json-out`
   commands mark themselves) and whenever stdout is piped, so scripts and
   parsers never see it (it corrupted `--json` output and failed eight tests
   the day 0.1.3 reached PyPI). `nh --version` is a click eager option and
   exits first, so the fastest path is untouched.
3. If the cache is older than 24 h, start a **daemon thread** to refresh it and
   return immediately. The notice always comes from the *previous* run's cache.

It never blocks (the network call is on a thread nobody joins), never fails a
command (every path is wrapped and degrades to silence), and never nags (one
line, at most one network call per day).

Turn it off with either:

```yaml
# ~/.no_human/config.yaml
updates:
  enabled: false
```

```sh
export NH_NO_UPDATE_CHECK=1     # also the right switch for CI
```

Honest limitation: a command that exits faster than an HTTPS round trip
(`nh --version`) dies before the daemon thread finishes, so that invocation's
cache write is lost and the next one retries. That is the deliberate trade for
never adding latency.

### The desktop half — signed and notarized on macOS, fail-loud elsewhere

`desktop/updater.mjs` drives `electron-updater` with **`autoDownload: false`**
and **`autoInstallOnAppQuit: false`**. Both default to the wrong value:
`autoDownload` would move 145 MB unasked, and `autoInstallOnAppQuit` would
install on quit the very update the user deferred.

The flow:

1. On launch (after the window exists, never awaited) the shell checks — at
   most once a day.
2. If a newer version exists, the board's **Settings → Updates** panel shows it
   with **Download now** and **Later**. Nothing is downloaded until the click.
3. **"Later" persists**, keyed on the *version*, in
   `<userData>/update-state.json`. That version never prompts again. A *newer*
   version still gets through, and **File → Check for Updates…** always answers
   regardless of any deferral — otherwise "Later" would mean "never".
4. Once downloaded, the panel offers **Restart and install**.

**macOS requires the app to be signed for any of step 4 to work.** Electron's
own docs: *"Your application must be signed for automatic updates on macOS.
This is a requirement of Squirrel.Mac."* Unsigned, `SQRLUpdater` throws
`Could not get code signature for running application`, which surfaces late and
opaquely.

The macOS release path signs and notarizes the DMG (§3), so a fully `signed`
build stamps `nhCanAutoUpdate: true` into the packaged app
(`extraMetadata.nhCanAutoUpdate`, set from `desktop/signing.cjs`'s verdict) and
step 4 works end-to-end. Windows and Linux are unsigned by decision (no
Authenticode certificate for Windows; `.deb`/AppImage packages carry no
equivalent signature — `docs/WINDOWS.md`, `docs/LINUX.md`), so
`nhCanAutoUpdate` stays `false` there. For those builds — and for any macOS
build missing notarization credentials (`signed-not-notarized` or
`unsigned`) — the updater still **fails loudly instead of silently doing
nothing**: it *reports* that a new version exists while refusing the install
with a sentence that names the cause and points at the manual download.

Also load-bearing: `mac.target` **must include `zip`**. Squirrel.Mac updates
from a ZIP, and electron-builder only emits `latest-mac.yml` — the file
`electron-updater` fetches — when a zip target is present. With `["dmg"]` alone
the updater fails at runtime with `ERR_UPDATER_ZIP_FILE_NOT_FOUND`. The DMG is
what a human downloads; the ZIP is what the updater consumes. Both ship in the
same release. `desktop/packagedFiles.test.mjs` pins this.

---

## 3. Code signing — the runbook

Assume you have never done this. Do these in order.

### 3.1 Enrol

1. Go to <https://developer.apple.com/programs/enroll/>.
2. Enrol as an **individual** (a company enrolment needs a D-U-N-S number and
   is not required here).
3. Cost: **99 USD per year**, renewed annually.
4. You must be the **Account Holder** of the membership to create a Developer
   ID certificate. Enrolling as an individual makes you that automatically.

> An **individual** membership does issue Developer ID certificates in
> practice: this project's notarization credentials were stored and validated
> by Apple under one (`docs/INSTALLER.md:418`). The $299 Enterprise Program is
> **not** what you want — it is internal-distribution only and never grants
> Developer ID.

### 3.2 Create the right certificate

Create a **“Developer ID Application”** certificate.

**This is the one that matters.** The alternatives are wrong in ways that cost
a full cycle to discover:

| Certificate | Use | For us |
|---|---|---|
| **Developer ID Application** | apps distributed **outside** the Mac App Store | ✅ **this one** |
| Developer ID Installer | signing `.pkg` installers | ❌ we ship a DMG |
| Mac App Store / Apple Distribution | submitting to the Mac App Store | ❌ wrong channel entirely |
| Apple Development | local debugging | ❌ cannot be distributed |

Steps: Xcode → Settings → Accounts → your Apple ID → **Manage Certificates** →
**+** → **Developer ID Application**. (Or the web flow at
<https://developer.apple.com/account/resources/certificates>, which requires
generating a CSR with Keychain Access first.)

### 3.3 Export it for CI

In **Keychain Access**, find `Developer ID Application: <YOUR NAME> (<TEAM ID>)`,
right-click → **Export** → `.p12` format → set a strong password.

For a local build the keychain is enough. For CI, base64 it:

```sh
base64 -i DeveloperID.p12 | pbcopy     # paste into the CI secret
```

### 3.4 Create an App Store Connect API key for notarization

Notarization needs separate credentials. Apple's `altool` was **decommissioned
on 2023-11-01**; only `notarytool` works. Apple and electron-builder both
recommend the **API key** method over an Apple ID password.

<https://appstoreconnect.apple.com/access/integrations/api> → **+** → give it
the **Developer** role → download the `.p8` file (**you can only download it
once**). Note the **Key ID** and **Issuer ID** shown on that page.

### 3.5 Set the environment variables

`desktop/signing.cjs` accepts any **one** of Apple's three credential sets. Use
the first.

**Identity (always required):**

| Variable | Value |
|---|---|
| `CSC_LINK` | path to (or base64 of) the `.p12` |
| `CSC_KEY_PASSWORD` | the `.p12` export password |

**Notarization — pick one set:**

| Set | Variables |
|---|---|
| **1 (recommended)** | `APPLE_API_KEY` (path to the `.p8`), `APPLE_API_KEY_ID`, `APPLE_API_ISSUER` |
| 2 | `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID` |
| 3 | `APPLE_KEYCHAIN_PROFILE` (and optionally `APPLE_KEYCHAIN`, a keychain **path**) |

A profile stored by `notarytool store-credentials` resolves through
notarytool's **default keychain search only**. Set `APPLE_KEYCHAIN`
alongside it (pointing at the login keychain) and notarization fails with
`No Keychain password item found for profile`, because `--keychain <path>`
does not see what the default search resolves. Set `APPLE_KEYCHAIN` only
when the profile actually lives in a **non-default** keychain.

A **partially** filled set counts as no credentials at all (for sets 1 and
2, every listed variable is required) — the build will sign but not
notarize, and will name the artifact `-UNNOTARIZED` rather than pretend.
Empty-string values (how CI exports unset secrets) also count as absent.

Your **Team ID** and **Apple ID** are not known until you enrol, so nothing in
this repo hardcodes them. Do not add placeholder values — supply them only as
environment variables.

As GitHub Actions secrets, the names are identical:
`CSC_LINK`, `CSC_KEY_PASSWORD`, `APPLE_API_KEY`, `APPLE_API_KEY_ID`,
`APPLE_API_ISSUER`.

### 3.6 Build

```sh
cd desktop && npm run dist:bundled
```

This one command freezes the server, packages the app, signs it, builds the
DMG, notarizes it, staples it, and verifies it.

To make an unsigned build a hard error (use this in a release pipeline):

```sh
NH_REQUIRE_SIGNED=1 npm run dist:bundled
```

### 3.7 Verify — expected output

Run all three. Any deviation means it is **not** shippable.

```sh
codesign -dv --verbose=2 packaging/dist/no_human-<version>.dmg
```
Expect `Authority=Developer ID Application: <NAME> (<TEAMID>)`,
`Authority=Developer ID Certification Authority`, `Authority=Apple Root CA`,
and a `Timestamp=` line — this is what a `signed` release build produces.

```sh
spctl -a -t open --context context:primary-signature -v packaging/dist/no_human-<version>.dmg
```
Expect `accepted` and `source=Notarized Developer ID` — this is what a
`signed` release build produces.
`accepted` with `source=Developer ID` (no "Notarized") means signing worked but
notarization did not — the artifact will still be refused on a machine that has
never seen it. `rejected`, or no `Timestamp=` line above, means do not ship.

```sh
xcrun stapler validate packaging/dist/no_human-<version>.dmg
```
Expect `The validate action worked!`.
An unstapled DMG can still fail on a machine with no network even when
notarization succeeded, so this is not optional.

Final check — the filename itself. A shippable build is
`no_human-<version>.dmg`. If it says `-UNSIGNED` or `-UNNOTARIZED`, the build
told you what is missing.

---

## 4. Current state

| Platform | Artifacts | Signing | Auto-update |
|---|---|---|---|
| macOS | DMG + ZIP | Signed and notarized when the credential set is present (`desktop/signing.cjs`); the filename carries the verdict — plain `no_human-<version>.dmg` only when fully `signed`, else `-UNSIGNED`/`-UNNOTARIZED` | `nhCanAutoUpdate: true` for a `signed` build |
| Windows | NSIS installer + ZIP | Unsigned by decision — no Authenticode certificate purchased; SmartScreen warns on first run (`docs/WINDOWS.md`) | `nhCanAutoUpdate` stays `false` |
| Linux | `.deb` + AppImage (x64) | Unsigned by decision — packages carry no code signature by convention; integrity travels as `SHA256SUMS-linux.txt` on the release (`docs/LINUX.md`) | `nhCanAutoUpdate` stays `false` |

- The DMG builds and launches, and the bundled server starts from inside the
  `.app`.
- The CLI update check (§2, CLI half) works on every platform, independently
  of any of the above.
- Icons are no longer a gap: `packaging/derive-icons.mjs` derives
  `icon.ico`/`icon.png`/`icon.icns` from the brand master at package time, and
  `desktop/electron-builder.config.cjs`'s `requireFreshIcon()` refuses to build
  if a derived icon is missing or older than that master — so a shipped app
  cannot silently ship wearing the stock Electron icon.

## 4a. Gotcha: a user-level `~/.npmrc` can rewrite the lockfile

A `registry=` line in a user-level `~/.npmrc` silently redirects every
`npm install`, and npm rewrites the `"resolved"` URLs in `package-lock.json`
to whatever registry served the request. The repo pins the public registry
with per-directory `.npmrc` files (repo root, `web/`, `desktop/`) because npm
reads project config from the current directory only — it does not walk up.

After any `npm install`, verify the lockfile still resolves only to the
public registry:

```sh
grep '"resolved"' desktop/package-lock.json | grep -vc 'registry\.npmjs\.org'   # must be 0
```

If a foreign host appears, search-and-replace that host's URL prefix back to
`https://registry.npmjs.org/`. The integrity hashes are unaffected when the
foreign registry is a pass-through proxy, so only the URL differs.

## 5. The commands that actually publish

Publishing is deliberately manual. No build step publishes on its own — every
`electron-builder` invocation runs with `--publish never`
(`desktop/package.json`'s dist scripts, pinned by
`desktop/packagedFiles.test.mjs`'s `"no script publishes anything"` test) — so
a release is exactly these commands, run by a human. Listed so publishing is
one reviewed command, and so nobody has to reconstruct it under pressure.

**CLI to PyPI** (irreversible — a version number can never be reused). The
actual path is `.github/workflows/publish-pypi.yml`: a manual
`workflow_dispatch` that requires typing `"publish"` to confirm and uses PyPI
Trusted Publishing (OIDC), so no token lives in the repo or its secrets. It
defaults to the `testpypi` index so a rehearsal cannot hit the real index by
accident — the real index must be chosen explicitly. The local equivalent:

```sh
uv build
uv publish            # or: python -m twine upload dist/*
```

Which versions are currently live on pypi.org is not recorded in this repo —
check pypi.org/project/no-human/ directly.

**Desktop to GitHub Releases** — the public `no-human-ai/no_human` repo is
where `electron-updater`'s configured `github` provider looks
(`desktop/electron-builder.config.cjs:446`). Publish with
`packaging/publish-release.sh`, not a bare `gh release create`/`gh release
upload`:

```sh
packaging/publish-release.sh --tag v<version> \
  packaging/dist/no_human-<version>.dmg \
  desktop/dist/no_human-<version>-arm64-mac.zip \
  desktop/dist/latest-mac.yml \
  --title "no_human <version>" --notes-file CHANGELOG.md
```

All three files are required: the DMG is what a human downloads, and the ZIP +
`latest-mac.yml` are what `electron-updater` reads. A release missing the ZIP
produces an updater that fails for every user. A Windows or Linux release adds
that platform's own installer/package, plus (for Linux) `SHA256SUMS-linux.txt`
for integrity, per `docs/LINUX.md`.

`packaging/publish-release.sh` wraps the same `gh release create`/`gh release
upload` call with `scripts/check_release_feeds.py`, run both before the
upload (against the exact file list on the command line) and after (against
the live release), because a bare `gh` call has no such check and that is
exactly how it went wrong before: v0.2.2 and v0.2.3's `latest-mac.yml` both
named a `no_human-<version>-arm64.dmg` that no release ever uploaded, and
v0.1.7 and v0.2.0 shipped Windows/Linux assets with no `latest.yml`/
`latest-linux.yml` at all. Both slipped through unnoticed for two releases
because nothing checked a feed's `url:`/`path:` against the assets actually
attached to the release.

The chosen fix is to stop `no_human-<version>-arm64.dmg` from ever being
named in the first place: `desktop/electron-builder.config.cjs`'s `mac.target`
no longer includes `dmg` (`zip` and `dir` stay). This is a build-time fix, not
a publish-time filter — `scripts/check_release_feeds.py` (run by
`packaging/publish-release.sh`) exists to catch a regression of this exact
shape, not to launder one out of a feed after the fact. The reason
electron-builder's own `dmg` target had to go, and not just deduplicated
against `packaging/make-dmg.sh`'s output, is that the two are not
duplicates: electron-builder's `dmg` target signs its output but never
notarizes or staples it, so even if it were uploaded, Gatekeeper would refuse
to open it. `packaging/make-dmg.sh` is what actually produces the notarized
DMG that ships, entirely outside electron-builder's own target list.

> ⚠️ `electron-builder`'s GitHub provider defaults `releaseType` to **draft**,
> and a draft release is invisible to the updater — it looks exactly like "no
> updates available". `packaging/publish-release.sh`'s post-publish check
> fails on this too — `scripts/check_release_feeds.py` treats a draft release
> as a hard problem, not just a warning — so a drafted release will not pass
> as "published" by this script either. Publish the release, don't leave it
> drafted.
