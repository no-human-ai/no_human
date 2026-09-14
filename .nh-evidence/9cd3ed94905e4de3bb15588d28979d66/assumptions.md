# Assumptions

_Harness-captured record for task `9cd3ed94`, commit `9fa2505bf911bfac832c37d58c6725cfa71a9bda` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** For the email persistence after reload: should we rehydrate the email field from the server when the wizard mounts, consult the server when validating for completion, or do both? **A:** Rehydrate the email field from the server when the wizard mounts. This ensures the UI state matches the server's persistent data: the stepper already shows completion based on server response, so the field should be populated from the same source. This is simpler than consulting the server at validation time, maintains UI consistency (if the step shows done, the field should be populated), and ali _(assumption)_
- **Q:** When the completion paths reject due to missing email (e.g., after a reload), how should the error be shown to the user: toast notification, modal dialog, inline error message/banner, form field validation error, or another mechanism? **A:** Display the error as an inline error message or banner within the wizard UI when completion is rejected. This allows the user to see the specific issue while remaining on the form to resolve it, providing clear recovery guidance. This is standard form validation UX and directly addresses the current problem where the thrown error has no render site, leaving users with no feedback on why they canno _(assumption)_
- **Q:** For the reload test case in the acceptance criteria: should it be added to packaging/linux-acceptance.mjs (acceptance suite), to Jest tests in the web module, or to both? **A:** Add the test to both Jest tests in the web module and to packaging/linux-acceptance.mjs. Jest tests provide fast feedback during development with server mocking, while the acceptance suite provides end-to-end validation of the shipped packaged product. The acceptance criteria's requirement for a test that 'fails when the fix is reverted' suggests comprehensive coverage across both fast-feedback (J _(assumption)_

</details>

