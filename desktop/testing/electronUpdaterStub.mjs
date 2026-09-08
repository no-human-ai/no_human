// Inert stand-in for "electron-updater", loaded only so main.mjs's dynamic
// `import("electron-updater")` succeeds under updaterLoader.mjs. Nothing here
// is ever called: updaterStub.mjs's createUpdater() ignores the `autoUpdater`
// it is handed and returns a fully fake updater instead.
export const autoUpdater = { on() {}, checkForUpdates() {}, downloadUpdate() {},
  quitAndInstall() {} };
export default { autoUpdater };
