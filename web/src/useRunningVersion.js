import { useEffect, useState } from "react";
import { fetchVersion } from "./api.js";
import { runningVersion } from "./runningVersion.js";

// The one hook every surface that shows a version calls. The precedence lives
// in runningVersion.js (pure, tested); this is only the plumbing that gets the
// two facts into it — a preload read that needs no network, and a fetch that
// only happens where the preload is absent.
//
// It is a hook rather than a prop threaded from App on purpose: a version
// fetched at the root would be requested on every board load for two surfaces
// that are usually never opened, and it would give App a piece of state whose
// only reader is a page it does not otherwise know about.
//
// Returns `channel` too: GET /api/version also carries `dist_name`/`published`,
// which Settings > Updates needs to decide whether it may print a pip command.
// That payload arrives on the same request, so fetching it twice would be two
// requests for one answer — and two answers that could disagree.
export function useRunningVersion() {
  const desktop = typeof window !== "undefined" ? window.nhDesktop : undefined;
  const inShell = Boolean(desktop?.shell);
  const [channel, setChannel] = useState(null);

  useEffect(() => {
    // Inside the shell the bridge has already answered synchronously, so there
    // is nothing to ask the server for.
    if (inShell) return undefined;
    let live = true;
    // Best-effort, exactly like the composer's greeting: a failed lookup leaves
    // the version unknown, which is what it always was. Never fabricated.
    fetchVersion().then((v) => { if (live) setChannel(v); }).catch(() => {});
    return () => { live = false; };
  }, [inShell]);

  const { version, source } = runningVersion({
    desktopVersion: desktop?.version,
    serverVersion: channel?.version,
  });
  return { version, source, inShell, channel };
}
