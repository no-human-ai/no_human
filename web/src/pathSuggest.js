// A native <datalist> only surfaces options whose `value` starts with the text
// in the paired <input>. So an absolute /Users/you/Downloads option never
// matches a ~/-relative input like "~/Dow" — no completion ever appears. The
// fix is to build each option value in the SAME shape as what the user typed:
// keep everything up to and including the last separator they typed (the dir
// prefix), then append the suggestion's directory name. The cut is on the
// last "/" OR "\" so a Windows input (`C:\Users\me\Doc`) completes too — a
// bare `.lastIndexOf("/")` never finds a backslash-only path, so every option
// fell back to the bare folder name and never started with the input.
//
//   optionValue("~/Dow", "Downloads")        -> "~/Downloads"
//   optionValue("/Users/x/Dow", "Downloads") -> "/Users/x/Downloads"
//   optionValue("~/git/", "svc")             -> "~/git/svc"
//   optionValue("proj", "projects")          -> "projects"   (no separator yet)
//   optionValue("", "git")                   -> "git"
//   optionValue("C:\\Users\\me\\Doc", "Documents") -> "C:\\Users\\me\\Documents"
//   optionValue("D:\\", "work")               -> "D:\\work"
//
// `children`: the server can now report it listed the CHILDREN of the typed
// path (an existing folder named in full, e.g. "~/work") rather than filtering
// by a trailing partial segment. In that case the input itself is a complete
// directory and the suggestion must be appended AFTER it, not spliced in
// place of its last segment:
//
//   optionValue("~/work", "svc", true)       -> "~/work/svc"
//   optionValue("C:\\Users\\me", "work", true) -> "C:\\Users\\me\\work"
//   optionValue("~/work/", "svc", true)      -> "~/work/svc"  (already has a
//                                                trailing separator: falls
//                                                through to the cut behaviour)
export function optionValue(input, name, children = false) {
  const s = input || "";
  const n = name || "";
  if (children && s && !/[\\/]$/.test(s)) {
    const sep = s.includes("\\") && !s.includes("/") ? "\\" : "/";
    return s + sep + n;
  }
  const cut = Math.max(s.lastIndexOf("/"), s.lastIndexOf("\\"));
  return (cut >= 0 ? s.slice(0, cut + 1) : "") + n;
}
