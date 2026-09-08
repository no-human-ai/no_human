#!/usr/bin/env python3
"""Drive a real MCP handshake against the MCP server and check what it answers.

This is what an MCP registry does to decide whether a server is healthy: start
it, send `initialize`, then ask for `tools/list`. Running it ourselves — in CI,
against the image built from `Dockerfile.mcp` — is the difference between
learning the image is broken from our own red check and learning it from a
third party's "unhealthy" badge weeks later.

Usage:
    python3 packaging/mcp_handshake_probe.py <docker-image>            # container
    python3 packaging/mcp_handshake_probe.py --command nh mcp-serve    # directly

STDOUT IS THE PROTOCOL. The server speaks JSON-RPC over stdin/stdout, so this
probe writes diagnostics to stderr and runs `docker run -i` without a TTY.
Exit status is the verdict: 0 when the handshake and the tool list are what the
bridge promises, 1 when they are not, 2 on bad usage.

Two failure modes are handled because they are the LIKELY ones for this server,
not because they are exotic:

* **The server exits immediately.** `nh mcp-serve` does exactly that when the
  local API is missing — the case this image exists to fix. Writing the next
  request into a dead pipe raises `BrokenPipeError`, so every write is guarded
  and a dead child is reported as a verdict, not a traceback.
* **The server goes quiet.** `readline()` blocks with no deadline, so a timeout
  measured around it is fiction: a wedged server would hang the probe forever
  (measured: a 2 s "timeout" returned after 120 s, when the child happened to
  exit). Both pipes are therefore drained by reader threads and the timeout is
  taken on a queue, which is a deadline that actually holds. Draining stderr
  continuously also removes the second hang: a server that writes more than a
  pipe buffer of logs would otherwise block on its own stderr.

It calls no tool. `task_add` would file real work; a health check must not.
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading

EXPECTED_TOOLS = ["task_add", "task_status"]
PROTOCOL = "2025-06-18"
STDERR_TAIL = 4000


def _pump_stdout(stream, q: queue.Queue) -> None:
    for line in stream:
        if line.strip():
            q.put(line)
    q.put(None)  # EOF


def _pump_stderr(stream, sink: list[str]) -> None:
    for line in stream:
        sink.append(line)
        del sink[:-400]  # keep a bounded tail, never the whole log


class Probe:
    def __init__(self, cmd: list[str]) -> None:
        print(f"probing: {' '.join(cmd)}", file=sys.stderr)
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self.lines: queue.Queue = queue.Queue()
        self.err: list[str] = []
        for target, args in ((_pump_stdout, (self.proc.stdout, self.lines)),
                             (_pump_stderr, (self.proc.stderr, self.err))):
            threading.Thread(target=target, args=args, daemon=True).start()

    def send(self, obj: dict) -> str | None:
        """Write one JSON-RPC message. Returns a problem string, or None."""
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            rc = self.proc.poll()
            return (f"the server closed its input"
                    f"{f' (it exited with {rc})' if rc is not None else ''}"
                    f" — it did not stay up to answer {obj.get('method')!r}")
        return None

    def read(self, timeout: float) -> tuple[dict | None, str | None]:
        """One JSON message, or (None, problem). The deadline really holds."""
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            return None, f"no response within {timeout:.0f}s (the server is up but silent)"
        if line is None:
            rc = self.proc.poll()
            return None, f"the server closed its output (exit {rc})"
        try:
            return json.loads(line), None
        except json.JSONDecodeError:
            # A non-JSON line on stdout is itself a defect: it corrupts the
            # stream every MCP client reads.
            return None, f"non-JSON line on stdout: {line[:200]!r}"

    def close(self) -> str:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        return "".join(self.err)[-STDERR_TAIL:]


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    if argv[0] == "--command":
        cmd = argv[1:]
        if not cmd:
            print("--command needs a command", file=sys.stderr)
            return 2
    else:
        # `-e NH_ENV` with NO value forwards the host's NH_ENV when it is set
        # and passes nothing when it is not. Without it the container sees no
        # CI marker at all, HOME is /root so the "dev" branch cannot catch it
        # either, and the image's own lifespan tags `app_started` as
        # environment=real — a CI run counted as an install (issue #120).
        cmd = ["docker", "run", "-i", "--rm", "-e", "NH_ENV", argv[0]]

    probe = Probe(cmd)
    problems: list[str] = []
    try:
        problems.append(probe.send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "no_human-handshake-probe", "version": "1"},
            },
        }))
        # Generous: a container has to bring the API up before the bridge speaks.
        init, problem = probe.read(timeout=180.0)
        problems.append(problem)
        if init and init.get("result"):
            info = init["result"].get("serverInfo", {})
            print(f"initialize ok: {info.get('name')} {info.get('version')}"
                  f" (protocol {init['result'].get('protocolVersion')})")
        elif init is not None:
            problems.append(f"initialize returned no result: {json.dumps(init)[:200]}")

        names: list[str] = []
        if not [p for p in problems if p]:
            problems.append(probe.send({"jsonrpc": "2.0", "method": "notifications/initialized"}))
            problems.append(probe.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}))
            tools_msg, problem = probe.read(timeout=60.0)
            problems.append(problem)
            names = sorted(t.get("name") for t in (tools_msg or {}).get("result", {}).get("tools", []))
            if names == EXPECTED_TOOLS:
                print(f"tools/list ok: {names}")
            elif not [p for p in problems if p]:
                problems.append(f"tools/list returned {names}, expected {EXPECTED_TOOLS}")
    finally:
        tail = probe.close()
        if tail:
            print("--- server stderr (tail) ---", file=sys.stderr)
            print(tail, file=sys.stderr)

    real = [p for p in problems if p]
    if real:
        for p in real:
            print(f"FAIL: {p}")
        return 1
    print("HANDSHAKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
