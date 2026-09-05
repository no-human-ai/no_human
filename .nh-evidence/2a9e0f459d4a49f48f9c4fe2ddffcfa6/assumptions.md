# Assumptions

_Harness-captured record for task `2a9e0f45`, commit `9015860f295ae5657ab2e6d861f3bbaf589e447d` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** The task says the per-PR figure should 'present a token-basis or mark clearly as an estimate that is NOT primary.' Should it display tokens as the primary metric (e.g., '200 tokens (~$0.81 est.)') to match the drawer and card, or keep dollars primary but mark it clearly as an estimate? **A:** Tokens primary, dollars secondary as '~$X est.' — match deriveSpendDisplay's 'tokens lead, dollars marked est.' convention explicitly stated for subscription mode. _(assumption)_
- **Q:** Should the Kanban card cost chip show only tokens for subscription mode (e.g., '200 tokens'), or include dollars as secondary (e.g., '200 tokens (~$0.81 est.)')? **A:** For subscription: tokens primary with dollars optional secondary format (e.g., '200 tokens (~$0.81 est.)' or '200 tokens' alone); for api_key: dollars as current behavior. _(assumption)_
- **Q:** Are there authMode values beyond 'api_key' and 'subscription' (e.g., 'free', 'trial', 'enterprise') that need distinct token/dollar handling in the conditional logic? **A:** Only api_key and subscription/absent (non-api_key); no other modes explicitly mentioned. Treat absent/undefined as non-api_key (subscription-like). _(assumption)_
- **Q:** Should the token/dollar display pattern be implemented by extending `deriveSpendDisplay`, or reimplemented locally in the Kanban card and per-PR components? **A:** Use pricingIsReal(authMode) conditional check pattern locally in each component (card, drawer, per-PR); deriveSpendDisplay is designed for spend tiles, reuse its logic pattern (tokens first, dollars marked est.) but implement per-component to avoid forced dependency. _(assumption)_

</details>

