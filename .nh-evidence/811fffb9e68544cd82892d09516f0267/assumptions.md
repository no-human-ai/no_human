# Assumptions

_Harness-captured record for task `811fffb9`, commit `e3a6733cd935f979087f57a9f848a5ce084f1561` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 3 assumptions made on your behalf — verify at review</summary>

- **Q:** Where should the cleanup error callback record information about items that could not be removed, so the doctor can later detect incomplete cleanups? For example, a marker file in the sandbox's parent directory, a central log file, or another mechanism? **A:** A marker file in or adjacent to the sandbox directory. The error callback should write a marker (e.g., '.cleanup_failed') when shutil.rmtree encounters items it cannot remove, allowing the doctor to later detect incomplete cleanups by checking for this marker's presence. _(assumption)_
- **Q:** When a sandbox directory contains no files (0 bytes total), should the doctor suppress the advisory entirely to reduce noise, or report it with different messaging that indicates it's an empty orphaned directory rather than a disk-reclamation issue? **A:** Suppress the advisory entirely when the residue contains no files (0 bytes). An empty orphaned directory represents no disk to reclaim and produces noise without actionable information in a health command. _(assumption)_
- **Q:** Should the doctor's age threshold (currently >2h old) remain unchanged, be adjusted to a different duration, or be removed so that all orphaned sandboxes are reported regardless of age? **A:** Keep the age threshold (>2h) unchanged. The threshold appropriately avoids alerting on in-flight eval sandboxes. The root problem is the false attribution and poor measurement logic, not the threshold itself. _(assumption)_

</details>

