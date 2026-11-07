import test from "node:test";
import assert from "node:assert/strict";
import { matchShortcut, SHORTCUTS } from "./shortcuts.js";

const ev = (key, mods = {}) => ({
  key, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false,
  target: { tagName: "DIV" }, ...mods,
});

// ⌘N and ⌘1-4 are the Electron application menu's own accelerators
// (desktop/menu.mjs: CmdOrCtrl+N/1/2/3/4 — New Task, In progress, Backlog,
// Stats, Settings). The menu handles those keystrokes end-to-end and posts
// "nh:menu"; this module must NOT also claim them, or one keypress would fire
// twice. So this is the real ownership split, not the brief's literal draft.
test("keys already owned by the Electron menu are not matched here", () => {
  for (const key of ["n", "1", "2", "3", "4"]) {
    assert.equal(matchShortcut(ev(key, { metaKey: true }), { isDesktop: true }), null,
      `${key} should stay the menu's alone`);
    assert.equal(matchShortcut(ev(key, { metaKey: true }), { isDesktop: false }), null);
  }
});

test("cmd+, opens settings and cmd+/ opens the cheat-sheet, desktop-only", () => {
  assert.equal(matchShortcut(ev(",", { metaKey: true }), { isDesktop: true }), "settings");
  assert.equal(matchShortcut(ev("/", { metaKey: true }), { isDesktop: true }), "help");
  assert.equal(matchShortcut(ev(",", { metaKey: true }), { isDesktop: false }), null);
  assert.equal(matchShortcut(ev("/", { metaKey: true }), { isDesktop: false }), null);
});

test("ctrl+,  and ctrl+/ work the same as cmd on non-mac desktop", () => {
  assert.equal(matchShortcut(ev(",", { ctrlKey: true }), { isDesktop: true }), "settings");
  assert.equal(matchShortcut(ev("/", { ctrlKey: true }), { isDesktop: true }), "help");
});

test("escape is never matched here — each open dialog owns its own Escape handler", () => {
  assert.equal(matchShortcut(ev("Escape"), { isDesktop: false }), null);
  assert.equal(matchShortcut(ev("Escape"), { isDesktop: true }), null);
});

test("typing in an input never matches a modifier shortcut", () => {
  assert.equal(
    matchShortcut({ ...ev(",", { metaKey: true }), target: { tagName: "INPUT" } }, { isDesktop: true }),
    null);
  assert.equal(
    matchShortcut({ ...ev("/", { metaKey: true }), target: { tagName: "TEXTAREA" } }, { isDesktop: true }),
    null);
});

test("escape from inside an editable field is still not matched here — the dialog's own useEscapeKey.js handles it directly, not through this module", () => {
  assert.equal(
    matchShortcut({ ...ev("Escape"), target: { tagName: "INPUT" } }, { isDesktop: true }),
    null);
});

test("alt+key never matches, even for a bound letter", () => {
  assert.equal(matchShortcut(ev(",", { metaKey: true, altKey: true }), { isDesktop: true }), null);
});

test("SHORTCUTS lists real accelerators only — no page-done/page-failed placeholders", () => {
  const ids = SHORTCUTS.map((s) => s.id);
  assert.ok(ids.includes("page-stats"), "cmd+3 really opens Stats, not a Done page");
  assert.ok(!ids.includes("page-done") && !ids.includes("page-failed"),
    "the menu has no accelerator for Done/Failed — nothing should claim it does");
  for (const s of SHORTCUTS) {
    assert.ok(["desktop", "browser", "always"].includes(s.when), s.id);
    assert.ok(s.keys && s.keys.mac && s.keys.other, s.id);
    assert.ok(s.label, s.id);
  }
});

test("? opens the cheat-sheet in both desktop and browser", () => {
  assert.equal(matchShortcut(ev("?"), { isDesktop: true }), "help");
  assert.equal(matchShortcut(ev("?"), { isDesktop: false }), "help");
});

test("? with ctrl or meta or alt does not match", () => {
  assert.equal(matchShortcut(ev("?", { metaKey: true }), { isDesktop: true }), null);
  assert.equal(matchShortcut(ev("?", { ctrlKey: true }), { isDesktop: false }), null);
  assert.equal(matchShortcut(ev("?", { altKey: true }), { isDesktop: true }), null);
});

test("typing ? in an editable field never opens the cheat-sheet", () => {
  assert.equal(
    matchShortcut({ ...ev("?"), target: { tagName: "INPUT" } }, { isDesktop: false }),
    null);
  assert.equal(
    matchShortcut({ ...ev("?"), target: { tagName: "TEXTAREA" } }, { isDesktop: true }),
    null);
  assert.equal(
    matchShortcut({ ...ev("?"), target: { tagName: "DIV", isContentEditable: true } }, { isDesktop: false }),
    null);
});

