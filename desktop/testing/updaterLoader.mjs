// Resolve hook for the defer-push test (mainUpdateDefer.test.mjs).
//
// electronLoader.mjs alone is not enough there: main.mjs's getUpdater() only
// ever produces a real updater when `import("electron-updater")` succeeds, and
// under that loader it never does (electron-updater is not installed in
// desktop/node_modules, and even if it were, it reaches for app-update.yml and
// the network at import time). That leaves nh:update-defer permanently on its
// `!u` early-return path, which cannot exercise the push-after-defer()
// ordering the test needs. This loader swaps in a FAKE updater instead, so
// defer() itself is scriptable.
export function resolve(specifier, context, next) {
  if (specifier === "electron") {
    return { url: new URL("./electronStub.mjs", import.meta.url).href,
             shortCircuit: true };
  }
  // Only main.mjs imports "./updater.mjs" as a bare relative specifier.
  if (specifier === "./updater.mjs") {
    return { url: new URL("./updaterStub.mjs", import.meta.url).href,
             shortCircuit: true };
  }
  if (specifier === "electron-updater") {
    return { url: new URL("./electronUpdaterStub.mjs", import.meta.url).href,
             shortCircuit: true };
  }
  return next(specifier, context);
}
