import { useState, useEffect } from "react";
import { suggestPaths } from "./api.js";
import { optionValue } from "./pathSuggest.js";

// Directory autocomplete over GET /api/fs/suggest, shared by Settings' add-repo
// and scan-root fields and the composer's free-text repository input. Options
// refresh 150ms after the last keystroke; the `live` guard keeps a stale
// response from landing after unmount. Each mounted instance needs its own
// `listId` — two datalists with one id break the browser's input pairing.
export default function PathInput({
  value, onChange, listId, placeholder,
  className, style, autoFocus = false, ...rest
}) {
  const [opts, setOpts] = useState([]);
  const [prefix, setPrefix] = useState(undefined);
  useEffect(() => {
    let live = true;
    const t = setTimeout(async () => {
      const res = await suggestPaths(value);
      if (live) { setOpts(res.suggestions || []); setPrefix(res.prefix); }
    }, 150);
    return () => { live = false; clearTimeout(t); };
  }, [value]);
  // prefix === "" means the server listed the typed folder's CHILDREN (it is
  // itself a complete, existing directory) rather than filtering by a
  // trailing partial segment - the suggestion belongs AFTER it, not spliced
  // in place of its last segment. Older servers that omit `prefix` keep the
  // pre-existing splice behaviour.
  const children = prefix === "" && !/[\\/]$/.test(value || "");
  return (
    <>
      <input
        className={className} list={listId} value={value}
        placeholder={placeholder} spellCheck={false} autoFocus={autoFocus}
        onChange={(e) => onChange(e.target.value)}
        style={style}
        {...rest}
      />
      <datalist id={listId} className="ph-no-capture">
        {/* Value must match the typed text or the native datalist hides it
            (a ~/-relative input never matches an absolute path). is_repo was
            removed from /api/fs/suggest — every entry is a folder. */}
        {opts.map((o) => (
          <option key={o.path} value={optionValue(value, o.name, children)}>folder</option>
        ))}
      </datalist>
    </>
  );
}
