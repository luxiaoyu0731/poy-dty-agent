import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";
import { bareApexRedirectLocation } from "../scripts/local-public-server.mjs";

const canonicalOrigin = "https://app.example.test";
const bareApexHost = "example.test";
const listen = (server) => new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server.address().port)));
const close = (server) => new Promise((resolve) => { server.closeAllConnections(); server.close(resolve); });

// Raw request helper: fetch() forbids overriding the Host header, and the
// redirect under test is keyed on Host, so drive node:http directly.
function request({ port, method = "GET", path = "/", host }) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: "127.0.0.1", port, method, path, headers: { host } }, (res) => {
      const chunks = [];
      res.on("data", (chunk) => chunks.push(chunk));
      res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString("utf8") }));
    });
    req.on("error", reject);
    if (method !== "GET" && method !== "HEAD") req.end("{}");
    else req.end();
  });
}

test("bare apex redirect location derivation", () => {
  const url = (path) => new URL(path, canonicalOrigin);
  assert.equal(bareApexRedirectLocation(bareApexHost, canonicalOrigin, url("/")), "https://app.example.test/");
  assert.equal(
    bareApexRedirectLocation(`${bareApexHost}:443`, canonicalOrigin, url("/assistant?module=workbench&tab=2")),
    "https://app.example.test/assistant?module=workbench&tab=2"
  );
  // Canonical host, unrelated hosts, and empty hosts are served inline.
  assert.equal(bareApexRedirectLocation("app.example.test", canonicalOrigin, url("/")), null);
  assert.equal(bareApexRedirectLocation("other.example.test", canonicalOrigin, url("/")), null);
  assert.equal(bareApexRedirectLocation("127.0.0.1:4173", canonicalOrigin, url("/")), null);
  assert.equal(bareApexRedirectLocation("", canonicalOrigin, url("/")), null);
  assert.equal(bareApexRedirectLocation(undefined, canonicalOrigin, url("/")), null);
  // A canonical host without a droppable subdomain label never redirects.
  assert.equal(bareApexRedirectLocation("example.test", "https://example.test", url("/")), null);
});

test("bare apex requests get a permanent 308 to the canonical origin before auth or csrf", async () => {
  const backendHits = [];
  const backend = http.createServer((req, res) => {
    backendHits.push(req.url);
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: true }));
  });
  const backendPort = await listen(backend);
  Object.assign(process.env, {
    PUBLIC_BACKEND_ORIGIN: `http://127.0.0.1:${backendPort}`,
    PUBLIC_BACKEND_TOKEN: "isolated-bare-apex-test-token",
    PUBLIC_AUTH_MODE: "public",
    PUBLIC_CANONICAL_ORIGIN: canonicalOrigin
  });
  delete process.env.PUBLIC_LOGIN_PASSWORD_HASH;
  const { server } = await import(`../scripts/local-public-server.mjs?bare-apex-test=${Date.now()}`);
  const port = await listen(server);
  try {
    // GET on the bare apex: 308 to the canonical origin, path and query intact.
    const home = await request({ port, host: bareApexHost, path: "/?module=reports&reportView=ledger" });
    assert.equal(home.status, 308);
    assert.equal(home.headers.location, `${canonicalOrigin}/?module=reports&reportView=ledger`);
    assert.equal(home.headers["cache-control"], "no-store");

    // POST on the bare apex (the old 403 csrf path): also a 308 redirect that
    // browsers follow while preserving method and body, and the proxy must not
    // have touched the backend for the redirected request.
    const post = await request({
      port, host: bareApexHost, method: "POST",
      path: "/api/v1/assistant/chat?trace=1",
      headers: {}
    });
    assert.equal(post.status, 308);
    assert.equal(post.headers.location, `${canonicalOrigin}/api/v1/assistant/chat?trace=1`);
    assert.deepEqual(backendHits, []);

    // /healthz is exempt so bare-domain probes keep a direct 200.
    const healthz = await request({ port, host: bareApexHost, path: "/healthz" });
    assert.equal(healthz.status, 200);
    assert.deepEqual(JSON.parse(healthz.body), { status: "ok" });

    // Zero behavior change on the canonical host and on direct loopback access.
    // (/api/v1/health/live intentionally bypasses the proxy cache, so each of
    // the two iterations below must reach the stub backend exactly once.)
    for (const host of ["app.example.test", `127.0.0.1:${port}`]) {
      const canonicalHealth = await request({ port, host, path: "/healthz" });
      assert.equal(canonicalHealth.status, 200, host);
      const canonicalApi = await request({ port, host, path: "/api/v1/health/live" });
      assert.equal(canonicalApi.status, 200, host);
      assert.deepEqual(JSON.parse(canonicalApi.body), { ok: true });
    }
    assert.deepEqual(backendHits, ["/api/v1/health/live", "/api/v1/health/live"]);
  } finally {
    await close(server);
    await close(backend);
  }
});
