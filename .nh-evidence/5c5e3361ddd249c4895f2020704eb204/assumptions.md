# Assumptions

_Harness-captured record for task `5c5e3361`, commit `3e6174da53eb4486c508c951e5785c45fc557685` — not model-authored: no_human wrote this file from the intake step's recorded questions and assumptions. It records what the gate produced; it is not a verdict of the model that wrote the code._

<details><summary>⚠️ 4 assumptions made on your behalf — verify at review</summary>

- **Q:** The task mentions adding documentation to 'docs/INSTALLER.md (or wherever desktop dev setup is described)'. Should documentation be added as a new docs/INSTALLER.md file, or to an existing file such as docs/DEVELOPMENT.md or similar? **A:** Create a new docs/INSTALLER.md file. The task lists this as the primary option and presents it as a distinct topic—Electron binary installation during npm ci is installer-specific setup, warranting its own documentation file rather than diluting general DEVELOPMENT.md. _(assumption)_
- **Q:** The task describes two equivalent postinstall approaches: `node node_modules/electron/install.js` or `node -e "require('electron')"`. Which should be implemented in desktop/package.json? **A:** Implement `node node_modules/electron/install.js` in the postinstall script. The task lists this first as the primary approach; it is more explicit and maintainable than relying on electron's lazy-loading mechanism, and directly invokes the installer without depending on require() side effects. _(assumption)_
- **Q:** The task mentions 'desktop/packagedFiles.test.mjs (or a new small test)' for asserting the postinstall. Should the assertion be added to packagedFiles.test.mjs, placed in a different existing test file, or created as a new test file? **A:** Add the assertion to desktop/packagedFiles.test.mjs. The task cites it first as the primary target, and a postinstall declaration is inherently a package-file concern—validating the package.json manifest structure is packagedFiles' domain. _(assumption)_
- **Q:** The acceptance criteria includes 'scripts/check_release_manifest.py --strict OK'. Does this existing script already handle the new postinstall, or does it require modifications to pass --strict after the changes? **A:** The check_release_manifest.py script should pass --strict without modification. Adding a postinstall field to package.json is a standard, valid manifest change; the script is presented as a verification step, not a known blocker requiring updates. _(assumption)_

</details>

