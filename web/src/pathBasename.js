// The last path segment of a repository path, tolerant of BOTH separator
// styles. A repo path can be Windows-shaped (`C:\Users\me\svc`) as well as
// POSIX-shaped (`/Users/x/svc`) — a bare `.split("/").pop()` renders the
// whole backslash path as the "name" on Windows instead of just `svc`.
//
//   basename("C:\\Users\\me\\svc") -> "svc"
//   basename("/Users/x/svc/")      -> "svc"
//   basename("C:\\")               -> "C:"
//   basename("")                   -> ""
export function basename(p) {
  const s = String(p ?? "").replace(/[\\/]+$/, "");
  if (!s) return "";
  const segs = s.split(/[\\/]+/);
  return segs[segs.length - 1] || "";
}
