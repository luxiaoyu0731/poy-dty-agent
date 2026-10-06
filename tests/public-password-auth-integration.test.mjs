import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";

import { createScryptVerifier } from "../scripts/public-password-auth.mjs";

const password = "correct horse battery staple";
const canonicalOrigin = "https://app.example.test";

function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve(server.address().port));
  });
}

function close(server) {
  return new Promise((resolve) => server.close(resolve));
}

function cookiePair(setCookie, name) {
  const expression = new RegExp(`(?:^|, )${name}=([^;]*)`);
  const match = expression.exec(setCookie);
  return match ? `${name}=${match[1]}` : "";
}

test("public proxy protects static and API routes behind the password session", async () => {
  const upstreamRequests = [];
  const backend = http.createServer((req, res) => {
    upstreamRequests.push({ method: req.method, url: req.url, headers: req.headers });
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: true }));
  });
  const backendPort = await listen(backend);

  process.env.PUBLIC_BACKEND_ORIGIN = `http://127.0.0.1:${backendPort}`;
  process.env.PUBLIC_BACKEND_TOKEN = "server-side-only-token";
  process.env.PUBLIC_AUTH_MODE = "single_user_password";
  process.env.PUBLIC_CANONICAL_ORIGIN = canonicalOrigin;
  process.env.PUBLIC_LOGIN_PASSWORD_HASH = await createScryptVerifier(password, Buffer.alloc(16, 9));
  const { server } = await import(`../scripts/local-public-server.mjs?integration=${Date.now()}`);
  const proxyPort = await listen(server);
  const base = `http://127.0.0.1:${proxyPort}`;

  try {
    const health = await fetch(`${base}/healthz`);
    assert.equal(health.status, 200);

    const anonymousPage = await fetch(`${base}/`, { redirect: "manual" });
    assert.equal(anonymousPage.status, 303);
    assert.equal(anonymousPage.headers.get("location"), "/login");
    assert.equal((await fetch(`${base}/release.json`, { redirect: "manual" })).status, 303);
    assert.equal((await fetch(`${base}/api/v1/health/deep`)).status, 401);
    assert.equal((await fetch(`${base}/metrics`)).status, 401);
    assert.equal(upstreamRequests.length, 0);

    const rejectedBrowser = await fetch(`${base}/auth/login`, {
      method: "POST", headers: { accept: "text/html", "content-type": "application/x-www-form-urlencoded", origin: "https://other.example.test" },
      body: "password=untrusted&csrf_token=expired"
    });
    assert.equal(rejectedBrowser.status, 403);
    assert.match(rejectedBrowser.headers.get("content-type"), /text\/html/);
    assert.match(await rejectedBrowser.text(), /返回登录页/);
    assert.equal(rejectedBrowser.headers.get("set-cookie"), null);

    const loginPage = await fetch(`${base}/login`);
    assert.equal(loginPage.headers.get("referrer-policy"), "same-origin");
    const html = await loginPage.text();
    const csrfToken = /name="csrf_token" value="([^"]+)"/.exec(html)?.[1] ?? "";
    const loginCsrfCookie = cookiePair(loginPage.headers.get("set-cookie") ?? "", "__Host-poy_dty_login_csrf");
    assert.ok(csrfToken);
    assert.ok(loginCsrfCookie);

    const login = await fetch(`${base}/auth/login`, {
      method: "POST",
      redirect: "manual",
      headers: {
        "content-type": "application/x-www-form-urlencoded",
        cookie: loginCsrfCookie,
        origin: canonicalOrigin,
        "sec-fetch-site": "same-origin"
      },
      body: new URLSearchParams({ password, csrf_token: csrfToken })
    });
    assert.equal(login.status, 303);
    const setCookie = login.headers.get("set-cookie") ?? "";
    const sessionCookie = cookiePair(setCookie, "__Host-poy_dty_session");
    const csrfCookie = cookiePair(setCookie, "__Host-poy_dty_csrf");
    assert.ok(sessionCookie);
    assert.ok(csrfCookie);
    const csrf = csrfCookie.split("=", 2)[1];
    const cookies = `${sessionCookie}; ${csrfCookie}`;

    const authenticatedPage = await fetch(`${base}/`, { headers: { cookie: cookies } });
    assert.equal(authenticatedPage.status, 200);
    assert.equal(authenticatedPage.headers.get("cache-control"), "private, no-store");
    assert.equal(authenticatedPage.headers.get("referrer-policy"), "no-referrer");
    assert.match(authenticatedPage.headers.get("content-security-policy") ?? "", /script-src 'self'/);
    const csp = authenticatedPage.headers.get("content-security-policy");
    assert.match(csp, /worker-src 'self'/);
    assert.match(csp, /img-src 'self' data:/);
    assert.doesNotMatch(csp, /unsafe-eval|blob:|script-src[^;]*unsafe-inline/);
    const release = await fetch(`${base}/release.json`, { headers: { cookie: cookies } });
    assert.equal(release.status, 200);
    assert.equal(release.headers.get("cache-control"), "private, no-store");

    const api = await fetch(`${base}/api/v1/workbench/market-chain`, {
      headers: {
        authorization: "Bearer attacker",
        cookie: cookies,
        "x-forwarded-for": "203.0.113.8",
        "x-internal-token": "attacker"
      }
    });
    assert.equal(api.status, 200);
    assert.equal(upstreamRequests.at(-1).headers["x-internal-token"], "server-side-only-token");
    assert.equal(upstreamRequests.at(-1).headers.authorization, undefined);
    assert.equal(upstreamRequests.at(-1).headers.cookie, undefined);
    assert.equal(upstreamRequests.at(-1).headers["x-forwarded-for"], undefined);

    for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
      const deniedMutation = await fetch(`${base}/api/v1/predictions`, {
        method,
        headers: { cookie: cookies, origin: canonicalOrigin, "sec-fetch-site": "same-origin" }
      });
      assert.equal(deniedMutation.status, 403);
    }
    const mutation = await fetch(`${base}/api/v1/predictions`, {
      method: "POST",
      headers: {
        cookie: cookies,
        origin: canonicalOrigin,
        "sec-fetch-site": "same-origin",
        "x-csrf-token": csrf
      }
    });
    assert.equal(mutation.status, 200);

    const logout = await fetch(`${base}/auth/logout`, {
      method: "POST",
      headers: {
        cookie: cookies,
        origin: canonicalOrigin,
        "sec-fetch-site": "same-origin",
        "x-csrf-token": csrf
      }
    });
    assert.equal(logout.status, 204);
    assert.match(logout.headers.get("set-cookie") ?? "", /Max-Age=0/);
    assert.equal((await fetch(`${base}/api/v1/workbench/market-chain`, { headers: { cookie: cookies } })).status, 401);
  } finally {
    await close(server);
    await close(backend);
  }
});
