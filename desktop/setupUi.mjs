// Decisions for the credential screen, split out so they are unit-testable.
//
// These encode a regression that shipped once: the screen was written for first
// run, where quitting is the only sensible secondary action. Once File >
// Re-enter Claude Token made it reachable OVER a live board, that same action
// quit the app and stopped a shell-spawned server on one keystroke.

/** Whether a board is reachable behind the screen, from the loader's query. */
export function parseCanReturn(search) {
  return new URLSearchParams(search || "").get("canReturn") === "1";
}

/**
 * What the secondary control (and Escape) must do. NEVER "quit" when a board is
 * behind the screen — that is the regression above.
 */
export function dismissTarget(canReturn) {
  return canReturn ? "dismiss" : "quit";
}

/** Copy that has to change with context, so the buttons never lie. `mode` is
 *  the credential type the operator has selected on the screen:
 *  "subscription" (default) or "api_key" (the sanctioned BYO-key opt-in). */
export function labels(canReturn, mode = "subscription") {
  if (canReturn) {
    return {
      primary: "Save and restart",
      secondary: "Back to board",
      hint: "Saving restarts the no_human server so it picks up the new " +
            "credential — a task running right now will be interrupted.",
    };
  }
  return {
    primary: "Save and start",
    secondary: "Quit",
    hint: mode === "api_key"
      ? "The key is stored only on this computer, in ~/.no_human/.env, and " +
        "bills your Anthropic account directly."
      : "Use the token from `claude setup-token`. Have your own Anthropic " +
        "API key instead? Switch the credential type above.",
  };
}

// --- Existing Claude Code sign-in: honest copy for a detected row -------- //
//
// `claude setup-token` mints a subscription token via browser OAuth with a
// loopback callback — it is NOT a way to hand no_human the CLI's existing
// session non-interactively. MEASURED (macOS, `claude` 2.1.263, `auth
// status` already loggedIn:true): `setup-token --help` exposes no
// non-interactive flag, and spawned the way this app spawns children (piped,
// non-TTY stdio) the command writes nothing to stdout/stderr even once its
// loopback listener is up — a bounded child only ever times out with both
// streams empty. A Windows 11 field report (no_human build bd70645a): 3 of 3
// clicks opened a `localhost:<port>/callback` tab and a 10-second probe
// found nothing listening on that port (ERR_CONNECTION_REFUSED). So the
// detected row never spawns anything: the click only reveals the
// manual-paste instructions, which is the one path that actually works on
// every platform.

/**
 * Copy for the "Use my existing Claude Code sign-in" row. Platform-neutral
 * (no OS-specific terminal app or path shape) and describes rather than
 * disparages the manual-paste path it points at — same claims discipline as
 * labels() above (D4). Names `claude setup-token` explicitly so the
 * instruction is copy-pasteable, and never claims no_human does anything
 * automatically: it only reveals the same paste field every other path uses.
 */
export function existingSignInCopy() {
  return {
    button: "How to get a token",
    note: "Detected a Claude Code sign-in. no_human cannot reuse it "
        + "directly — a subscription token has to be minted with "
        + "`claude setup-token`.",
    guidance: "Run `claude setup-token` in a terminal, finish the sign-in "
            + "it starts, then paste the token it prints below.",
  };
}

/**
 * The OPTIONAL codex OpenAI key to hand the main process, given the selected
 * codex mode and the field's contents. Returns "" — meaning "write nothing for
 * OpenAI" — for EVERY path except an explicit api_key choice with a non-empty
 * field. So a skipped section (no codex radio chosen) and codex SUBSCRIPTION
 * both send nothing, which is the whole of constraint #6b at the UI layer: in
 * subscription mode no_human stores no OpenAI credential. The Claude credential
 * is unaffected — it is sent separately and stays required.
 */
export function codexKeyToSend(codexMode, fieldValue) {
  return codexMode === "api_key" ? (fieldValue || "").trim() : "";
}

/**
 * What to say while a save is in flight, by elapsed time.
 *
 * The save path can legitimately take ~43s: stopping the old server is capped
 * at 20s, then two 1.5s probes, then up to 20s waiting for the replacement to
 * bind. Previously the screen showed ONE static string for all of it with both
 * buttons disabled — indistinguishable from a hang, and the user's only move
 * was to force-quit. The thresholds are deliberately few: #msg is role="alert",
 * so every change is announced assertively and chatty updates would be worse
 * than none.
 */
export function saveProgress(elapsedMs, canReturn = false) {
  if (elapsedMs < 3000) return "Saving and starting the server…";
  if (!canReturn) {
    // FIRST RUN: there is no old server. Saying "stopping the old server" here
    // is simply false, and this window is easy to reach (a 1.5s probe plus a
    // login-shell lookup plus the spawn wait).
    if (elapsedMs < 25000) return "Starting the server…";
    return "Still working — a first run can take up to a minute.";
  }
  if (elapsedMs < 12000) return "Saved. Stopping the old server…";
  if (elapsedMs < 25000) return "Still working — waiting for the old server to stop…";
  return "Still working — this can take up to a minute.";
}

/**
 * What to say when we saved a token but could not stop the server holding the
 * old one. `weOwnIt` MUST come from the same source that selected the restart
 * branch (ownsAny), not from `state` alone: mid-boot the child is in the owned
 * registry while state is still null, and branching on state told a friend on a
 * packaged DMG to "restart the nh start you ran" when there is no terminal and
 * the culprit is our own child.
 */
export function restartFailedMessage(origin, weOwnIt) {
  return weOwnIt
    ? "Saved, but the server this app started would not stop, so it still holds "
      + "the old token. Quit no_human and open it again."
    : `Saved, but another server is already serving ${origin} and still holds the `
      + "old token. Restart that server to pick up the new one.";
}

// --- Task 5: first-run requirements check (claude/node on PATH) ----------- //
//
// The Agent SDK shells out to `claude` for every task and no_human never
// passes cli_path, so a first run that only asked for a token used to die on
// its first task with a "connected" setup screen behind it. This block turns
// the nh:requirements() IPC result into what the checklist shows and whether
// Save may be pressed — pure, so the rendering and the gate are unit-testable
// without a real Electron window.

// Same install line as the paragraph this checklist replaces (token.html) —
// kept verbatim so a friend who has seen it once recognises it here.
export const CLI_INSTALL_LINE =
  "Install Node.js (nodejs.org), then run npm install -g @anthropic-ai/claude-code";

/**
 * One checklist row's text for `label` ("claude" or "node"), from that key's
 * slice of the nh:requirements() result. `info` may be missing entirely (the
 * check hasn't run yet, or the IPC round-trip failed) — treated as "not
 * found" rather than thrown, so a broken IPC call still renders a row instead
 * of leaving the screen blank.
 */
export function requirementLine(label, info) {
  const i = info || {};
  if (i.ok) {
    const version = i.version ? ` ${i.version}` : "";
    return `✓ ${label}${version} at ${i.path}`;
  }
  return `✗ ${label} not found — ${CLI_INSTALL_LINE}`;
}

/**
 * Whether Save must stay disabled. Gated on claude.ok alone — a missing
 * `claude` is the failure the Agent SDK cannot work around, while a missing
 * `node` only matters for the one-time `npm install` this screen suggests.
 * `skipped` is the "I'll install it later" escape hatch: once the operator
 * has chosen to proceed anyway, this must never re-lock them out, even on a
 * Re-check that still finds nothing.
 */
export function saveDisabled(requirements, skipped) {
  if (skipped) return false;
  return !(requirements && requirements.claude && requirements.claude.ok);
}
