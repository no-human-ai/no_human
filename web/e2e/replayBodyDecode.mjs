// Pure decode layer extracted out of replay-body-leak.mjs so it is
// unit-testable without a browser (see web/src/replayBodyDecode.test.mjs)
// and so degradation of the decode path is OBSERVABLE via a stats object
// instead of silently swallowed.
//
// posthog-js's recorder chunk (lazy-recorder.js) gzip-compresses individual
// rrweb snapshot fields BEFORE they ever reach a "$snapshot" event's
// `properties.$snapshot_data` array — independent of, and NOT disabled by,
// the mock server's `supportedCompression: []` config (which only controls
// the OUTER whole-POST-body transport encoding, e.g. a content-encoding:
// gzip header on the fetch/XHR itself). For a `FullSnapshot` event the whole
// `data` field is replaced by compressed bytes; for a `Mutation`/
// `StyleSheetRule` `IncrementalSnapshot`, the `texts`/`attributes`/
// `removes`/`adds` sub-fields are each compressed individually — both cases
// marked with `cv: "2024-10"` on the event. The compressed bytes are
// encoded as a JS "binary string" (one UTF-16 code unit per raw byte, via
// `String.fromCharCode`) — NOT base64 — so `Buffer.from(s, "binary")`
// (Node's alias for latin1) reverses it byte-for-byte before
// `zlib.gunzipSync`.
//
// Without reversing this, a substring search over the outer captured JSON
// text is blind to whatever DOM content (element text nodes, attributes —
// e.g. a rendered `title="..."` attribute) got swept into a FullSnapshot or
// a Mutation record. That DOM/rrweb capture channel is entirely separate
// from the network-capture channel `maskCapturedNetworkRequestFn`
// (replayScrub.js) redacts — so a substring check that never decompresses
// this can't tell "not on the wire at all" apart from "on the wire, just
// gzip'd where a naive scan can't see it".
import zlib from "node:zlib";

// A fresh, caller-owned accumulator — one per harness pass (see runPass in
// replay-body-leak.mjs), threaded through every decode() call for that pass
// so decompressionHealth() below can tell "the decode path degraded" apart
// from "nothing to decode this time".
export function createDecodeStats() {
  return {
    cvMarkedEvents: 0, // rrweb events whose cv === "2024-10" (the compression marker)
    compressedFieldsSeen: 0, // non-empty string field candidates on those events
    inflateOk: 0, // of those, how many gunzip'd successfully
    inflateFailed: 0, // of those, how many threw (posthog-js's encoding drifted)
    snapshotEvents: 0, // top-level PostHog events that carried $snapshot_data
  };
}

function inflateBinaryGzipString(s, stats) {
  if (typeof s !== "string" || s.length === 0) return undefined;
  if (stats) stats.compressedFieldsSeen++;
  try {
    const out = zlib.gunzipSync(Buffer.from(s, "binary")).toString("utf8");
    if (stats) stats.inflateOk++;
    return out;
  } catch {
    // Kept silent for the caller (a field that isn't actually compressed
    // gzip data is not an error) but COUNTED, so a posthog-js encoding
    // change surfaces as inflateFailed > 0 rather than as nothing.
    if (stats) stats.inflateFailed++;
    return undefined;
  }
}

// Decompresses one rrweb event's `cv: "2024-10"`-marked field(s) (see above)
// into plain text, so its DOM content can be substring-checked like
// anything else. Returns "" for events that aren't compressed (most —
// e.g. the "rrweb/network@1" Plugin-type events that actually carry HTTP
// request/response bodies are never in the compression-eligible set: only
// FullSnapshot and Mutation/StyleSheetRule IncrementalSnapshot events are).
export function decompressRrwebEvent(ev, stats) {
  if (!ev || ev.cv !== "2024-10" || ev.data == null) return "";
  if (stats) stats.cvMarkedEvents++;
  const pieces = [];
  if (typeof ev.data === "string") {
    const out = inflateBinaryGzipString(ev.data, stats);
    if (out !== undefined) pieces.push(out);
  } else if (typeof ev.data === "object") {
    for (const key of ["texts", "attributes", "removes", "adds"]) {
      const out = inflateBinaryGzipString(ev.data[key], stats);
      if (out !== undefined) pieces.push(out);
    }
  }
  return pieces.join("\n");
}

