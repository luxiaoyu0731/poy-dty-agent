import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import http from "node:http";
import https from "node:https";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { chromium } from "playwright";
import { createScryptVerifier } from "../scripts/public-password-auth.mjs";

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve(server.address().port));
  });
}

// Real TLS preserves Chromium's native form headers, including Sec-Fetch-Site.
// HTTP tests that synthesize Origin cannot catch this referrer-policy regression.
for (const mode of ["single_user_password", "public"]) test(`native TLS browser enforces the ${mode} access contract`, { timeout: 60_000 }, async () => {
  const temporary = await mkdtemp(path.join(os.tmpdir(), "poy-login-browser-"));
  const password = "isolated browser fixture password";
  let browser;
  let backend;
  const received = [];
  let server;
  let tlsServer;
  let submission;
  let loginStatus;
  try {
    backend = http.createServer((req, res) => {
      received.push({ url: req.url, headers: req.headers });
      res.writeHead(200, { "content-type": "application/json" }); res.end(JSON.stringify({ ok: true }));
    });
    process.env.PUBLIC_BACKEND_ORIGIN = `http://127.0.0.1:${await listen(backend)}`;
    process.env.PUBLIC_BACKEND_TOKEN = "isolated-native-browser-token";
    execFileSync("openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes",
      "-keyout", path.join(temporary, "key.pem"), "-out", path.join(temporary, "cert.pem"),
      "-days", "1", "-subj", "/CN=localhost"], { stdio: "ignore" });
    tlsServer = https.createServer({
      key: await readFile(path.join(temporary, "key.pem")),
      cert: await readFile(path.join(temporary, "cert.pem"))
    }, (req, res) => {
      if (req.url === "/auth/login") {
        submission = { origin: req.headers.origin, site: req.headers["sec-fetch-site"] };
      }
      const upstream = http.request({ host: "127.0.0.1", port: server.address().port,
        path: req.url, method: req.method, headers: req.headers }, (response) => {
        if (req.url === "/auth/login") loginStatus = response.statusCode;
        res.writeHead(response.statusCode, response.headers);
        response.pipe(res);
      });
      upstream.on("error", () => { res.writeHead(502); res.end(); });
      req.pipe(upstream);
    });
    const origin = `https://localhost:${await listen(tlsServer)}`;
    process.env.PUBLIC_AUTH_MODE = mode;
    process.env.PUBLIC_CANONICAL_ORIGIN = origin;
    process.env.PUBLIC_LOGIN_PASSWORD_HASH = await createScryptVerifier(password);
    ({ server } = await import(`../scripts/local-public-server.mjs?browser-login-test=${mode}`));
    await listen(server);
    browser = await chromium.launch({ headless: true });
    // Self-signed certificate applies only to this isolated loopback fixture.
    const context = await browser.newContext({ ignoreHTTPSErrors: true });
    const page = await context.newPage();
    if (mode === "single_user_password") {
      await page.goto(`${origin}/login`);
      await page.getByLabel("密码", { exact: true }).fill(password);
      await Promise.all([
        page.waitForURL((url) => url.pathname !== "/login"),
        page.getByRole("button", { name: "登录", exact: true }).click()
      ]);
      assert.deepEqual(submission, { origin, site: "same-origin" });
      assert.equal(loginStatus, 303);
    }
    const session = await page.goto(`${origin}/auth/session`);
    assert.equal(session.status(), 200);
    assert.deepEqual(await session.json(), { authenticated: mode !== "public", mode });
    if (mode === "public") {
      const status = await page.evaluate(async () => (await fetch("/api/v1/sources/fetch-configured", {
        method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": "native-public-fixture" }, body: "{}"
      })).status);
      assert.equal(status, 200, "native browser POST needs no password or CSRF cookie");
      const last = received.at(-1);
      assert.equal(last.url, "/api/v1/sources/fetch-configured");
      assert.equal(last.headers["x-internal-token"], "isolated-native-browser-token");
      assert.equal(last.headers["idempotency-key"], "native-public-fixture");
      assert.equal(last.headers.cookie, undefined);
    }
  } finally {
    await browser?.close();
    if (tlsServer) await new Promise((resolve) => tlsServer.close(resolve));
    if (server) await new Promise((resolve) => server.close(resolve));
    if (backend) { backend.closeAllConnections(); await new Promise((resolve) => backend.close(resolve)); }
    await rm(temporary, { recursive: true, force: true });
  }
});
