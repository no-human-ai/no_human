# Assumptions

_Harness-captured record for task `2b62483a`, commit `6164ddcc213d1168d554ac51a2d6e223928be0a0` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 6 assumptions made on your behalf — verify at review</summary>

- **Q:** Which subscription tiers or user segments should display token usage? Should all subscription users see this, or only specific tiers (e.g., Pro, Enterprise, etc.)? **A:** All subscription users (paid tiers). The feature targets subscription users collectively; absent evidence of tier-specific gating in the original spec, the implementation should apply uniformly to all users with active paid subscriptions, excluding free-tier accounts. _(assumption)_
- **Q:** Which specific UI pages, dashboard sections, and API endpoints currently display dollar pricing and must be modified to show token usage instead? **A:** Primary targets: user dashboard billing/usage card or dedicated usage analytics page, account settings usage section, and REST API endpoints for usage retrieval (e.g., GET /api/usage or /api/billing/usage). Modify the main display components that currently render dollar estimates to render token counts instead, and ensure corresponding API endpoints return token data. _(assumption)_
- **Q:** What time period or window should the token count represent: cumulative all-time, current calendar month, or user's current billing cycle? **A:** Current billing cycle (typically calendar month or subscription-anniversary month). This aligns with standard SaaS billing practice and allows users to understand consumption within their charging period. Query aggregation should sum tokens from billing-cycle start to now. _(assumption)_
- **Q:** Should the legacy dollar-based billing API endpoints and UI displays be retained for backward compatibility with existing integrations, or completely removed? **A:** Retain legacy dollar-based API endpoints for backward compatibility but mark them deprecated. Existing client integrations depend on them; a hard removal would break deployments. Gradual deprecation (3–6 month notice) allows partners to migrate to token-based endpoints before sunset. _(assumption)_
- **Q:** Do you require access to production billing systems and live user data for development, testing, and verification? **A:** HUMAN-GATED: not self-answerable
- **Q:** Should historical billing records prior to this deployment retroactively show token usage, or should tokens only apply to usage going forward? **A:** HUMAN-GATED: not self-answerable

</details>

