# The friend-shareable installer

A no-source macOS build: the nh server is frozen to bytecode with PyInstaller and
shipped inside the Electron app, so someone can run no_human without a Python
toolchain and without reading the source.

## Build

Requires a populated `.venv` at the repo root (`uv sync`), since the freeze runs
`.venv/bin/pyinstaller`, plus `npm install` in `desktop/` and `web/`.

```bash
uv sync
cd desktop && npm run dist:bundled
```

`npm ci` (or `npm install`) in `desktop/` now fetches the Electron binary at
install time, via a `postinstall` script (`node node_modules/electron/install.js`)
in `desktop/package.json`. Electron itself has shipped no postinstall of its own
since v42 — left alone, the binary is instead fetched lazily on the first real
`require("electron")`, which for a packaging build is invisible but for a cold
`node --test desktop/*.test.mjs` run landed *inside* the concurrent suite and
starved the Electron-stub boot tests' fixed settle windows (20 s in
`mainSaveFailure.test.mjs`, 5 s in `mainLateServer.test.mjs`). A repeat
`npm ci` wipes `node_modules/` first, so its postinstall re-extracts the zip
from the local Electron download cache (`~/Library/Caches/electron` on macOS)
rather than re-downloading; electron's own `isInstalled()` check short-circuits
only when `dist/` and `path.txt` survive — `npm install`, or running
`install.js` by hand. With a cold cache and no network, `npm ci` now fails at
this step instead of the
first test run failing later; `npm ci --ignore-scripts` skips it, but the
test suite then downloads the binary inside its first run — see
`CONTRIBUTING.md`'s "Desktop shell" section for why that is not recommended.

That runs three steps:

1. `packaging/build-installer.sh` — builds `web/dist`, freezes the server via
   `packaging/nh-server.spec`, places runtime data at the bundle root, and
   **fails the build** if the frozen tree contains any `.py` file or is missing
   the board or the migrations.
2. `electron-builder --config electron-builder.config.cjs --mac` — wraps it as
   `no_human.app`, with the frozen server copied in as `extraResources`, and
   also emits the `.zip` + `latest-mac.yml` the auto-updater consumes.
3. `packaging/make-dmg.sh` — produces
   `packaging/dist/no_human-<version>[-UNSIGNED|-UNNOTARIZED].dmg`, then
   **mounts it and verifies its contents** (below).

The filename carries the signing verdict: only a signed **and** notarized build
gets the plain `no_human-<version>.dmg` name, so an unshippable artifact cannot
be uploaded by mistake. See `docs/DISTRIBUTION.md` for the signing runbook.

### Release assets

Each platform's release also carries its electron-builder updater feed —
`latest-mac.yml` (macOS), `latest.yml` (Windows), `latest-linux.yml` (Linux) —
because each one enables in-app update checks to report available versions.
The install path forks by signing: the signed **and** notarized macOS build
stamps `nhCanAutoUpdate: true`, so on the user's "Download now" and then
"Restart and install" it installs the update via Squirrel.Mac from the
`arm64-mac.zip` published beside the DMG (the `path:` in `latest-mac.yml`) —
nothing downloads or installs unattended (`desktop/updater.mjs` turns
`autoDownload` and `autoInstallOnAppQuit` off). The unsigned Windows and
Linux builds stamp `nhCanAutoUpdate: false` — they report the newer version
but refuse the install path.

## The build stamp, and why the DMG is opened before it is called done

On 2026-08-05 a signed, notarized DMG was found to contain none of that week's
UI: it had been built from a checkout 44 commits behind main. Every step
reported success because every step *was* successful — it was a clean build of
the wrong tree. codesign, spctl and stapler all passed it, because a signature
says who built an artifact and notarization says Apple scanned it. Neither says
which source it came from.

So step 1 writes `BUILD_STAMP` into the bundle root — `commit`, `dirty` and
`board_sha256` — and step 3 mounts the finished DMG and runs
`scripts/verify_artefact.py` over it. A mismatch fails the build; the DMG is
not announced as OK. This runs for unsigned builds too — but *what each is
compared against* differs, and that difference is the whole of the two sections
below.

`desktop/signing.cjs` has **three** modes, and the release gate splits them two
ways: only `signed` (signed **and** notarized) is held against `origin/main`.
`signed-not-notarized` and `unsigned` are both held only against the checkout
that built them, because neither is distributable — Gatekeeper rejects an
un-notarized DMG — and neither should require a reachable remote to build.
Their filenames say so (`-UNNOTARIZED`, `-UNSIGNED`) and so does the verdict
line, e.g. `[signed-not-notarized; provenance NOT verified]`.

### What a **signed** build is compared against

`origin/main`, fetched at that moment — not the checkout that produced the
artifact. The first version of this gate compared the stamp against
`git rev-parse HEAD` of the very checkout `build-installer.sh` had read it from,
which is a tautology: it can only fail if someone edits the DMG between building
and packaging it. A DMG built from a clone 45 commits behind main passed it,
printing `verify-artefact: OK — built from 8d8a33130a12, clean tree`.

