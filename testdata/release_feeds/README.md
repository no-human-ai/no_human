# Fixtures for `tests/test_release_feeds_gate.py`

Real captures, not synthetic data, so the gate is proven against what the
release process actually shipped — per this ticket's acceptance criterion
that the fix be verified against a real published release's asset list, not
only a local dist directory.

| file | source | command | captured |
|---|---|---|---|
| `v0.2.2.assets.json` | `no-human-ai/no_human` release `v0.2.2` | `gh release view v0.2.2 --repo no-human-ai/no_human --json tagName,isDraft,isPrerelease,assets` (then `python3 -m json.tool`) | 2026-09-15 |
| `v0.2.2.latest-mac.yml` | same release | `gh release download v0.2.2 --repo no-human-ai/no_human --pattern 'latest-mac.yml' --dir <tmp> --clobber` | 2026-09-15 |
| `v0.2.0.assets.json` | release `v0.2.0` | `gh release view v0.2.0 --repo no-human-ai/no_human --json tagName,isDraft,isPrerelease,assets` | 2026-09-15 |
| `v0.1.7.assets.json` | release `v0.1.7` | `gh release view v0.1.7 --repo no-human-ai/no_human --json tagName,isDraft,isPrerelease,assets` | 2026-09-15 |
| `good.assets.json` | copy of `v0.2.2.assets.json` | — | 2026-09-15 |
| `good.latest-mac.yml` | `v0.2.2.latest-mac.yml` with the `no_human-0.2.2-arm64.dmg` `files:` row removed | — | 2026-09-15 |

What each demonstrates:

* **`v0.2.2.assets.json` + `v0.2.2.latest-mac.yml`** — the actual defect this
  ticket is about: `latest-mac.yml`'s second `files:` entry names
  `no_human-0.2.2-arm64.dmg`, electron-builder's own (never-notarized, never
  uploaded) dmg. The real release's assets list carries `no_human-0.2.2.dmg`
  (the notarized `make-dmg.sh` output) instead — a different filename, so the
  URL in the feed 404s. `path:` still points at the zip, which IS published,
  which is why the update path itself has kept working.
* **`good.assets.json` + `good.latest-mac.yml`** — the same release with the
  phantom row removed: the state this ticket's fix (dropping `dmg` from
  `mac.target`) produces going forward. Used as the passing counterpart so the
  gate is shown to both fail on the real broken feed and pass on the corrected
  one, not just the former.
* **`v0.2.0.assets.json`** — ships `no_human-0.2.0-UNSIGNED.exe` (a Windows
  asset) but no `latest.yml` at all: the missing-feed shape, from the release
  that actually shipped it.
* **`v0.1.7.assets.json`** — ships `no_human-0.1.7-linux-x86_64.AppImage`
  (a Linux asset) but no `latest-linux.yml`: same shape, the other platform.
