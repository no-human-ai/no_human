// Resolve hook for the defer-push test (mainUpdateDefer.test.mjs).
//
// electronLoader.mjs alone is not enough there: main.mjs's getUpdater() only
// ever produces a real updater when `import("electron-updater")` succeeds.
// electron-updater IS a declared, installed dependency (desktop/package.json)
// — the reason getUpdater() still resolves null under electronLoader.mjs
// alone is that electron-updater is CommonJS, and its own `require("electron")`
// is a synchronous CJS require that bypasses this process's ESM `register()`
// resolve hook entirely (that hook only intercepts `import()`/ESM
// specifiers). It ends up reading off whatever the real "electron" package
// exports outside an actual Electron process, throws (e.g. "Cannot read
// properties of undefined (reading 'getVersion')"), and getUpdater()'s
// try/catch turns that into a plain null. That leaves nh:update-defer
// permanently on its `!u` early-return path, which cannot exercise the
// push-after-defer() ordering the test needs. This loader swaps in a FAKE
// updater instead (via the "./updater.mjs" and "electron-updater" redirects
// below, both resolved through the dynamic `import()` that this hook DOES
// see), so defer() itself is scriptable.
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
