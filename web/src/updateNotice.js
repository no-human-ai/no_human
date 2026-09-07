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
      details: null,
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
      details: null,
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
      details: null,
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
      details: null,
      tone: "warn",
      actions: ["download-page"],
      version,
    };
  }

  if (mode === "available") {
    return {
      title: `no_human ${update?.latest ?? ""} is available`.trim(),
      detail: `You have ${version}. Nothing downloads until you choose to.`,
      details: null,
      tone: "info",
      actions: ["download", "later"],
      version,
    };
  }

  if (mode === "failed") {
    // `update.error` is already the short, classified sentence (see
    // desktop/updatePolicy.mjs) — the raw electron-updater dump (headers,
    // status, stack) travels separately in `rawError` and is only ever
    // rendered behind a collapsed "Details" element, never on this line.
    return {
      title: "Could not check for updates",
      detail: update?.error || "The update service could not be reached.",
      details: update?.rawError ? String(update.rawError) : null,
      tone: "error",
      actions: ["check"],
      version,
    };
  }

  if (mode === "up-to-date") {
    return {
      title: `no_human ${version} is up to date`,
      detail: "Checked just now.",
      details: null,
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
    details: null,
    tone: "info",
    actions: ["check"],
    version,
  };
}