// Returns:
//   - text: the decoded raw outer TEXT (byte-level substring inspection
//     scope for the NETWORK-capture channel specifically — this is what
//     maskCapturedNetworkRequestFn actually redacts, and it is never
//     gzip'd per-field the way DOM snapshot data is, so this text already
//     contains any network-capture body content in the clear).
//   - expandedText: `text` plus every inner rrweb DOM snapshot field this
//     request's $snapshot event(s) carried, decompressed — the broader
//     scope for "absent from every captured byte" claims that must also
//     account for the DOM/rrweb channel, not just the network sub-channel.
//   - events: best-effort parsed top-level PostHog events (for the vacuity
//     guard: proving $snapshot events were actually captured).
//
// `stats` (see createDecodeStats above) is optional and caller-owned so
// per-field decode health can be observed across a whole pass, not just a
// single request.
export function decode(raw, req, stats) {
  let buf = raw;
  try {
    const enc = req.headers["content-encoding"] || "";
    if (enc.includes("gzip")) buf = zlib.gunzipSync(buf);
  } catch {
    /* fall through to raw */
  }
  let text = buf.toString("utf8");
  if (text.startsWith("data=")) {
    try {
      const b64 = decodeURIComponent(text.slice(5));
      text = Buffer.from(b64, "base64").toString("utf8");
    } catch {
      /* keep text */
    }
  }
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { text, expandedText: text, events: [] };
  }
  const events = Array.isArray(parsed) ? parsed : parsed.batch ? parsed.batch : [parsed];
  const inner = [];
  for (const ev of events) {
    const rrwebEvents =
      ev && ev.properties && Array.isArray(ev.properties.$snapshot_data) ? ev.properties.$snapshot_data : [];
    if (stats && rrwebEvents.length) stats.snapshotEvents++;
    for (const re of rrwebEvents) {
      const d = decompressRrwebEvent(re, stats);
      if (d) inner.push(d);
    }
  }
  const expandedText = inner.length ? text + "\n\x00\n" + inner.join("\n\x00\n") : text;
  return { text, expandedText, events };
}

// Turns the accumulated stats for a whole pass (plus that pass's actual
// network-only vs. expanded haystack byte sizes) into a single ok/reason
// verdict. This is what promotes "did decompression actually run" from an
// INFO line nobody gates on into a real check() — see replay-body-leak.mjs.
//
// Each branch corresponds to a concrete way the decode path can silently
// degrade:
//   - cvMarkedEvents === 0: the "cv" compression-version marker this file
//     matches on (posthog-js's lazy-recorder.js) drifted, or no rrweb
//     snapshot data was captured at all — either way, decompressRrwebEvent
//     never even attempted anything.
//   - inflateOk === 0: cv-marked events were seen but not one field
//     inflated — e.g. the encoding changed from a gzip binary-string to
//     something else and every attempt threw.
//   - inflateFailed > 0: SOME fields inflated but at least one did not —
//     posthog-js's per-field encoding is drifting, not fully broken.
//   - fullBytes <= netBytes: the decode ran but added nothing — e.g. an
//     accidental `expandedText: text` regression that stops actually
//     appending decompressed DOM content.
export function decompressionHealth(stats, { netBytes, fullBytes } = {}) {
  if (!stats || stats.cvMarkedEvents === 0) {
    return {
      ok: false,
      reason:
        `no rrweb event matched the cv:"2024-10" compression marker ` +
        `(cvMarkedEvents=${stats?.cvMarkedEvents ?? 0}) — the marker may have drifted, ` +
        `or no compressed snapshot data was captured at all`,
    };
  }
  if (stats.inflateOk === 0) {
    return {
      ok: false,
      reason:
        `${stats.cvMarkedEvents} cv-marked event(s) seen but zero compressed field(s) inflated ` +
        `successfully (compressedFieldsSeen=${stats.compressedFieldsSeen}, inflateOk=0)`,
    };
  }
  if (stats.inflateFailed > 0) {
    return {
      ok: false,
      reason:
        `${stats.inflateFailed} of ${stats.compressedFieldsSeen} compressed field(s) failed to inflate ` +
        `— posthog-js's per-field gzip encoding may have changed`,
    };
  }
  if (!(fullBytes > netBytes)) {
    return {
      ok: false,
      reason:
        `expanded (decompressed) haystack (${fullBytes} B) is not strictly larger than the ` +
        `network-only haystack (${netBytes} B) — decode() did not actually add decompressed DOM content`,
    };
  }
  return {
    ok: true,
    reason:
      `${stats.cvMarkedEvents} cv-marked event(s), ${stats.inflateOk}/${stats.compressedFieldsSeen} field(s) ` +
      `inflated, 0 failures, expanded haystack ${fullBytes} B > network-only ${netBytes} B`,
  };
}
