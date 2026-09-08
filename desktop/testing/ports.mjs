// Ports for the desktop test suite, handed out by the OS — never derived from
// process.pid. Two files running concurrently under `node --test` used to be
// able to compute the SAME port (overlapping bases/moduli), so a fake server in
// one file answered the other file's boot probe and loaded the board where the
// test expected the setup or error screen.
import http from "node:http";
import net from "node:net";

/** {host, port, origin} for a port the OS just handed out and we closed again:
 *  the nearest thing to a guaranteed-closed loopback port. Callers that need
 *  "nothing listening" MUST additionally assert probe(origin) !== "up". */
export function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close((err) => {
        if (err) { reject(err); return; }
        resolve({ host: "127.0.0.1", port, origin: `http://127.0.0.1:${port}` });
      });
    });
  });
}

/** {server, port, origin} — an http server already LISTENING on an
 *  OS-assigned 127.0.0.1 port. Mirrors server.test.mjs's serve() helper.
 *  The caller owns the server and closes it (test.after). */
export function listenOnFreePort(handler) {
  return new Promise((resolve, reject) => {
    const server = http.createServer(handler);
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      resolve({ server, port, origin: `http://127.0.0.1:${port}` });
    });
  });
}
