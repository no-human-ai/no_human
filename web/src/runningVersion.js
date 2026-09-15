// Which version this board is running, and how a surface phrases it.
//
// One module, because #332 is a divergence risk as much as a discoverability
// one: Settings > Updates was the ONLY surface that printed a version, and the
// moment About printed one too there were two independent answers that could
// disagree after an upgrade. Both now read this.
//
// Two facts feed it, in this order:
//
//   * window.nhDesktop.version — the packaged shell's own app.getVersion(),
//     handed to the sandboxed preload over --nh-app-version. Inside the shell
//     that IS the build the user installed, so it wins: the server it launched
//     could in principle be a different install on PATH.
//   * GET /api/version — no_human.__version__, served by the process that IS
//     the installed package. It is the only source in a plain browser, where
//     there is no preload bridge at all.
//
// Both are already pinned to pyproject.toml's `version` by tests that exist:
// tests/test_api_version.py for the server literal, and
// desktop/packagedFiles.test.mjs for desktop/package.json. So a surface that
// reads this module cannot print a version the packaged build did not ship.
//
// It NEVER invents one. When neither source answered, `version` is null and the
// caller says so plainly rather than printing "no_human unknown", which reads
// as a bug rather than as an honest gap — the same judgement updateNotice.js
// already made about that word.

import React from "react";

/**
 * A version string only when it is really one.
 *
 * Not just a truthiness check: Settings used `desktop?.version ?? server`, and
 * `??` lets an empty string win over a source that actually answered — the
 * preload defaults `appVersion` to a literal when it has nothing better, and a
 * blank `--nh-app-version=` argument reaches it as `""`.
 */
function usable(value) {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

/**
 * @param {object} s
 * @param {string} [s.desktopVersion] window.nhDesktop.version, inside the shell
 * @param {string} [s.serverVersion]  the `version` field of GET /api/version
 * @returns {{version: string|null, source: "shell"|"server"|null}}
 */
export function runningVersion({ desktopVersion = null, serverVersion = null } = {}) {
  const shell = usable(desktopVersion);
  if (shell) return { version: shell, source: "shell" };
  const server = usable(serverVersion);
  if (server) return { version: server, source: "server" };
  return { version: null, source: null };
}

/**
 * What the About surface prints, as data so it can be asserted without a DOM.
 *
 * The detail line exists because the reason to put a version on About at all is
 * the bug report: a reporter should be able to read it and paste it without
 * being told where to look for it.
 *
 * @param {object} s
 * @param {string} [s.version] the resolved version, or null
 * @returns {{known: boolean, line: string, detail: string}}
 */
export function aboutVersionView({ version = null } = {}) {
  const real = usable(version);
  if (!real) {
    return {
      known: false,
      line: "Version unavailable",
      detail: "The board could not reach the server to read it.",
    };
  }
  return {
    known: true,
    line: `no_human ${real}`,
    detail: "Include this when you report a bug.",
  };
}

// The About page's version block, factored out of About.jsx so its rendered
// output is testable directly (`renderToStaticMarkup`) instead of only via a
// source-text guard. Written with React.createElement rather than JSX, for the
// same reason drainChip.js's PausedIndicator is: this stays a plain .js module
// that a `node --test` file can import with no build-time transform, and
// About.jsx renders it exactly as any other component.
//
// It does NOT resolve the version itself — it phrases what useRunningVersion
// already resolved, so About and Settings > Updates cannot print different
// numbers (#332).
export function AboutVersion({ version = null } = {}) {
  const view = aboutVersionView({ version });
  return React.createElement(
    "section",
    { className: "nh-about-block" },
    React.createElement("h2", null, "Version"),
    React.createElement(
      "p",
      null,
      React.createElement("code", { "data-testid": "about-version" }, view.line)
    ),
    React.createElement("p", null, view.detail)
  );
}
