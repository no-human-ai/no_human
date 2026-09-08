// The Settings "!" nudge: onboarding no longer walks the AI-learnings steps
// (they left the wizard 2026-08-30), so a one-time nudge points the user at
// Settings to finish their AI configuration — review rules, pick models, seed
// their memories. A "!" badge sits on the Settings nav row with the tooltip
// "Complete AI configuration", and a small popup prompts once after onboarding.
//
// Fix round (review, 2026-09-01): the badge and the popup are two SEPARATE
// acknowledgements now, each its own localStorage bit — collapsing them into
// one (the D2.1 first cut) made the popup nag forever, because the badge's
// bit only clears once the Second-brain pane is actually seen, and a user who
// opens Settings on some OTHER pane (the row body, or a Finish-setup deep
// link) never visits it:
//   - the BADGE (`nh-ai-config-done` / isAiConfigDone/markAiConfigDone)
//     clears only once the Second-brain pane in Settings has actually
//     rendered — not merely because Settings opened on some other pane, and
//     not by clicking the "!" badge itself (it only navigates there).
//   - the POPUP (`nh-ai-config-popup-dismissed` / isPopupDismissed/
//     markPopupDismissed) clears the moment Settings opens on ANY pane, or
//     the popup's own × is clicked — it is a one-time prompt pointing at
//     Settings, not a repeat of the badge's own, stricter condition.
//
// Both are per-install, per-browser acknowledgements, so localStorage is the
// right home (like the theme toggle next to it) — not server state. Every
// access is guarded: localStorage throws in private windows / blocked-storage
// browsers, and a throw there must degrade to "show the nudge" (badge shown /
// popup shown), never crash the board.

const KEY = "nh-ai-config-done";
const POPUP_KEY = "nh-ai-config-popup-dismissed";

/** True once the Second-brain pane has actually rendered once. Fail-open to
 *  NOT done (show the badge) if storage is unreadable — a returning user
 *  seeing the badge once more is harmless; a crashed board is not. */
export function isAiConfigDone(storage = safeStorage()) {
  try {
    return storage?.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

/** Record that the Second-brain pane has been seen. A write failure is
 *  swallowed: the badge simply shows again next load, which is strictly
 *  better than throwing into a render/click path. */
export function markAiConfigDone(storage = safeStorage()) {
  try {
    storage?.setItem(KEY, "1");
  } catch {
    /* storage blocked — the badge will reappear, which is acceptable */
  }
}

/** True once the one-time popup has been dismissed — by opening Settings on
 *  ANY pane, or by its own ×. Fail-open to NOT dismissed (show the popup) if
 *  storage is unreadable, same reasoning as isAiConfigDone above. */
export function isPopupDismissed(storage = safeStorage()) {
  try {
    return storage?.getItem(POPUP_KEY) === "1";
  } catch {
    return false;
  }
}

/** Record the popup's dismissal. A write failure is swallowed: the popup
 *  simply shows again next load, which is strictly better than throwing into
 *  a click handler. */
export function markPopupDismissed(storage = safeStorage()) {
  try {
    storage?.setItem(POPUP_KEY, "1");
  } catch {
    /* storage blocked — the popup will reappear, which is acceptable */
  }
}

function safeStorage() {
  try {
    return typeof localStorage !== "undefined" ? localStorage : null;
  } catch {
    return null; // accessing the property itself can throw (sandboxed frames)
  }
}
