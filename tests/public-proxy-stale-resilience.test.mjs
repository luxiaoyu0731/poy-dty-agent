import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";
import { setTimeout as delay } from "node:timers/promises";

// Regression test for the R08 outage pattern: the proxy served a stale cache
// entry while the in-flight refresh later rejected (backend hang → upstream
// timeout). That rejection used to surface as an unhandled rejection and kill
// the whole frontend process, so every public request 502-ed until launchd
// restarted it.

const backendHits = { ok: 0, hung: 0 };
let hangAfterFirst = false;

const backend = http.createServer((req, res) => {
  if (hangAfterFirst) {
    backendHits.hung += 1;
    return; // never respond; the proxy's upstream timeout must fire
  }
  backendHits.ok += 1;
  res.writeHead(200, { "content-type": "application/json" });
  res.end(JSON.stringify({ ok: true, hit: backendHits.ok }));
});

const listen = (server) =>
  new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server.address().port)));

const backendPort = await listen(backend);
process.env.PUBLIC_BACKEND_ORIGIN = `http://127.0.0.1:${backendPort}`;
process.env.PUBLIC_BACKEND_TOKEN = "isolated-stale-token";
process.env.PUBLIC_AUTH_MODE = "public";
process.env.PUBLIC_CANONICAL_ORIGIN = "https://public.example.test";
process.env.PUBLIC_API_CACHE_TTL_MS = "0"; // every cached entry is immediately stale
process.env.PUBLIC_API_STALE_CACHE_TTL_MS = "60_000";
process.env.PUBLIC_API_UPSTREAM_TIMEOUT_MS = "150"; // hung backend times out fast

const { server } = await import(`../scripts/local-public-server.mjs?stale-test=${Date.now()}`);
const close = (s) => new Promise((resolve) => { s.closeAllConnections(); s.close(resolve); });
const port = await new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server.address().port)));
const base = `http://127.0.0.1:${port}`;

test("stale cache survives a rejecting in-flight refresh without killing the proxy", async () => {
  const first = await fetch(base + "/api/v1/stale-probe");
  assert.equal(first.status, 200);
  assert.deepEqual(await first.json(), { ok: true, hit: 1 });

  hangAfterFirst = true;
  const stale = await fetch(base + "/api/v1/stale-probe");
  assert.equal(stale.status, 200, "stale entry must still be served");
  assert.equal((await stale.json()).hit, 1);

  // The abandoned refresh rejects via upstream timeout shortly after; give it
  // time to surface. Without the fix this crashes the process (and this test).
  await delay(500);
  const stillUp = await fetch(base + "/api/v1/stale-probe");
  assert.equal(stillUp.status, 200, "proxy must stay alive after the refresh rejection");
  assert.equal((await stillUp.json()).hit, 1);
  assert.equal(backendHits.hung >= 1, true, "the hung refresh attempt must have been made");
  await close(server);
  await close(backend);
});
