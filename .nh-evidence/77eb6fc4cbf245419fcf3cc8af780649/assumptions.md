# Assumptions

_Harness-captured record for task `77eb6fc4`, commit `c76e5cbeac8575c77378efd1c4055e9f8b86d1c6` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** What value should N be, and how should the code handle nights with fewer than N historical records? **A:** N should be 30 nights. For nights with fewer than N historical records, the code should apply the threshold only when at least N nights are available; before that, it should pass the night (no alert) to establish the baseline. _(assumption)_
- **Q:** Should `weighted_tokens` be populated via `core/pricing.py::weighted_tokens` (including output share) or removed from the record entirely? **A:** weighted_tokens should be populated via core/pricing.py::weighted_tokens including the output share, so the record carries the complete cost signal and no code path sees a null. _(assumption)_
- **Q:** What is the complete path and filename where the reference median cost file should be stored? **A:** The reference median cost file should be stored as cost_median.json alongside baseline.json in the same directory. _(assumption)_
- **Q:** Do you have write access to the no_human repository on GitHub? **A:** HUMAN-GATED: not self-answerable

</details>

