import assert from "node:assert/strict";
import test from "node:test";
import { gunzip } from "node:zlib";
import { promisify } from "node:util";

import {
  acceptsGzip,
  encodeProxyBody,
  fetchWithTimeout,
  shouldBypassProxyCache,
  shouldServeStale
} from "../scripts/public-proxy-response.mjs";
import {
  backendRequestHeaders,
  isLoopbackBindHost,
  parseBackendOrigin
} from "../scripts/local-public-server.mjs";

const unzip = promisify(gunzip);

test("large JSON proxy responses are compressed for gzip clients", async () => {
  const body = Buffer.from(JSON.stringify({ items: ["x".repeat(50_000)] }));
  const encoded = await encodeProxyBody(
    body,
    { "content-type": "application/json; charset=utf-8" },
    "gzip, deflate"
  );

  assert.equal(encoded.headers["content-encoding"], "gzip");
  assert.match(encoded.headers.vary, /Accept-Encoding/);
  assert.ok(encoded.body.length < body.length);
  assert.deepEqual(await unzip(encoded.body), body);
});

test("small responses and clients without gzip stay uncompressed", async () => {
  const body = Buffer.from("{}");
  const encoded = await encodeProxyBody(
    body,
    { "content-type": "application/json" },
    ""
  );

  assert.equal(encoded.headers["content-encoding"], undefined);
  assert.equal(encoded.headers["content-length"], "2");
  assert.deepEqual(encoded.body, body);
});

test("stale cache is bounded by an explicit safety window", () => {
  const cached = { cachedAt: 1_000 };
  assert.equal(shouldServeStale(cached, 61_000, 300_000), true);
  assert.equal(shouldServeStale(cached, 301_001, 300_000), false);
});

test("gzip capability parsing does not accept unrelated encodings", () => {
  assert.equal(acceptsGzip("br, gzip;q=1.0"), true);
  assert.equal(acceptsGzip("br, deflate"), false);
});

test("release gates and health probes never consume stale proxy cache", () => {
  assert.equal(shouldBypassProxyCache("/api/v1/health/ready"), true);
  assert.equal(shouldBypassProxyCache("/api/v1/rag-index/status"), true);
  assert.equal(shouldBypassProxyCache("/api/v1/forecasts/seven-product/evidence"), true);
  assert.equal(shouldBypassProxyCache("/api/v1/workbench/market-chain", "", "poy-dty-release-gate/1.0"), true);
  assert.equal(shouldBypassProxyCache("/api/v1/workbench/market-chain", "no-cache"), false);
  assert.equal(shouldBypassProxyCache("/api/v1/workbench/market-chain"), false);
});

test("a hung upstream request is aborted so the proxy can retry later", async () => {
  const hangingFetch = (_target, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(signal.reason), { once: true });
  });

  await assert.rejects(fetchWithTimeout(hangingFetch, "http://backend.test", {}, 10), /upstream_timeout/);
});

test("public proxy strips caller credentials before adding its service credential", () => {
  assert.deepEqual(
    backendRequestHeaders(
      {
        "cf-access-authenticated-user-email": "owner@example.com",
        "cf-access-jwt-assertion": "signed-assertion",
        "cf-connecting-ip": "203.0.113.8",
        authorization: "Bearer caller-controlled",
        cookie: "session=caller-controlled",
        forwarded: "for=203.0.113.8",
        "x-forwarded-for": "203.0.113.8",
        "x-csrf-token": "caller-controlled",
        "x-internal-token": "caller-controlled",
        accept: "application/json"
      },
      "127.0.0.1:8000",
      "server-side-token"
    ),
    {
      host: "127.0.0.1:8000",
      "x-internal-token": "server-side-token",
      accept: "application/json"
    }
  );
});

test("public proxy may only bind to loopback", () => {
  assert.equal(isLoopbackBindHost("127.0.0.1"), true);
  assert.equal(isLoopbackBindHost("127.10.20.30"), true);
  assert.equal(isLoopbackBindHost("::1"), true);
  assert.equal(isLoopbackBindHost("::ffff:127.0.0.1"), true);
  assert.equal(isLoopbackBindHost("localhost"), true);
  assert.equal(isLoopbackBindHost("0.0.0.0"), false);
  assert.equal(isLoopbackBindHost("192.0.2.1"), false);
});

test("backend origin is restricted to a plain loopback HTTP origin", () => {
  assert.equal(parseBackendOrigin("http://127.0.0.1:8000").origin, "http://127.0.0.1:8000");
  for (const value of [
    "https://127.0.0.1:8000", "http://example.com:8000", "http://127.0.0.1:8000/path",
    "http://user:pass@127.0.0.1:8000", "http://127.0.0.1:8000?next=http://evil.test"
  ]) assert.throws(() => parseBackendOrigin(value), /plain HTTP loopback/);
});
