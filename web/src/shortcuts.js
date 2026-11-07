// Keyboard shortcuts for the board — the pure matching logic behind the
// cheat-sheet dialog (ShortcutsDialog.jsx) and the desktop key handling in
// App.jsx.
//
// ⌘N and ⌘1-4 are already owned end-to-end by the Electron application menu
// (desktop/menu.mjs: CmdOrCtrl+N/1/2/3/4 → New Task, In progress, Backlog,
// Stats, Settings). The menu's accelerator fires the click handler directly
// and posts "nh:menu" to the renderer (App.jsx's onMenu effect) — this module
// does NOT also match those keys, or a single keystroke would double-fire.
// What's left for the web layer itself to own is: the platform-standard ⌘,
// for Settings (⌘4 already does the same job under the menu's own numbering,
// but ⌘, is the shortcut users actually reach for out of habit), ⌘/ or ? to
// open this cheat-sheet. Esc is NOT owned here — this module never acts on
// it; every dialog that can be open (composer, drawer, Settings, the
// cheat-sheet) closes itself on Escape via its own listener (useEscapeKey.js).
// The cheat-sheet still LISTS Esc below ("Close dialog or drawer") because
// that's true of every one of those dialogs individually. Browser (i.e.
// non-Electron) keeps its existing shortcut, bare "n", handled separately
// by keyboardShortcut.js/shouldTriggerNewTask, plus bare "?" to open this
// cheat-sheet.
const EDITABLE = new Set(["INPUT", "TEXTAREA", "SELECT"]);

// The full list, for the cheat-sheet dialog. Includes shortcuts this module
// does not itself act on (⌘N/⌘1-4 are the menu's) so the dialog can still
// document every key that works.
export const SHORTCUTS = [
  { id: "new-task", keys: { mac: "⌘N", other: "Ctrl+N" }, label: "New task", when: "desktop" },
  { id: "page-board", keys: { mac: "⌘1", other: "Ctrl+1" }, label: "In progress", when: "desktop" },
  { id: "page-backlog", keys: { mac: "⌘2", other: "Ctrl+2" }, label: "Backlog", when: "desktop" },
  { id: "page-stats", keys: { mac: "⌘3", other: "Ctrl+3" }, label: "Stats", when: "desktop" },
  { id: "settings", keys: { mac: "⌘4 or ⌘,", other: "Ctrl+4 or Ctrl+," }, label: "Settings", when: "desktop" },
  { id: "help", keys: { mac: "⌘/ or ?", other: "Ctrl+/ or ?" }, label: "Keyboard shortcuts", when: "desktop" },
  { id: "help-browser", keys: { mac: "?", other: "?" }, label: "Keyboard shortcuts", when: "browser" },
  { id: "new-task-browser", keys: { mac: "n", other: "n" }, label: "New task", when: "browser" },
  { id: "close", keys: { mac: "Esc", other: "Esc" }, label: "Close dialog or drawer", when: "always" },
];

// The subset this module actually decides. Deliberately excludes "n" and the
// digits — those belong to the menu alone (see comment above).
const MOD = { ",": "settings", "/": "help" };

export function matchShortcut(e, { isDesktop } = {}) {
  if (!e) return null;
  const t = e.target;
  if (t && (EDITABLE.has(t.tagName) || t.isContentEditable)) return null;
  if (e.key === "?" && !e.metaKey && !e.ctrlKey && !e.altKey) return "help";
  if (!isDesktop) return null;
  if ((e.metaKey || e.ctrlKey) && !e.altKey) return MOD[e.key] ?? null;
  return null;
}
