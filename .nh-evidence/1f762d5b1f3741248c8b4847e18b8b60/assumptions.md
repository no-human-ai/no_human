# Assumptions

_Harness-captured record for task `1f762d5b`, commit `9178907ee479b7dad0c0185b39dd39e2b2ea04ec` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

> ⚠️ **Unresolved:** mechanical derived-artefact conflict resolution failed: https://github.com/no-human-ai/no_human/pull/42 step=regenerate

> ⚠️ **Open question:** PR https://github.com/no-human-ai/no_human/pull/42 conflicts only in derived artefact(s) (RELEASE_MANIFEST.txt) but mechanical resolution failed at step 'regenerate'. Advise, or take over?

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** How should failed integration probes be re-triggered 'before the next poll cycle uses the integration'? Should re-probes happen immediately upon detecting a failure, on a separate timer, or opportunistically as part of the next scheduled health-check cycle? **A:** (unanswered)
- **Q:** What HTTP timeout value (in seconds) should integration health probes use? **A:** (unanswered)
- **Q:** What format should the 'detail' field have for different probe failures (e.g., just '404', '404 Unauthorized', or '404 Unauthorized at jira.example.com')? **A:** (unanswered)
- **Q:** Should all 9 integration types (jira, github, gitlab, slack, linear, monday, teams, jenkins, circleci) be fully probed in this implementation, or is there a priority subset for MVP? **A:** (unanswered)

</details>

