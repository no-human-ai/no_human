#!/usr/bin/env python3
"""Fail when a session-loaded context file is over the load limit.
CLAUDE.md: Anthropic targets <200 lines. MEMORY.md: only the first 200 lines / 25 KB load;
the rest is silently dropped — so >24,000 bytes is already a truncation. Size and links only:
prose is never inspected (regex-over-prose guards have cost this project nine review rounds)."""
import argparse, re, sys
from pathlib import Path
LINK = re.compile(r"\]\(([^)]+)\)")

def check_claude(path: Path, max_lines: int) -> list[str]:
    if not path.exists():  # public export ships the workflow but not the instruction file it sizes — nothing loaded, nothing to size-check
        print(f"note: {path} absent — skipping size check"); return []
    n = len(path.read_text(encoding="utf-8").splitlines())
    return [f"{path}: {n} lines > {max_lines}"] if n > max_lines else []

def check_memory(path: Path, max_lines: int, max_bytes: int, max_line_chars: int) -> list[str]:
    fails, text = [], path.read_text(encoding="utf-8")
    size, lines = len(text.encode()), text.splitlines()
    if size > max_bytes: fails.append(f"{path}: {size} bytes > {max_bytes} (tail would not load)")
    if len(lines) > max_lines: fails.append(f"{path}: {len(lines)} lines > {max_lines}")
    seen: dict[str, int] = {}
    for i, line in enumerate(lines, 1):
        if len(line) > max_line_chars: fails.append(f"{path}: line {i} is {len(line)} chars > {max_line_chars}")
        for target in LINK.findall(line):
            if target.startswith(("http://", "https://")): continue
            if target in seen: fails.append(f"{path}: duplicate target {target} (lines {seen[target]} and {i})")
            seen.setdefault(target, i)
            if not (path.parent / target).exists(): fails.append(f"{path}: missing target {target} (line {i})")
    return fails

def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--claude"); ap.add_argument("--memory")
    ap.add_argument("--max-lines", type=int, default=200); ap.add_argument("--max-bytes", type=int, default=24000)
    ap.add_argument("--max-line-chars", type=int, default=200); a = ap.parse_args()
    fails: list[str] = []
    if a.claude: fails += check_claude(Path(a.claude), a.max_lines)
    if a.memory: fails += check_memory(Path(a.memory), a.max_lines, a.max_bytes, a.max_line_chars)
    if not a.claude and not a.memory: print("nothing to check"); return 2
    for f in fails: print("FAIL:", f)
    print("VERDICT=" + ("FAIL" if fails else "OK")); return 1 if fails else 0

if __name__ == "__main__": sys.exit(main())
