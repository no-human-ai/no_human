// The "Repo docs & wiki" step left the onboarding wizard on the operator's
// 2026-09-04 ruling: "why is this a step in the onboarding??? we just need
// no_human to do it asyncly and not ask anything from the user." Wiki
// generation is now enqueued automatically when Launch completes, through the
// existing generateDocs/getDocsJob machinery (api.js, wikiJobs.js) — nothing
// in this module talks to the network directly, so it stays a pure,
// testable helper. A docs-job failure is advisory only and must never block
// or fail onboarding completion.

export function kickoffWikiGeneration({ repos, generate, log } = {}) {
  const list = [...(repos || [])].filter(Boolean);
  const note = log || ((...a) => console.info(...a));
  return Promise.all(list.map((rp) =>
    Promise.resolve()
      .then(() => generate(rp))
      .then((res) => { note("wiki: queued", rp, res?.job_id ?? ""); return { repo: rp, ok: true }; })
      .catch((e) => { note("wiki: enqueue failed (ignored)", rp, e?.message || e); return { repo: rp, ok: false }; })
  ));
}
