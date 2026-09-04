# Assumptions

_Harness-captured record for task `ed786a65`, commit `2fc637a7f54595de12161db17ae12a17ea9c48aa` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

> ⚠️ **Unresolved:** You've hit your session limit · resets 1:40pm (Asia/Jerusalem) ('personal2' subscription)

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** What does the ready_path parameter represent and how should default_manifest() use it to determine app readiness? Should it: (a) navigate the browser to base_url + ready_path as a URL endpoint, (b) poll a CSS/XPath selector, or (c) something else from profile config? How is readiness determined—navigating to that path and waiting for page load, or separately verifying the endpoint responds? **A:** Option (a): navigate the browser to base_url + ready_path as a URL endpoint. The task explicitly states 'goto base_url+ready_path, wait for network idle', indicating a simple navigation-based readiness check. Readiness is determined by constructing the full URL from the base and path parameters, navigating to it, and waiting for the page to reach network-idle state before proceeding with the scree _(assumption)_
- **Q:** Which browser automation framework is already in use in tests/test_ui_evidence*.py (Selenium WebDriver, Playwright, or other)? This determines how to stub the browser seam in new tests and implement screenshot capture. **A:** HUMAN-GATED: not self-answerable
- **Q:** When default_manifest() captures the second screenshot (if diff touched web/src), should it wait for another network-idle cycle before screenshotting, wait a fixed delay, or screenshot immediately after the first one? **A:** Wait for another network-idle cycle before screenshotting. Since no user interaction occurs and the task emphasizes 'keep it dumb and reliable—no selectors that can rot', reusing the same network-idle detection used for the first screenshot is the most maintainable approach. Fixed delays are brittle; network-idle is proven and framework-native, ensuring the page is fully settled regardless of app _(assumption)_
- **Q:** Should RELEASE_MANIFEST re-pinning mentioned in the acceptance criteria be automated within the code (e.g., calling a tool to re-pin dependencies), or is it a manual post-step by the requester? **A:** HUMAN-GATED: not self-answerable

</details>