Every step of resolving that expectation fails closed:

| condition | result |
| --- | --- |
| `packaging/..` is not a git checkout | FAIL |
| no remote named `origin` | FAIL |
| `packaging/..`'s own `HEAD` cannot be resolved, or the self-reference probe cannot be created | FAIL — the check's input is missing, and a gate that skips itself when it cannot see is not a gate |
| the build checkout *itself* does not advertise the probe ref | FAIL — the probe is blind, so an empty answer from `origin` would prove nothing |
| `origin` advertises the probe ref, i.e. it shares this checkout's refs | FAIL — fetching it would compare the artifact against the tree that built it |
| the probe question cannot be put to `origin` (unreachable) | FAIL — before, and independently of, the fetch below |
| `git fetch origin refs/heads/main` does not succeed | FAIL — an unreachable remote is not permission to skip the check |
| `FETCH_HEAD` is not a commit | FAIL |
| the stamped commit is not that tip | FAIL |

The refspec is `refs/heads/main`, spelled out. `git fetch origin main` is
unqualified, and a **tag** named `main` on the release remote resolves ahead of
the branch — `* tag main -> FETCH_HEAD`. A reviewer pushed one and walked a
signed build 45 commits behind straight through this gate, with no environment
variable and no change to the build checkout.

The remote and branch are **literals**, not environment variables. They were
`NH_RELEASE_REMOTE` / `NH_RELEASE_BRANCH`, and this page previously said no
override existed while shipping two:
`NH_RELEASE_REMOTE=$ROOT NH_RELEASE_BRANCH=HEAD` pointed the gate at the
artifact's own checkout and a 45-commit-stale signed build printed a flat `OK`.
`NH_ALLOW_STALE_BUILD=1` waives the *behind-main* check in step 1, so you can
build a deliberate point-in-time artifact; it does **not** reach this gate,
because a point-in-time build is a legitimate thing to make and not a legitimate
thing to sign.

This page used to claim, at this point, that removing those two variables
"stops the **environment** from redirecting the gate", and that anyone
redirecting it "can still repoint `origin`" by rewriting `.git/config`. **Both
halves were false**, and each was refuted by its own driven case:

* `GIT_NAMESPACE=rel` — an environment variable, no config write anywhere —
  blinded the gate's self-reference check. (On git 2.49.0 the build then stops
  at the fetch, which the same namespace also hides, so the *blinding* is what
  reproduced; an end-to-end ship on this variable is **not** claimed.)
* `transfer.hideRefs` in a **global** gitconfig — not this checkout's
  `.git/config`, and a repoint of nothing — walked a signed, 45-commit-stale
  build all the way to `OK`, rc=0.

Deleting two named variables is not the same as stopping the environment.

What is true is narrower, and is a mechanism rather than a boundary:

* the gate **clears** the `GIT_*` variables that redirect it (`GIT_NAMESPACE`,
  `GIT_DIR`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_CONFIG_COUNT` and the
  rest of the list at the head of the block in `make-dmg.sh`) before asking git
  anything, and so do `build-installer.sh` — which writes the stamp — and
  `verify_artefact.py`, which is also run standalone;
* it **calibrates** its self-reference probe, so a config it cannot clear
  cannot silently blind it either (below).

What remains outside is anyone who can repoint `origin` at a **different**
repository serving the same commits — via `.git/config`, or via an `insteadOf` in
a global or system gitconfig — and, strictly stronger, anyone who can choose the
**ssh command** for an ssh `origin` (`GIT_SSH_COMMAND`, or `core.sshCommand` in a
global config): that command is what *answers*, so it can blind the probe as well
as redirect the fetch. Both spellings are driven, and both are pinned by
`test_the_declared_residual_an_ssh_redirect_still_walks_through`; see the note
under "The probe is calibrated" below.

A **third** shape belongs here, and it is the one that shows why the first two
read too narrowly: **a `url` value can be a command.**
`remote.origin.url = ext::git -c uploadpack.hideRefs=refs/nh-self-probe
upload-pack <root>`, with `protocol.ext.allow = always` in the same global
config, points `origin` at *this very repository*, uses no ssh, and **blinds**
the probe rather than redirecting it — driven, 45 commits behind, board reading
`THIS IS THE WRONG BOARD`: `verify-artefact: OK` / `OK: …x.dmg [signed]`, rc=0.
Driven identically against the commit before the by-url change, so it is
**pre-existing** and not a consequence of asking by url; the capability it needs
is the same one the two shapes above already declare (writing a config the gate
reads on purpose), so the posture is unchanged. It is named separately because
it is neither of them, and because it refutes the natural reading of the
key-by-key sweep in `make-dmg.sh` — that only `url` and `pushurl` *name a
repository*. A `url` need not name a repository at all. **Not closed.**

One shape is checked (`origin` resolving
to the build checkout's own **repository**), because it reproduces the tautology
exactly while looking like an ordinary fetch.

That check does **not parse the url**. It plants
`refs/nh-self-probe/<pid>` in the build checkout and asks whether the release
remote advertises it; a remote that answers is sharing this checkout's refs.
The question is asked **by url and never by remote name** —
`git ls-remote -- "$url"`, where `$url` is the **raw, first-listed**
`remote.origin.url` — because every `remote.<name>.*` config key applies to the
name form and to nothing else, and one of them was driven blinding a by-name
probe while the by-url control passed (see "The probe is calibrated" below).
The `--` is not decoration: that url is a config value, and without the
separator a value beginning with `-` reaches git as an **option**, which git
will execute. The url is resolved by git, so the scope is every spelling git
accepts:

* **caught** — `origin` naming this checkout by an absolute path, by a path
  relative to the repo root, as `…/.git`, through a symlink, through a
  `url.<x>.insteadOf` rewrite (git's own resolver applies those, exactly once,
  to the raw url the probe hands it — `git remote get-url` is deliberately
  **not** used, because it reports the url with the rewrite *already* applied
  and re-feeding that to git rewrites a second time), as a
  **linked worktree** of this checkout in either direction (the build runs in
  the primary and `origin` is a worktree, or the build runs in a worktree and
  `origin` is the primary), and as a `file://` url in any of its forms — empty
  authority, any host (git ignores it: `file://example.com/…` resolves),
  userinfo, port, and percent-escapes, which git decodes and the filesystem
  does not. All of these are driven in
  `test_every_url_that_names_the_build_checkout_is_refused` and its neighbours,
  each asserting first that git really does resolve the url back to the build
  checkout;
* **not caught** — a remote that is a separate *ref namespace* merely holding
  the same **commits**: a bare mirror, a `--mirror` clone, a **bundle file**
  used as `origin`, a second clone, or a `cp -al` hardlink farm of this
  checkout. (The `--mirror` clone and the bundle are additions from the sixth
  review; both were driven walking a stale signed build through, and a bundle is
  worth naming on its own because it is a single **file**, not something that
  looks like a repository. Both are pinned by
  `test_the_declared_residual_a_mirror_or_a_bundle_still_walks_through`, which
  asserts they *do* walk through — if that test ever fails, this bullet is what
  needs rewriting.) The reason is narrower than this page used to claim
  (see the note below): a ref created here after such a copy exists never
  appears in it, so the probe cannot see itself there — and nothing else in the
  script could distinguish it from a real remote serving those commits either.
  Staleness measured against a mirror of a stale tree is not detectable from
  inside this script.

The distinction between those two bullets is the one this page got wrong: the
limit is **same commits**, not **same repository**. A linked worktree is the
same repository.

#### The probe is calibrated, because its silence is ambiguous

An empty answer from `git ls-remote -- "$url"` has two readings — "`origin` is
not this repository" and "the probe cannot be seen at all" — and only the first is
the one the check wants. The previous version of this page filed the second
under the residual above, saying `uploadpack.hideRefs` "sits inside the
`.git/config` trust boundary — anyone who can set it can repoint `origin`".
**Both clauses were refuted.** `transfer.hideRefs = refs/nh-self-probe` in a
**global** gitconfig is not in this checkout's `.git/config`, and setting it
repoints nothing; driven, it shipped a signed, 45-commit-stale,
`THIS IS THE WRONG BOARD` DMG at rc=0. A knob in a file the gate never reads is
not inside a boundary drawn around a file it does.

So the probe is no longer trusted — it is **calibrated**. Before the answer from
`origin` is interpreted, the same question is put to the build checkout itself,
a remote that trivially *is* this repository and must therefore advertise the
probe. If it does not, the probe is blind, and the build **FAILS**:

```
FAIL: the self-reference probe is blind — <root> did not advertise
      refs/nh-self-probe/<pid>, a ref that exists in it right now.
```

It fails a build that would otherwise have shipped correctly — asserted
deliberately by
`test_a_blinded_probe_fails_a_build_that_would_otherwise_have_shipped` — because
a gate allowed to pass when its own instrument is broken is the exact shape this
whole mechanism exists to delete.

This paragraph used to end "that is the general answer, not a patch for two
spellings … and configs this page could not enumerate". **The seventh review
refuted it in one config key**, and what it is worth is now stated as its
limits rather than as a class:

* **The control and the probe must be the same *kind* of question.** They were
  not: the control asked a **url** (`ls-remote "$ROOT"`) and the probe asked a
  **name** (`ls-remote origin`). Every `remote.<name>.*` key applies to the name
  form and to nothing else, so a two-line global config —
  `[remote "origin"] uploadpack = "git -c uploadpack.hideRefs=refs/nh-self-probe
  upload-pack"` — blinded the probe while the control passed, and a signed,
  45-commit-stale, `THIS IS THE WRONG BOARD` DMG shipped at rc=0. Both are urls
  now, and the fetch with them (the same key's other half redirected the
  **release-tip fetch** to the build checkout while `origin` was genuine). That
  removes the *asymmetry*; it does not remove a class. `make-dmg.sh` carries the
  key-by-key sweep of the family, including what is deliberately given up by not
  going through the remote name.
* **Its transport.** The control exercises the `upload-pack` this machine spawns
  for a **local path**, because the build checkout is one — while a production
  `origin` is **https**. So the control cannot, by construction, calibrate the
  instrument that answers in production. What it does establish is that the
  local `upload-pack` path is not hiding the probe, which is the transport every
  driven bypass so far has used and the one a self-referential `origin` must
  use, since the build checkout is on this filesystem. It says nothing about a
  genuinely remote server's advertisement — but such a server is a different
  repository, which has no probe to hide.
* **An ssh `origin` is outside it entirely** — the residual above. The ssh
  command answers the probe and the control is not on that transport, so the
  probe is *blinded*, not merely redirected. Driven both ways, with
  `origin = ssh://localhost$ROOT` and a wrapper serving `$ROOT` with the probe
  ref hidden: `verify-artefact: OK` / `OK: …x.dmg [signed]`, rc=0, 45 commits
  behind. `core.sshCommand` in a global gitconfig reproduces it with no
  environment variable at all, which is why `GIT_SSH_COMMAND` is **not** added
  to the scrub: that would close one spelling of a config the gate reads on
  purpose.

> This row was refuted **three times**. Each counterexample was driven by an
> independent reviewer, and each shipped a signed, 45-commit-stale build with a
> flat `OK`, rc=0. All three share one shape: the check DERIVED a filesystem
> path from the url, the derivation produced something unresolvable, and
> "cannot tell" was read as "not self-referential" — a fail-open on a gate
> input.
>
> 1. With a **relative** `origin` url, the check resolved the path against the
>    shell's working directory while `git -C "$ROOT" fetch` resolved it against
>    the repo root — so the same configuration gave two different verdicts
>    depending on where `make-dmg.sh` was invoked from, and
>    `npm run dist:bundled` invokes it from `desktop/`, not the root.
> 2. With `origin` set to a **linked worktree**, the check asked
>    `--absolute-git-dir`, which is per-worktree: `$ROOT` answers `$ROOT/.git`
>    and its worktree answers `$ROOT/.git/worktrees/<name>`. Different strings,
>    so the check stayed silent — while the two share one object store and one
>    `refs/heads/main`, so the fetch returned the build checkout's own stale
>    branch and the gate printed `origin/main = <$ROOT's own ref>`.
> 3. With `origin = file://localhost$ROOT`, `${remote_url#file://}` stripped the
>    **scheme** but not the **authority**, leaving `localhost/…` — no leading
>    slash, so it was joined onto `$ROOT` as if it were relative, and `[ -d … ]`
>    on the result was false. A fourth vector turned up while fixing that one
>    and never shipped: git **percent-decodes** a `file://` path and the
>    filesystem does not, so `file:///…/%52oot` is `$ROOT` to git and a
>    non-existent directory to `[ -d ]` — no host required.
>
> Three derivations, four defects, every one of them the same shape: that is
> the argument for having no derivation. The check asks the remote instead, and
> the shape of the question — "can you see a ref I made a moment ago?" — has no
> arithmetic in it to get wrong.
>
> Tests drive `origin = .` (must be refused), `origin = ..` (must *not* be, or
> the check would be rejecting everything), both worktree orientations, the two
> worktree orientations again through a `file://localhost` url, an `insteadOf`
> rewrite, 18 spellings of the build checkout's own path, and a genuine remote
> reached over `file://localhost` (which must still ship, or the check would be
> passing every test above by refusing everything).

> **Why this took six reviews to surface.** Every one of those tests ran with
> `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` pinned to `/dev/null` and no
> `GIT_*` redirect set at all, while a real `npm run dist:bundled` inherits the
> operator's whole environment and their `~/.gitconfig`. **The harness was
> cleaner than the thing it was testing**, so a defect living in what git
> *inherits* could not be seen from it — not by the 18 driven url spellings,
> and not by any prior round's mutation testing. The suite is still hermetic by
> default (a
> test that reads the machine's real config is a test that means something
> different on each machine); the fix is that it now also hands the block an
> explicitly **hostile** environment and an explicitly **hostile** global
> config, on purpose, and asserts the refusal.
>
> What those cases still do **not** cover, listed rather than implied: `PATH`
> (which chooses the `git` binary itself, and cannot be fixed from inside the
> script), `GIT_SSH_COMMAND` — **this line used to say "on an ssh origin it
> reaches a *different* repository, which is the residual above", and that is
> false**: on an ssh origin it is what *answers*, so it **blinds** the probe,
> driven to a signed 45-behind rc=0 ship and now pinned by
> `test_the_declared_residual_an_ssh_redirect_still_walks_through`; the clause
> that survives is "no effect on a local origin", separately re-confirmed —
> `GIT_PROTOCOL` and `GIT_TRACE*` (no answer here depends on them), and the
> **system** gitconfig (same mechanism as the global one and caught by the same
> control, but not separately driven).

### What an **unsigned** build is compared against

Its own checkout — the tautology described above. That is deliberate: an
unsigned build must keep working offline, and its filename and the banner the
script prints already say it is not distributable.

So it can never report verified provenance. `make-dmg.sh` passes
`--repo-built-this-artefact`, which tells the verifier the expectation came from
the checkout that *built* the artifact, and **`verify_artefact.py`'s** exit code
is 3, `OK (provenance NOT verified)`, for **every** unsigned build — clean or
dirty. `make-dmg.sh` itself still exits 0 and carries the verdict in the final
line and the filename instead: `OK: …-UNSIGNED.dmg (…) [unsigned; provenance NOT
verified]` — see "…and why `make-dmg.sh` still exits 0 on 3" under **Exit
codes** below.

> An earlier version of this section said the unsigned verdict came back as
> exit code 3 "and never as a flat `OK`" **because of `--allow-dirty`**. That
> was false. `--allow-dirty` downgrades only when the stamp records
> `dirty=yes`, and the checkout that caused the incident was **clean** — the
> case that actually burned us. Driven: unsigned, clean, 45 commits behind,
> board reading `THIS IS THE WRONG BOARD` → `verify-artefact: OK — built from
> 0ca24000e3ac, clean tree`, `OK: …x.dmg [unsigned]`, rc=0, no caveat anywhere.

The comparison is still **run**, and it is still worth running: a stamp naming a
commit this checkout never built is a FAILURE. That is the one thing a
self-referential comparison genuinely establishes — the **board** was not
modified between being built and being packaged. It establishes nothing about
which source the checkout was on.

> This sentence said "the **DMG** was not modified", here and in
> `make-dmg.sh` and `verify_artefact.py`, and it was **false**. `board_sha256`
> is computed over the files under the bundled `dist/` and nothing else, so an
> `_internal/evil.so` planted beside the frozen server — with a
> `migrations/003_backdoor.sql` and a replaced `app.asar` for company —
> verifies at a flat `OK`, rc=0. Driven by the seventh review.
>
> **What is outside the digest**: the frozen server's own code and its
> `_internal/` tree, `migrations/`, the Electron code, `app.asar`, and the
> `.app` bundle's metadata. Only `web/dist/` is inside it.
>
> **Widening it is out of scope**, and not only because it was not asked for.
> `npm run dist:bundled` runs `build-installer.sh` — which writes the stamp —
> and *then* `electron-builder`, which **signs the `.app`** and in doing so
> rewrites the Mach-O binaries under `_internal/`. A digest taken over that tree
> at stamp time would disagree with itself on every signed build. The thing that
> covers that tree is the `.app`'s own **codesign seal**; the DMG's signature
> and its `spctl` assessment are checked by `make-dmg.sh`.

### Exit codes

| code | meaning |
| --- | --- |
| 0 | verified — the stamped commit was compared against a named commit and matched, from a clean tree |
| 1 | FAILED |
| 2 | usage error (e.g. `--expect-commit ""`, or `--expect-commit` together with `--repo-built-this-artefact`) |
| 3 | every check that ran passed, and at least one was **deliberately weakened** by `--allow-dirty`, `--allow-unknown-commit` or `--repo-built-this-artefact` |

3 exists because a caller that reads `$?` — which is every caller that is a
script — could not otherwise tell a fully verified artifact apart from one whose
provenance was never established. The caveats were printed as stderr prose, and
prose is not a signal a pipeline can act on.

#### …and why `make-dmg.sh` still exits 0 on 3

`make-dmg.sh` collapses `verify_artefact.py`'s 3 to 0 for non-signed builds, so
its own `$?` does not carry the distinction the table above exists to provide.
A fourth review flagged that as the same shape one layer out. It is deliberate,
and the reasoning is recorded here rather than left implicit:

* the distinction is already machine-readable in the **filename**, and not as
  an approximation — as the *same predicate*. `SIGN_MODE` and `ARTIFACT_TAG`
  are two reads of one `signingPlan()` call in `desktop/signing.cjs`, where
  `artifactTag` is `""` if and only if the mode is `signed`
  (`desktop/signing.test.mjs` pins all three modes). The flag that makes 3
  reachable, `--repo-built-this-artefact`, is passed under exactly the same
  condition;
* the collapse is guarded on the mode, so a **signed** build that somehow
  returned 3 is not collapsed — it exits 1
  (`test_rc3_from_a_SIGNED_build_is_never_collapsed`);
* therefore `exit 0` from `make-dmg.sh` means *verified against `origin/main`*
  when the artifact's name carries no tag, and *provenance not verified* when it
  carries `-UNSIGNED` or `-UNNOTARIZED`. There is no third state, and a caller
  already has to read that name to decide distributability.

The alternative — propagating 3 out of `make-dmg.sh` — was rejected because a
developer with no signing credentials in their environment gets an unsigned
build from **every** `npm run dist:bundled`, so this script would exit non-zero
on every one of them, and a tool that always fails teaches people to append
`|| true`. That is the exact fail-open shape (`git fetch … || true`) this work
exists to delete.

> An earlier version of this paragraph said "every build is unsigned until the
> Apple Developer membership exists". That is no longer true — the notarization
> credentials are stored and were validated by Apple — and it was never the
> load-bearing part: `signingPlan()` reads the **environment** alone, so the
> argument is about what is set on a developer's machine, not about the state of
> an account.

This argument rests entirely on the filename carrying the mode. If
`make-dmg.sh` ever emits an artifact whose name does not, it is void and the
exit code must be propagated instead.

The stamp deliberately does **not** record the branch name. A branch name is
free text a human chose (`fix/<tracker-id>-<customer>-outage`) and this file is
handed to third parties; the commit sha carries the same provenance without the
disclosure. The
verifier rejects any stamp field it does not recognise, so a future convenience
field cannot quietly become a channel.

`verify_artefact.py` takes a **directory**, not a `.dmg` — a mounted image, an
unpacked `.app`, or a Windows resources tree all go through the same check:

**`--repo .` is only a real check if `.` is not the checkout that built the
DMG.** If it is, the comparison is the tautology this whole page is about, and
you should add `--repo-built-this-artefact` so the verdict says so (exit 3)
rather than reading as verification. To check a DMG against a *source*, pass
`--expect-commit <sha>` resolved from the release remote.

```bash
# exactly one DMG is expected in packaging/dist/ after `npm run dist`
hdiutil attach -nobrowse -readonly packaging/dist/no_human-*.dmg
python scripts/verify_artefact.py /Volumes/no_human --repo . \
  --require 'a string that must be in this release'
hdiutil detach /Volumes/no_human
```

Every field it reads must be present, non-empty and well-formed. A blank value
is treated as broken provenance, never as "nothing to check" — the first version
of this gate passed a stale board because an empty `board_sha256` skipped the
content check, and the build wrote that empty value on any host without
`shasum`. Both ends now abort instead.

`board_sha256` covers **paths as well as contents**: each file contributes a
`<sha256 of its bytes>  <path relative to dist/>` line, and the digest is the
hash of those lines sorted under `LC_ALL=C`. The first version hashed only the
content digests, so exchanging two files' paths inside the board — leaving the
stale file served at the entry point — produced an identical digest and passed.
`build-installer.sh` and `verify_artefact.py` must build byte-identical lines
here; if they ever diverge, every real artifact fails its own check, so
`tests/test_verify_artefact.py` pins the parity on a tree with spaces, dotfiles,
subdirectories, non-ASCII names, an empty file and duplicate contents, under
both `shasum` and `sha256sum` and under both collations.

If you run it against something with **no usable history** — a clone of the
public repo, whose export has its own fresh `git init`, or an unpacked release
tarball with no `.git` at all — it cannot resolve the sha in the stamp. It says
which of those it is, specifically, rather than claiming staleness; pass
`--allow-unknown-commit` to check the artifact's internal integrity, and it will
print `PROVENANCE NOT VERIFIED` and exit **3**.

Step 2 has no build-time assertion of its own: electron-builder's `files` is a
literal allowlist, and a file omitted there is simply absent from `app.asar`
with no error — which once shipped an app that could not launch at all.
`desktop/packagedFiles.test.mjs` guards that in the test suite instead, so run
`npm test` in `desktop/` before building.

## What a friend does

The build is **unsigned**, so Gatekeeper refuses a double-click. Right-click the
app and choose **Open**, then confirm once. After that it launches normally.

On **Windows** the installer is `no_human-<version>-UNSIGNED.exe` (SmartScreen:
More info → Run anyway; see `WINDOWS.md`). On **Linux** install the `.deb`
(`sudo apt install ./no_human-<version>-linux-amd64.deb`) or run the AppImage;
see `LINUX.md` for the frictions Ubuntu 22.04+/24.04 add to the AppImage path.
The Claude Code CLI has to be installed on that machine on every platform.

**Claude Code must be installed on that Mac.** no_human runs coding tasks by
launching the `claude` CLI through the Agent SDK; the CLI is *not* bundled (it is
244 MB, and a friend needs it anyway for the `claude setup-token` step below).
Without it the board loads and looks perfectly healthy while every task fails
with `CLINotFoundError`. The app widens the server's `PATH` to cover the usual
install locations — including `/opt/homebrew/bin`, which is in neither launchd's
`PATH` nor the SDK's own fallback list — but the CLI has to exist somewhere.

On first launch the app asks to connect Claude and offers two credential
types. The default is a **subscription token** (personal or enterprise):
create one with `claude setup-token` and paste it in. The alternative is an
**Anthropic API key**, which bills the operator's own Anthropic account —
select "Anthropic API key" on that screen and paste an `sk-ant-api…` key.
Either credential is written to `~/.no_human/.env` (mode 600) on that machine
only, and a run bills exactly the one configured path: a key pasted into the
subscription field is rejected, and stray billing variables are scrubbed at
startup.

## Verify your install is real

**Decision (F6):** the DMG reader's install-health check is `nh doctor` — path
A (a real command reachable from the installed build), not path B (new prose
instructions in place of a command). It needed **no new code**: `doctor` is
already a `@cli.command` in `src/no_human/cli/commands.py`, and
`packaging/nh_entry.py` statically imports `no_human.cli.commands` as a
PyInstaller analysis anchor, so every subcommand — `doctor` included — is
already in the frozen `nh` binary the app ships. The gap `docs/quickstart.md`
had was pointing at it only through `nh init` in section 3, which it tells
`.dmg` readers to skip; the fix is routing them to the command directly
instead of building a second one.

Run it from a Terminal, using the binary the app already bundles — no source
checkout, no `uv run`:

```bash
/Applications/no_human.app/Contents/Resources/nh-server/nh doctor
```

On **Windows** the same command, at the same place in the installed tree. The
nesting differs only because the two platforms nest an app differently — macOS
puts resources inside the `.app` bundle, Windows puts them beside the `.exe`:

```powershell
& "$env:LOCALAPPDATA\Programs\no-human-desktop\resources\nh-server\nh.exe" doctor
```

The install directory is `no-human-desktop` (from the package `name`) while the
app and the "Add or remove programs" entry read `no_human` — that is
electron-builder's convention, not a mis-install. The NSIS installer is
per-user, so it needs no administrator prompt.

On **Linux** the `.deb` installs the app under `/opt/no_human`, so the same
command reads (an extracted AppImage puts it under `squashfs-root/`):

```bash
/opt/no_human/resources/nh-server/nh doctor
```

The Linux build, its two formats and their known frictions are in `LINUX.md`.

Verified output on Windows 11, from the installed artifact — same semantics,
same exit code as macOS:

```
coding backend - claude CLI: ...\.local\bin\claude.EXE
mechanism liveness (lifetime firings)
  planning                0  last: never  zero = planning disabled or no task got past intake
  ...
no contradictions, no evidence gaps
EXIT CODE: 0
```

Full Windows build, divergence and acceptance record: `docs/WINDOWS.md`.

What you should see: a three-line verdict — `install healthy — no
contradictions, no evidence gaps` (exit 0) or `install needs attention` over a
red `contradictions` block naming what's wrong (exit 1) — then the `auth` and
`coding backend — claude CLI: <path>` lines and a one-line mechanism-liveness
summary. `nh doctor --verbose` prints the full per-mechanism table.

The captured runs below predate that summary (they were taken when the table
printed by default); the verdict, the backend line and both exit codes are
unchanged, and the table itself is one flag away.

**Real output**, captured by building the frozen bundle in this environment
(`bash packaging/build-installer.sh`, then running
`packaging/dist/nh-server/nh doctor` directly — the same binary that lands at
`no_human.app/Contents/Resources/nh-server/nh`) and running it both with and
without the `claude` CLI on `PATH`:

Healthy (exit 0):

```
coding backend — claude CLI: ~/.local/bin/claude
mechanism liveness (lifetime firings)
  planning                0  last: never  zero = planning disabled or no task got past intake
  ...
no contradictions, no evidence gaps
```

Unhealthy (exit 1) — `claude` CLI not on `PATH`, the single most common friend
failure (see "What a friend does" above):

```
coding backend — claude CLI: not found
mechanism liveness (lifetime firings)
  ...
contradictions — evidence of activity without evidence of the mechanism:
  ✗ CODING BACKEND UNUSABLE: the `claude` CLI is not installed or not on PATH —
the Claude Agent SDK shells out to it for every task, so every task would fail
at launch. Install it with: npm install -g @anthropic-ai/claude-code
```

On a brand-new install every mechanism reads `0  last: never` — expected,
nothing has run yet. Re-run `nh doctor` after your first task to see counters
move, and whenever something behaves oddly.

**Scope of this evidence:** the two transcripts above are real, run against
the actual frozen PyInstaller bundle (`packaging/nh-server.spec` /
`packaging/build-installer.sh`), not inferred from source. What was **not**
run in this environment: `electron-builder --mac` and `make-dmg.sh` — no
`electron-builder` was installed here, and installing new packages is out of
scope for a docs-only change. So the exact path
`no_human.app/Contents/Resources/nh-server/nh` is confirmed by
`electron-builder.config.cjs`'s `extraResources` mapping and by "Verifying a
build" below (which does mount and run a built DMG this way), but was not
re-mounted from a freshly-built signed DMG for this change.

**Gatekeeper on the nested binary, not just the app.** If you extract or
`open` the `nh` binary directly instead of running it from a Terminal path,
Gatekeeper can refuse *it* independently of the app's own right-click-Open
step: a quarantined copy gives `spctl -a -t exec` a `rejected` verdict and
`open` fails with error `-128`. Invoking the identical binary as a shell
command (`.../nh doctor`, as documented above) is unaffected by that check —
verified on a deliberately quarantined copy of the frozen binary built here,
which still ran and exited 0 as a shell command. Always run it as a path in
Terminal; don't double-click or `open` the nested binary.

## Why the packaging looks the way it does

Three constraints drove the design, each found by measuring a real build:

- **`collect_submodules`, not `collect_all`.** `collect_all()` copies a package's
  source tree in as *data* — that put 88 readable `.py` files (48 `websockets`,
  40 `uvicorn`) in the bundle. `collect_submodules()` bundles the same modules as
  bytecode inside the PYZ.
- **`web/dist` and `migrations/` are placed at the bundle root by a script, not
  by PyInstaller `datas`.** The server resolves both with
  `Path(__file__).resolve().parents[3]`, which under a frozen onedir build is the
  bundle root. PyInstaller 6 puts every `datas` entry under `_internal` and
  rejects a `..` destination outright, so `datas` cannot reach the right place.
  Without `migrations/` the server aborts on a fresh install with
  `sqlite3.OperationalError: no such table: tasks`.
- **The DMG is built with `hdiutil`, not electron-builder's dmg target.** That
  target drives Finder over AppleScript and then dies in `hdiutil detach`.
  `hdiutil create -srcfolder` on a directory containing an `.app` fails with
  `Resource busy`. What works: create a read-write image, copy into it,
  force-detach, convert. The force is required because an endpoint-security
  data-protection agent holds a volume containing an app bundle.

## Known limits

- **The packaged build cannot run the repro gate — and fails it *confidently*.**
  `src/no_human/testing/repro_gate.py` and `src/no_human/eval/replay.py` invoke
  `[sys.executable, "-m", "pytest", …]`; in a frozen bundle `sys.executable` is
  the `nh` binary, not a Python interpreter. The subprocess therefore *launches*
  (so the gate's `OSError` handler never fires), the CLI rejects `-m` and exits
  2, and `ran = returncode != 5` makes that look like a genuine failing test
  rather than a broken environment. Every bugfix task in a frozen build gets a
  false "still fails" verdict. The bundle serves the board and the API; running a
  target repo's test suite needs a real Python install. (The fix belongs in
  `repro_gate.py`, which is outside this installer's scope.)
- **`nh test` is a developer command** and is not part of this build — it shells
  out to `scripts/run_tests.sh`, which the bundle does not ship.
- **Built for the host architecture.** No `mac.target.arch` is set, so the build
  matches whatever machine produced it, and it is not a universal binary — an
  arm64 build will not run on an Intel Mac.
- **A wrong-but-well-formed token is only caught at boot.** The token is checked
  for shape, not validity. If the server then fails to start, use **File →
  Re-enter Claude Token…** (or the link on the error screen) to replace it.
- **Unsigned and un-notarised.** Hence the right-click-Open step above.
- **The server is spawned in its own process group** (`detached`), so stopping it
  takes its workers down too. The trade-off is that a *crash* of the desktop app
  (as opposed to a clean quit) leaves `nh` running, and Ctrl-C in a terminal that
  launched the app no longer reaches it — quit from the app, or stop `nh` itself.

## Verifying a build

The build script's checks run against the frozen tree. To check the shipped DMG
itself, mount it and drive it by hand. Note the deliberate non-default port: the
default is 8420, and a dev server already bound there would answer the `curl`
and make the check pass without proving anything.

```bash
hdiutil attach packaging/dist/no_human-*-UNSIGNED.dmg -nobrowse -readonly
find /Volumes/no_human -name '*.py'            # must print nothing

H=$(mktemp -d); mkdir -p "$H/.no_human"
printf 'server:\n  port: 18994\n' > "$H/.no_human/config.yaml"
printf 'CLAUDE_CODE_OAUTH_TOKEN=dummy\n' > "$H/.no_human/.env"
env -i HOME="$H" PATH=/usr/bin:/bin \
  /Volumes/no_human/no_human.app/Contents/Resources/nh-server/nh start --no-open &
# The frozen server takes a few seconds to bind; poll rather than curling once.
for i in $(seq 1 40); do sleep 1; \
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18994/api/queue/health); \
  [ "$code" = "200" ] && break; done; echo "$code"
hdiutil detach /Volumes/no_human -force
```

The server refuses to boot without a token, hence the dummy one above; the app's
first-run screen supplies a real one for an actual user. That checks the frozen
server — it does not launch the Electron app, which is what `npm test` in
`desktop/` covers.
