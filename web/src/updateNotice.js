// What the Settings > Updates panel says, as a pure function.
//
// The board runs in two places — inside the desktop shell, where
// window.nhDesktop exists and updates are actionable, and in a plain browser,
// where the page is served by a locally installed `nh` and the only meaningful
// upgrade path is pip. Those are genuinely different products to the user, and
// conflating them produces a "Download update" button that cannot work.
//
// This is out of the JSX for the same reason drainChip.js is: the interesting
// part is the decision, and a decision buried in JSX can only be tested by
// regexing the source — which this project has already paid for once.
//
// It NEVER fabricates a version: the `version` field is "unknown" whenever the
// caller could not supply one. The browser COPY no longer prints that word,
// though — it just leaves the version out of the sentence, because "you are
// running no_human unknown" reads as a bug rather than as an honest gap.
// (Settings now sources the version from GET /api/version outside the shell, so
// that gap is rare.)

/** Closed set of tones, mirroring the rest of the board's status vocabulary. */
export const TONES = ["ok", "info", "warn", "error"];

/**
 * @param {object}  s
 * @param {boolean} s.inShell     running inside the desktop shell
 * @param {string}  s.current     the running version, or null/undefined
 * @param {object}  s.update      the last payload from the shell, if any
 * @param {object}  s.channel     the browser-path distribution channel, from
 *                                GET /api/version: {distName, published}
 * @returns {{title,detail,tone,actions:string[],version:string}}
 */
export function updateNotice({ inShell = false, current = null, update = null, channel = null } = {}) {
  const version = current || "unknown";

  if (!inShell) {
    // The browser path had no source for `current` at all, so this sentence
    // always read "You are running no_human unknown in a browser" — a word that
    // tells the operator nothing and looks like a bug. The version now comes
    // from GET /api/version (the server IS the installed package); on the rare
    // path where that lookup fails, say less rather than saying "unknown".
    const running = current ? `no_human ${current}` : "no_human";
    // A pip command is only ever printed when the channel PROVES the package
    // is published there (channel.published === true, from is_published() on
    // the server, which fails closed). Anything short of that - no channel
    // payload, an older server, a malformed response - gets the honest,
    // confident fallback below rather than a command that may 404.
    const detail = channel?.published === true
      ? `You are running ${running} in a browser. Upgrade the command line`
        + ` with: pip install --upgrade ${channel.distName}`
      : `You are running ${running} in a browser. New versions ship on the`
        + " releases page — the board updates when you update nh.";
    return {
      title: "Updates",
      detail,
      tone: "info",
      actions: [],
      version,
    };
  }

  const mode = update?.mode ?? null;

  if (mode === "downloading") {
    const pct = Number.isFinite(update?.percent) ? update.percent : 0;
    return {
      title: `Downloading ${update?.latest ?? "update"}… ${pct}%`,
      detail: "You can keep working. The update installs when you choose to restart.",
      tone: "info",
      actions: [],
      version,
    };
  }

  if (mode === "downloaded") {
    return {
      title: `no_human ${update?.latest ?? ""} is ready to install`.trim(),
      detail: "Restarting takes a few seconds. Running tasks are not interrupted"
        + " — the server keeps going.",
      tone: "ok",
      actions: ["install", "later"],
      version,
    };
  }

  if (mode === "unavailable") {
    // The honest, legible unsigned case. It still TELLS the user an update
    // exists — hiding it would be the silent failure the design forbids.
    return {
      title: `no_human ${update?.latest ?? ""} is available`.trim(),
      detail: update?.message
        || "This build is not code-signed, so it cannot install updates itself."
           + " Download the new version manually.",
      tone: "warn",
      actions: ["download-page"],
      version,
    };
  }

  if (mode === "available") {
    return {
      title: `no_human ${update?.latest ?? ""} is available`.trim(),
      detail: `You have ${version}. Nothing downloads until you choose to.`,
      tone: "info",
      actions: ["download", "later"],
      version,
    };
  }

  if (mode === "failed") {
    return {
      title: "Could not check for updates",
      detail: update?.error || "The update service could not be reached.",
      tone: "error",
      actions: ["check"],
      version,
    };
  }

  if (mode === "up-to-date") {
    return {
      title: `no_human ${version} is up to date`,
      detail: "Checked just now.",
      tone: "ok",
      actions: ["check"],
      version,
    };
  }

  // Nothing has been reported yet this session.
  return {
    title: `no_human ${version}`,
    detail: "Updates are checked once a day. You are told when one is"
      + " available and choose when to install it.",
    tone: "info",
    actions: ["check"],
    version,
  };
}

/**
 * The board-shell notice for an update the AUTOMATIC startup check found —
 * the fix for the defect this module exists to close: that check's one
 * "nh:update" push fires before Settings' UpdatesPanel (its only subscriber)
 * ever mounts, so a result nobody was listening for used to vanish. The main
 * process now retains it (main.mjs's `lastUpdate`) and the shell seeds from
 * that on load; this is the pure decision of whether to show it there.
 *
 * Deliberately covers "available" AND "unavailable" — the reported incident
 * was the unsigned case ("this build is not code-signed…"), whose mode is
 * "unavailable". Both mean "a newer version exists"; every other mode
 * (up-to-date, failed, downloading, downloaded) stays board-silent, same as
 * today — an automatic-check failure must never surface outside Settings.
 *
 * @param {object} s
 * @param {object} s.update           the last payload (from onUpdate or getLastUpdate), or null
 * @param {string} s.dismissedVersion the version the user last clicked "Later" on, this session
 * @returns {null|{text:string,version:string,className:string,role:string,tone:string}}
 */
export function updateBanner({ update = null, dismissedVersion = null } = {}) {
  if (!update) return null;
  if (update.mode !== "available" && update.mode !== "unavailable") return null;
  const version = update.latest ?? null;
  if (!version) return null;
  if (dismissedVersion && dismissedVersion === version) return null;

  const tone = update.mode === "unavailable" ? "warn" : "info";
  // main already computed the exact sentence via updateMessage; fall back to
  // the same copy updateNotice() would show, so the two surfaces never drift.
  const text = update.message
    || updateNotice({ inShell: true, current: update.current, update }).title;

  return {
    text,
    version,
    className: "nh-stale-banner nh-update-banner",
    role: "status",
    tone,
  };
}
