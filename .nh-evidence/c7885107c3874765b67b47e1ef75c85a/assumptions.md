# Assumptions

_Harness-captured record for task `c7885107`, commit `8ca80901d9ba6d678edd6ce2f8df1fd466eeb4c0` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

> ⚠️ **Unresolved:** SDK reported HTTP 403 — infrastructure/auth, not work ('personal3' subscription)

<details><summary>⚠️ 5 assumptions made on your behalf — verify at review</summary>

- **Q:** Should the gate validate all platform update feeds (latest.yml for Windows, latest-mac.yml for Mac, latest-linux.yml for Linux) or only the Mac feed? **A:** Validate all platform update feeds (latest.yml for Windows, latest-mac.yml for Mac, latest-linux.yml for Linux). The task describes the same class of bug hitting Windows in two prior releases (missing latest.yml in v0.1.7 and v0.2.0), establishing a pattern. A gate that prevents this across all platforms at once is cheaper than repeating separate fixes per platform, and directly addresses the acce _(assumption)_
- **Q:** Should the team retroactively fix and re-release assets with corrected feeds for already-published problematic releases (v0.2.2, v0.2.3, v0.1.7, v0.2.0), or should the gate only prevent future occurrences? **A:** HUMAN-GATED: not self-answerable
- **Q:** Should the electron-builder dmg issue be fixed by filtering it from the feed at build time (option a), or by correcting the feed just before/during publish (option c)? **A:** Filter the electron-builder dmg from the feed at build time (option a). The decision about which dmg to publish is inherently a build-time concern—the repo's documented mac-release guidance already states 'ship only the make-dmg.sh output.' Filtering at build time prevents the unwanted entry from reaching downstream consumers; correcting it at publish time is a downstream patch on a build-time dec _(assumption)_
- **Q:** When the gate detects a mismatch—unpublished URL in feed or expected feed missing—should it block release publication entirely, or warn and allow manual override? **A:** Block release publication entirely (hard blocker). The task states the pattern has shipped twice before despite hand-checking, and warns 'do not let that reasoning carry it to a third release.' Soft enforcement (advisory with override) has already failed; only mandatory automated validation will prevent the fourth repetition. _(assumption)_
- **Q:** For the gate's 'missing feed' check: should it expect feeds for all supported platforms (Mac, Windows, Linux) to exist in every release, or only feeds for platforms actually present in that release's assets? **A:** Expect feeds only for platforms actually present in that release's assets. Feeds are output artifacts of the build for platforms being shipped; requiring feeds for platforms not included in a release is false validation and conflates 'feed completeness' with 'platform completeness.' The check should be: for each platform with assets, the corresponding feed exists and lists only published assets. _(assumption)_

</details>

