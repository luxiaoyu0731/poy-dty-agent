import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createPublicAccess } from "../scripts/public-password-auth.mjs";

const origin = "https://public.example.test";
const listen = (server) => new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server.address().port)));
const close = (server) => new Promise((resolve) => { server.closeAllConnections(); server.close(resolve); });

test("public mode is explicit and does not access a password verifier", () => {
  const access = createPublicAccess({ mode: "public", canonicalOrigin: origin,
    get passwordVerifier() { throw new Error("must not read a password"); } });
  assert.equal(access.isPublic, true);
  for (const mode of [undefined, "", "unknown"]) assert.throws(() => createPublicAccess({ mode, canonicalOrigin: origin }));
  assert.throws(() => createPublicAccess({ mode: "public", canonicalOrigin: "https://user:secret@example.test" }));
});

test("anonymous full access keeps write origin, internal token and idempotency boundaries", async () => {
  const received = [];
  const backend = http.createServer((req, res) => {
    received.push({ method: req.method, url: req.url, headers: req.headers });
    res.writeHead(200, { "content-type": "application/json", "set-cookie": "backend-session=must-not-escape",
      ...(req.url.includes("/export") ? { "content-disposition": 'attachment; filename="fixture.json"' } : {}) });
    res.end(JSON.stringify(req.url === "/api/v1/source-automation/status"
      ? { status: "ok", database: { path: "/private/operator/agent.db", ready: true } } : { ok: true }));
  });
  const backendPort = await listen(backend);
  Object.assign(process.env, { PUBLIC_BACKEND_ORIGIN: `http://127.0.0.1:${backendPort}`,
    PUBLIC_BACKEND_TOKEN: "isolated-server-only-token", PUBLIC_AUTH_MODE: "public", PUBLIC_CANONICAL_ORIGIN: origin });
  delete process.env.PUBLIC_LOGIN_PASSWORD_HASH;
  const { server, publicReleaseIdentity, previousReleaseAsset } = await import(`../scripts/local-public-server.mjs?public-test=${Date.now()}`);
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "poy-assets-"));
  try {
    const old = path.join(temporary, "releases", "20260907T140000Z-aaaaaaaaaaaaaaaa", "dist", "assets");
    await fs.mkdir(old, { recursive: true });
    await fs.writeFile(path.join(old, "Map-oldhash.js"), "export default 1");
    const current = path.join(temporary, "releases", "20260907T150000Z-bbbbbbbbbbbbbbbb");
    assert.equal((await previousReleaseAsset("/assets/Map-oldhash.js", current)).body.toString(), "export default 1");
    for (const requested of ["/assets/../../secret.js", "/release.json", "/assets/missing.js"]) {
      assert.equal(await previousReleaseAsset(requested, current), null);
    }
  } finally { await fs.rm(temporary, { recursive: true, force: true }); }
  assert.deepEqual(publicReleaseIdentity({ release_id: "fixture", release_hash: "abc", rollback_target: "/private/old", git_branch: "private" }),
    { release_id: "fixture", release_hash: "abc" });
  const base = `http://127.0.0.1:${await listen(server)}`;
  try {
    const missing = await fetch(base + "/assets/nonexistent-acceptance.js");
    assert.equal(missing.status, 404);
    assert.match(missing.headers.get("content-type"), /text\/plain/);
    // Hashed assets must be served gzip-compressed when requested and marked
    // immutable; index.html stays no-store and uncompressed for small bodies.
    const distAssets = path.join(
      path.dirname(fileURLToPath(import.meta.url)), "..", "dist", "assets");
    const assetName = (await fs.readdir(distAssets)).find((name) => name.endsWith(".js"));
    assert.ok(assetName, "dist build with at least one js asset is required");
    const compressed = await fetch(base + `/assets/${assetName}`, {
      headers: { "accept-encoding": "gzip" }
    });
    assert.equal(compressed.status, 200);
    assert.equal(compressed.headers.get("content-encoding"), "gzip");
    assert.equal(compressed.headers.get("cache-control"), "public, max-age=31536000, immutable");
    assert.match(String(compressed.headers.get("vary") ?? ""), /Accept-Encoding/i);
    const identity = await fetch(base + `/assets/${assetName}`, {
      headers: { "accept-encoding": "identity" }
    });
    assert.equal(identity.headers.get("content-encoding"), null);
    assert.equal(identity.headers.get("cache-control"), "public, max-age=31536000, immutable");
    const home = await fetch(base + "/", { headers: { "accept-encoding": "gzip" } });
    assert.equal(home.headers.get("cache-control"), "private, no-store");
    for (const path of ["/", "/?module=reports&reportView=ledger", "/api/v1/health/live", "/api/v1/health/ready"]) {
      const response = await fetch(base + path, { redirect: "manual" });
      assert.equal(response.status, 200, path);
      assert.equal(response.headers.get("set-cookie"), null);
      assert.doesNotMatch(await response.text(), /isolated-server-only-token/);
    }
    assert.deepEqual(await (await fetch(base + "/api/v1/source-automation/status")).json(), { status: "ok", database: { ready: true } });
    for (let attempt = 0; attempt < 2; attempt++) {
      const download = await fetch(base + "/api/v1/forecasts/seven-product/export?format=json");
      assert.equal(download.headers.get("content-disposition"), 'attachment; filename="fixture.json"');
      assert.equal(download.headers.get("set-cookie"), null);
      assert.deepEqual(await download.json(), { ok: true });
    }
    const observationReads = received.filter(item => item.url === "/api/v1/predictions/observations").length;
    await (await fetch(base + "/api/v1/predictions/observations")).json();
    await (await fetch(base + "/api/v1/predictions/observations")).json();
    assert.equal(received.filter(item => item.url === "/api/v1/predictions/observations").length, observationReads + 2);
    assert.deepEqual(await (await fetch(base + "/auth/session")).json(), { authenticated: false, mode: "public" });
    const login = await fetch(base + "/login", { redirect: "manual" });
    assert.equal(login.status, 303); assert.equal(login.headers.get("location"), "/");
    assert.equal((await fetch(base + "/auth/login", { method: "POST" })).status, 404);
    const prior = received.length;
    assert.equal((await fetch(base + "/api/v1/auth/local-session", { method: "POST", headers: { origin, "sec-fetch-site": "same-origin" } })).status, 204);
    assert.equal(received.length, prior, "no backend local session is issued");
    for (const path of ["/assistant/chat", "/sources/fetch-configured", "/workbench/snapshot/materialize", "/intelligence/feedback"]) {
      const response = await fetch(base + "/api/v1" + path, { method: "POST", headers: {
        origin, "sec-fetch-site": "same-origin", "content-type": "application/json", "idempotency-key": "public-fixture-1",
        authorization: "Bearer browser-value", cookie: "untrusted=value", "x-internal-token": "forged-browser-value"
      }, body: "{}" });
      assert.equal(response.status, 200, path);
      assert.equal(response.headers.get("set-cookie"), null);
      assert.deepEqual(await response.json(), { ok: true });
      const headers = received.at(-1).headers;
      assert.equal(headers["x-internal-token"], "isolated-server-only-token");
      assert.equal(headers.authorization, undefined); assert.equal(headers.cookie, undefined);
      assert.equal(headers["idempotency-key"], "public-fixture-1");
    }
    for (const headers of [{}, { origin: "https://other.example.test", "sec-fetch-site": "cross-site" }, { origin, "sec-fetch-site": "cross-site" }]) {
      for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
        const count = received.length;
        assert.equal((await fetch(base + "/api/v1/intelligence/feedback", { method, headers })).status, 403);
        assert.equal(received.length, count);
      }
    }
  } finally { await close(server); await close(backend); }
});
