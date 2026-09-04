# no_human docs

| Doc | What it covers |
|-----|----------------|
| [**quickstart.md**](quickstart.md) | **Start here** — from zero to first task in 5 minutes |
| [configuration.md](configuration.md) | `~/.no_human/config.yaml`, every section + default, plus the model picker (`GET /api/models`, `PUT /api/config/models`, `nh config models`) |
| [verification.md](verification.md) | The gates that stop a broken change, the bounded loop, the limits |
| [security.md](security.md) | Subscription-auth boundary, the never-merge rule, guards |
| [INTEGRATIONS_LEGAL.md](INTEGRATIONS_LEGAL.md) | The recorded legal/compliance position for each third-party integration (Codex auth, sourcing, what is unresolved) |
| [adapters.md](adapters.md) | Intake (TRACKER/GitHub/GitLab), context, VCS, CI backends |
| [BACKENDS.md](BACKENDS.md) | The three coding backends (`claude`, `codex`, `local`): switching, credentials, per-mode defaults |
| [eval.md](eval.md) | Golden set, replay scoring, scorecard/CI gate, shadow mode |
| [blockers.md](blockers.md) | Part 22 taxonomy, escalation, wake watcher, `nh reply` |
| [KNOWN_ISSUES.md](KNOWN_ISSUES.md) | Reproduced defects that are not fixed yet, and what a fix must prove |
| [INSTALLER.md](INSTALLER.md) | The packaged apps: how the frozen server + shell are built, verified and installed (all platforms) |
| [WINDOWS.md](WINDOWS.md) | The Windows app: audit, divergences from macOS, real-Windows test and acceptance runs |
| [LINUX.md](LINUX.md) | The Linux app (`.deb` + AppImage): build, divergences from macOS, install/verify, acceptance status |
