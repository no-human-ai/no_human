// Fake `createUpdater()` for mainUpdateDefer.test.mjs — a scriptable stand-in
// for desktop/updater.mjs, loaded in place of it by updaterLoader.mjs.
//
// `defer()` records the push queue's length AT THE MOMENT IT RUNS: that is
// what makes "the sendUpdateEvent() push moved before u.defer(version)" an
// observable mutation rather than a hopeful comment — if the push fired first,
// `sentAt` would already be one higher than the count the test captured before
// calling the IPC handler.
import { calls } from "./electronStub.mjs";

export const state = {
  deferResult: { mode: "failed", error: "not configured" },
  deferCalls: [],
};

export function resetUpdaterStub() {
  state.deferCalls.length = 0;
  state.deferResult = { mode: "failed", error: "not configured" };
}

export function createUpdater() {
  return {
    configure() {},
    check() { return Promise.resolve(null); },
    download() { return Promise.resolve(null); },
    install() { return Promise.resolve(null); },
    defer(version) {
      state.deferCalls.push({ version, sentAt: calls.sent.length });
      return state.deferResult;
    },
    downloaded: false,
    pending: null,
  };
}
