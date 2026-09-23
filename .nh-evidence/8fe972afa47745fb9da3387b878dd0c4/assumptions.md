# Assumptions

_Harness-captured record for task `8fe972af`, commit `446107e73eff0e976d97137b40dbc8103f050f1d` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

> ⚠️ **Unresolved:** You've hit your weekly limit · resets 5am (Asia/Jerusalem) ('personal' subscription)

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** What defines 'the agent's own marker' referenced in the acceptance criteria for filtering? Is it a string pattern typically found in review bodies, a configuration value, or something derived from the reviewing user/bot identity? **A:** A string pattern or identifier constant embedded in review/comment bodies that the agent includes when it authors its own feedback (e.g., a bot signature, footer marker, or unique string literal). This marker allows the system to recognize and filter out the agent's own reviews to prevent self-referential feedback loops. _(assumption)_
- **Q:** Where should the bot opt-in configuration be stored (e.g., a specific config file path, environment variable name, or code constant), and what structure should it use (e.g., a list of bot login strings, a YAML/JSON dict, or regex patterns)? **A:** A configuration file (YAML or JSON format, stored in the project root or standard config directory) containing a list of bot login strings that should be treated as human feedback despite having user.type == 'Bot'. The structure should support a simple array or dict of login names for efficient lookup. Alternatively, as an environment variable if following that pattern elsewhere in the project. _(assumption)_
- **Q:** Should the new bot opt-in configuration replace the existing `ignore_comment_authors` mechanism, extend it, or be implemented as a completely separate new setting? **A:** Implement as a separate new configuration setting alongside ignore_comment_authors rather than replacing or merging with it. They serve complementary but opposite purposes: ignore_comment_authors blocks feedback from specified users, while the bot opt-in explicitly allows certain bot-typed users. Keeping them separate preserves clarity and makes config intent explicit. _(assumption)_

</details>

