import http from "node:http";
import { realpathSync } from "node:fs";
import fs from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createPublicAccess } from "./public-password-auth.mjs";
import {
  encodeProxyBody,
  fetchWithTimeout,
  shouldBypassProxyCache,
  shouldServeStale
} from "./public-proxy-response.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "..");
const distDir = path.join(rootDir, "dist");
const backendOriginUrl = parseBackendOrigin(process.env.PUBLIC_BACKEND_ORIGIN ?? "http://127.0.0.1:8000");
const backendOrigin = backendOriginUrl.origin;
const backendToken = process.env.PUBLIC_BACKEND_TOKEN ?? process.env.INTERNAL_API_TOKEN ?? "";
const port = Number(process.env.PUBLIC_FRONTEND_PORT ?? 4173);
const host = process.env.PUBLIC_FRONTEND_HOST ?? "127.0.0.1";
const cacheTtlMs = Number(process.env.PUBLIC_API_CACHE_TTL_MS ?? 60_000);
const staleCacheTtlMs = Number(process.env.PUBLIC_API_STALE_CACHE_TTL_MS ?? 300_000);
const upstreamTimeoutMs = Number(process.env.PUBLIC_API_UPSTREAM_TIMEOUT_MS ?? 60_000);

const responseCache = new Map();
const inFlightRequests = new Map();
// A single proxy request must never take the whole frontend down: launchd
// restarts take seconds during which every public request fails with 502.
// The stale-serving path already prevents the known leak; this guard keeps
// any future leak from becoming an outage while still logging the reason.
process.on("unhandledRejection", (reason) => {
  console.error("public-frontend unhandledRejection:", reason);
});
// Match the deployed resources without permitting inline/eval scripts or blob workers.
// Cloudflare's existing beacon uses these two explicit vendor origins.
export const PUBLIC_CONTENT_SECURITY_POLICY = "default-src 'self'; script-src 'self' https://static.cloudflareinsights.com; connect-src 'self' https://cloudflareinsights.com; worker-src 'self'; img-src 'self' data:; font-src 'self' data:; style-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'";
let publicAuth;

function getPublicAuth() {
  if (!publicAuth) {
    const mode = process.env.PUBLIC_AUTH_MODE;
    publicAuth = createPublicAccess({
      mode,
      canonicalOrigin: process.env.PUBLIC_CANONICAL_ORIGIN,
      ...(mode === "public" ? {} : { passwordVerifier: process.env.PUBLIC_LOGIN_PASSWORD_HASH }),
      absoluteTtlSeconds: Number(process.env.PUBLIC_SESSION_ABSOLUTE_TTL_SECONDS ?? 43_200),
      idleTtlSeconds: Number(process.env.PUBLIC_SESSION_IDLE_TTL_SECONDS ?? 3_600),
      loginWindowSeconds: Number(process.env.PUBLIC_LOGIN_WINDOW_SECONDS ?? 900),
      perSourceLimit: Number(process.env.PUBLIC_LOGIN_MAX_ATTEMPTS ?? 5),
      globalLimit: Number(process.env.PUBLIC_LOGIN_GLOBAL_MAX_ATTEMPTS ?? 20)
    });
  }
  return publicAuth;
}

export function isLoopbackBindHost(value) {
  const hostValue = String(value ?? "").trim().toLowerCase().replace(/^\[|\]$/g, "");
  if (hostValue === "localhost" || hostValue === "::1") {
    return true;
  }
  if (hostValue.startsWith("::ffff:127.")) return true;
  return net.isIP(hostValue) === 4 && hostValue.startsWith("127.");
}

export function isMainModule(argvPath, moduleUrl = import.meta.url) {
  if (!argvPath) return false;
  const modulePath = fileURLToPath(moduleUrl);
  try {
    return realpathSync(argvPath) === realpathSync(modulePath);
  } catch {
    return path.resolve(argvPath) === path.resolve(modulePath);
  }
}

export function parseBackendOrigin(value) {
  const target = new URL(String(value));
  if (target.protocol !== "http:" || !isLoopbackBindHost(target.hostname) ||
      target.username || target.password || target.pathname !== "/" || target.search || target.hash) {
    throw new Error("PUBLIC_BACKEND_ORIGIN must be a plain HTTP loopback origin");
  }
  return target;
}

export function backendRequestHeaders(requestHeaders, targetHost, serviceToken = backendToken) {
  const headers = { host: targetHost };
  for (const name of ["accept", "accept-encoding", "cache-control", "content-length", "content-type", "user-agent", "x-request-id", "idempotency-key"]) {
    if (requestHeaders[name] !== undefined) headers[name] = requestHeaders[name];
  }
  if (serviceToken) {
    headers["x-internal-token"] = serviceToken;
  }
  return headers;
}

export function publicReleaseIdentity(release) {
  return Object.fromEntries(["release_id", "release_hash", "created_at", "git_sha", "modules"]
    .filter((key) => key in release).map((key) => [key, release[key]]));
}

// The site is served under one canonical origin (for example
// https://app.example.com) and Cloudflare routes the bare apex host to this
// same process. Serving the apex inline made browser POSTs fail the backend's
// canonical-origin (CSRF/same-origin) check with 403. Every bare-apex request
// is therefore answered with a permanent 308 redirect to the canonical origin:
// 308 preserves method and body, so the retried request carries the correct
// Origin header. Only the bare apex derived from the canonical host matches;
// every other Host (including the canonical host itself) is served normally.
export function bareApexRedirectLocation(hostHeader, canonicalOrigin, requestUrl) {
  let canonicalHost;
  try {
    canonicalHost = new URL(canonicalOrigin).hostname.toLowerCase();
  } catch {
    return null;
  }
  const canonicalLabels = canonicalHost.split(".").filter(Boolean);
  const bareApexHost = canonicalLabels.length > 2 ? canonicalLabels.slice(1).join(".") : null;
  if (!bareApexHost) return null;
  let requestHost;
  try {
    requestHost = new URL(`http://${String(hostHeader ?? "").trim()}`).hostname.toLowerCase();
  } catch {
    return null;
  }
  if (requestHost !== bareApexHost) return null;
  return `${new URL(canonicalOrigin).origin}${requestUrl.pathname}${requestUrl.search}`;
}

const prewarmPaths = [
  "/api/v1/delivery/status",
  "/api/v1/crawler/source-readiness",
  "/api/v1/prices/latest",
  "/api/v1/news/fetch-runs?limit=20",
  "/api/v1/assistant/llm-event-directions?limit=80",
  "/api/v1/agent-runs?limit=6&compact=true",
  "/api/v1/predictions/model-signal?target=POY%2FDTY+%E4%B8%8A%E6%B8%B8%E6%88%90%E6%9C%AC%E5%8E%8B%E5%8A%9B&horizon_days=14",
  "/api/v1/knowledge/evidence-queue?status=all&q=&limit=80",
  "/api/v1/workbench/market-chain",
  "/api/v1/workbench/event-library?limit=30&offset=0",
  "/api/v1/workbench/rag-visual?limit=8",
  "/api/v1/source-automation/status"
];

const mimeTypes = new Map([
  [".html", "text/html; charset=utf-8"],
  [".js", "text/javascript; charset=utf-8"],
  [".css", "text/css; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
  [".svg", "image/svg+xml"],
  [".png", "image/png"],
  [".jpg", "image/jpeg"],
  [".jpeg", "image/jpeg"],
  [".webp", "image/webp"],
  [".ico", "image/x-icon"],
  [".woff2", "font/woff2"]
]);

async function send(res, status, body, headers = {}, acceptEncoding = "") {
  if (res.destroyed || res.writableEnded) {
    return;
  }
  const encoded = await encodeProxyBody(body, {
    "content-security-policy": PUBLIC_CONTENT_SECURITY_POLICY,
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    ...headers
  }, acceptEncoding);
  res.writeHead(status, encoded.headers);
  res.end(encoded.body);
}

async function proxyToBackend(req, res, requestUrl) {
  const target = new URL(`${requestUrl.pathname}${requestUrl.search}`, backendOriginUrl);
  if (req.method === "GET") {
    const bypassCache = shouldBypassProxyCache(
      target.pathname,
      String(req.headers["cache-control"] ?? ""),
      String(req.headers["user-agent"] ?? "")
    );
    const cached = bypassCache
      ? await fetchBackendResponse(target).catch(() => null)
      : await getCachedBackendResponse(target);
    if (cached) {
      await send(
        res,
        cached.status,
        cached.body,
        cached.headers,
        String(req.headers["accept-encoding"] ?? "")
      );
      return;
    }
    if (bypassCache) {
      await send(
        res,
        502,
        JSON.stringify({ error: "backend_unavailable" }),
        { "content-type": "application/json; charset=utf-8" }
      );
      return;
    }
  }
  const headers = backendRequestHeaders(req.headers, target.host);
  const upstream = http.request(
    { hostname: backendOriginUrl.hostname, port: backendOriginUrl.port,
      path: target.pathname + target.search, method: req.method, headers },
    (upstreamRes) => {
      const responseHeaders = { ...upstreamRes.headers };
      delete responseHeaders["set-cookie"];
      delete responseHeaders.server;
      res.writeHead(upstreamRes.statusCode ?? 502, {
        ...responseHeaders,
        "cache-control": "private, no-store",
        "content-security-policy": PUBLIC_CONTENT_SECURITY_POLICY,
        "referrer-policy": "no-referrer",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY"
      });
      upstreamRes.pipe(res);
    }
  );
  upstream.on("error", (error) => {
    void send(
      res,
      502,
      JSON.stringify({ error: "backend_unavailable", message: error.message }),
      { "content-type": "application/json; charset=utf-8" }
    );
  });
  req.pipe(upstream);
}

async function fetchBackendResponse(target) {
  const requestHeaders = backendToken ? { "x-internal-token": backendToken } : undefined;
  const response = await fetchWithTimeout(fetch, target, { headers: requestHeaders }, upstreamTimeoutMs);
  let body = Buffer.from(await response.arrayBuffer());
  if (getPublicAuth().isPublic && target.pathname === "/api/v1/source-automation/status" && response.ok) {
    try {
      const status = JSON.parse(body.toString("utf8"));
      if (status.database && typeof status.database === "object") delete status.database.path;
      body = Buffer.from(JSON.stringify(status));
    } catch { /* Preserve the upstream response contract if it is not JSON. */ }
  }
  const headers = {
    "content-type": response.headers.get("content-type") ?? "application/octet-stream",
    "cache-control": "private, max-age=0"
  };
  // Preserve the upstream download contract on both cache misses and hits.
  // Keep the allowlist narrow: upstream cookies and credentials remain private.
  const disposition = response.headers.get("content-disposition");
  if (disposition) headers["content-disposition"] = disposition;
  return { status: response.status, headers, body, cachedAt: Date.now() };
}

async function getCachedBackendResponse(target) {
  const key = target.toString();
  const cached = responseCache.get(key);
  const now = Date.now();
  if (cached && now - cached.cachedAt < cacheTtlMs) {
    return cached;
  }
  const refresh = refreshBackendResponse(key, target);
  if (shouldServeStale(cached, now, staleCacheTtlMs)) {
    // Stale bytes answer this request; the in-flight refresh is shared with
    // later callers, so swallow its rejection here to keep it from surfacing
    // as an unhandled rejection that would take the whole proxy down.
    refresh.catch(() => {});
    return cached;
  }
  try {
    return await refresh;
  } catch {
    return cached ?? null;
  }
}

function refreshBackendResponse(key, target) {
  if (!inFlightRequests.has(key)) {
    inFlightRequests.set(
      key,
      fetchBackendResponse(target)
        .then((response) => {
          if (response.status >= 200 && response.status < 300) {
            responseCache.set(key, response);
          }
          return response;
        })
        .finally(() => inFlightRequests.delete(key))
    );
  }
  return inFlightRequests.get(key);
}

function resolveAssetPath(urlPath) {
  const decoded = decodeURIComponent(urlPath.split("?")[0] || "/");
  const normalized = path.normalize(decoded).replace(/^(\.\.[/\\])+/, "");
  const filePath = path.join(distDir, normalized);
  if (!filePath.startsWith(distDir)) {
    return null;
  }
  return filePath;
}

// Open tabs may request a lazily loaded, content-hashed chunk after a release
// switch. Retain access only to assets from the last five immutable releases.
export async function previousReleaseAsset(urlPath, currentRoot = rootDir) {
  if (!/^\/assets\/[A-Za-z0-9_.-]+\.(js|css|woff2?|png|svg)$/.test(urlPath)) return null;
  const releases = path.dirname(currentRoot);
  if (path.basename(releases) !== "releases") return null;
  try {
    const entries = (await fs.readdir(releases, { withFileTypes: true }))
      .filter(entry => entry.isDirectory() && /^\d{8}T\d{6}Z-[a-f0-9]{16}$/.test(entry.name))
      .sort((a, b) => b.name.localeCompare(a.name)).slice(0, 5);
    for (const entry of entries) {
      const candidate = path.join(releases, entry.name, "dist", urlPath);
      try { return { body: await fs.readFile(candidate), ext: path.extname(candidate) }; } catch { /* next retained release */ }
    }
  } catch { /* no retained release directory */ }
  return null;
}

async function sendLoginError(req, res, status, code, retryAfter) {
  const wantsHtml = String(req.headers.accept ?? "").includes("text/html");
  const message = status === 429 ? "登录尝试过于频繁，请稍后重试。"
    : status === 401 ? "密码未通过验证，请重新登录。"
    : "登录请求未通过验证，可能是页面已过期。请重新打开登录页再试。";
  const body = wantsHtml
    ? `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>登录未完成</title><main><h1>登录未完成</h1><p>${message}</p><p><a href="/login">返回登录页</a></p></main></html>`
    : JSON.stringify({ error: code });
  await send(res, status, body, {
    "cache-control": "no-store",
    "content-type": wantsHtml ? "text/html; charset=utf-8" : "application/json; charset=utf-8",
    "referrer-policy": "same-origin",
    ...(retryAfter ? { "retry-after": String(retryAfter) } : {})
  });
}

function loginPage(csrfToken) {
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · POY/DTY Agent</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#111827;color:#f9fafb;font:16px system-ui,sans-serif}
main{width:min(360px,calc(100vw - 48px));padding:32px;border:1px solid #374151;border-radius:16px;background:#1f2937}
h1{font-size:22px;margin:0 0 8px}p{color:#9ca3af;margin:0 0 24px}label{display:block;margin-bottom:8px}
input{box-sizing:border-box;width:100%;padding:12px;border:1px solid #4b5563;border-radius:8px;background:#111827;color:#fff}
button{width:100%;margin-top:16px;padding:12px;border:0;border-radius:8px;background:#2563eb;color:#fff;font-weight:700;cursor:pointer}
</style></head><body><main><h1>POY/DTY Agent</h1><p>请输入单用户访问密码</p>
<form method="post" action="/auth/login"><input type="hidden" name="csrf_token" value="${csrfToken}">
<label for="password">密码</label><input id="password" name="password" type="password" minlength="24" maxlength="256" autocomplete="current-password" required autofocus>
<button type="submit">登录</button></form></main></body></html>`;
}

async function readRequestBody(req, limit = 4_096) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) throw new Error("request_body_too_large");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

function isUnsafeMethod(method) {
  return !["GET", "HEAD", "OPTIONS"].includes(String(method ?? "GET").toUpperCase());
}

function requestSource(req) {
  const peer = req.socket.remoteAddress ?? "unknown";
  const asserted = String(req.headers["cf-connecting-ip"] ?? "").trim();
  return isLoopbackBindHost(peer) && net.isIP(asserted) ? asserted : peer;
}

function isApiPath(pathname) {
  return pathname === "/api" || pathname.startsWith("/api/") || pathname.startsWith("/metrics");
}

async function rejectUnauthenticated(res, pathname) {
  if (isApiPath(pathname)) {
    await send(res, 401, JSON.stringify({ error: "authentication_required" }), {
      "cache-control": "no-store",
      "content-type": "application/json; charset=utf-8"
    });
  } else {
    await send(res, 303, "", { "cache-control": "no-store", location: "/login" });
  }
}

export const server = http.createServer(async (req, res) => {
  const auth = getPublicAuth();
  if (!req.url?.startsWith("/") || req.url.startsWith("//")) {
    await send(res, 400, JSON.stringify({ error: "invalid_request_target" }), {
      "cache-control": "no-store", "content-type": "application/json; charset=utf-8"
    });
    return;
  }
  const requestUrl = new URL(req.url, auth.canonicalOrigin);
  const urlPath = requestUrl.pathname;

  // Bare-apex hosts are redirected before any authentication, CSRF, or login
  // handling. /healthz stays exempt so bare-domain probes keep a direct 200.
  const apexRedirect = bareApexRedirectLocation(req.headers.host, auth.canonicalOrigin, requestUrl);
  if (apexRedirect && urlPath !== "/healthz") {
    await send(res, 308, "", { "cache-control": "no-store", location: apexRedirect });
    return;
  }

  if (urlPath === "/healthz" && ["GET", "HEAD"].includes(req.method ?? "GET")) {
    await send(res, 200, req.method === "HEAD" ? "" : JSON.stringify({ status: "ok" }), {
      "cache-control": "no-store",
      "content-type": "application/json; charset=utf-8"
    });
    return;
  }
  if (urlPath === "/login" && req.method === "GET") {
    if (auth.isPublic) {
      await send(res, 303, "", { "cache-control": "no-store", location: "/" });
      return;
    }
    const challenge = auth.issueLoginChallenge(requestSource(req));
    if (challenge.limited) {
      await send(res, 429, "Too many login requests", {
        "cache-control": "no-store",
        "content-type": "text/plain; charset=utf-8",
        "retry-after": String(challenge.retryAfter)
      });
      return;
    }
    await send(res, 200, loginPage(challenge.token), {
      "cache-control": "no-store",
      // Native form POSTs under no-referrer send Origin: null, which our
      // same-origin login guard correctly rejects. Keep referrers local.
      "referrer-policy": "same-origin",
      "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
      "content-type": "text/html; charset=utf-8",
      "set-cookie": challenge.setCookie
    });
    return;
  }
  if (urlPath === "/auth/login" && req.method === "POST") {
    if (auth.isPublic) {
      await send(res, 404, JSON.stringify({ error: "password_login_disabled" }), {
        "cache-control": "no-store", "content-type": "application/json; charset=utf-8"
      });
      return;
    }
    if (!String(req.headers["content-type"] ?? "").toLowerCase().startsWith("application/x-www-form-urlencoded")) {
      await sendLoginError(req, res, 415, "unsupported_media_type");
      return;
    }
    try {
      const form = new URLSearchParams(await readRequestBody(req));
      const result = await auth.login({
        password: form.get("password"),
        csrfToken: form.get("csrf_token"),
        cookieHeader: req.headers.cookie,
        headers: req.headers,
        source: requestSource(req)
      });
      if (result.ok) {
        await send(res, 303, "", { "cache-control": "no-store", location: "/", "set-cookie": result.cookies });
      } else {
        await sendLoginError(req, res, result.status, result.error, result.retryAfter);
      }
    } catch (error) {
      const tooLarge = error instanceof Error && error.message === "request_body_too_large";
      await sendLoginError(req, res, tooLarge ? 413 : 400, "invalid_login_request");
    }
    return;
  }

  const authorization = auth.isPublic ? { authorized: true } : auth.authorize(req.headers.cookie);
  if (!authorization.authorized) {
    await rejectUnauthenticated(res, urlPath);
    return;
  }
  if (urlPath === "/auth/session" && req.method === "GET") {
    await send(res, 200, JSON.stringify({ authenticated: !auth.isPublic, mode: auth.isPublic ? "public" : "single_user_password" }), {
      "cache-control": "no-store", "content-type": "application/json; charset=utf-8"
    });
    return;
  }
  if (urlPath === "/auth/logout" && req.method === "POST") {
    if (!auth.validateCsrf(req.headers, authorization)) {
      await send(res, 403, JSON.stringify({ error: "csrf_validation_failed" }), {
        "cache-control": "no-store", "content-type": "application/json; charset=utf-8"
      });
      return;
    }
    await send(res, 204, "", { "cache-control": "no-store", ...(auth.isPublic ? {} : { "set-cookie": auth.logout(authorization) }) });
    return;
  }
  if (urlPath === "/api/v1/auth/local-session" && req.method === "POST") {
    if (!auth.sameOrigin(req.headers)) {
      await send(res, 403, JSON.stringify({ error: "request_not_allowed" }), {
        "cache-control": "no-store", "content-type": "application/json; charset=utf-8"
      });
      return;
    }
    await send(res, 204, "", { "cache-control": "no-store" });
    return;
  }
  if (isApiPath(urlPath)) {
    if (isUnsafeMethod(req.method) && !auth.validateCsrf(req.headers, authorization)) {
      await send(res, 403, JSON.stringify({ error: "csrf_validation_failed" }), {
        "cache-control": "no-store", "content-type": "application/json; charset=utf-8"
      });
      return;
    }
    await proxyToBackend(req, res, requestUrl);
    return;
  }

  if (auth.isPublic && urlPath === "/release.json" && ["GET", "HEAD"].includes(req.method ?? "GET")) {
    try {
      const release = JSON.parse(await fs.readFile(path.join(distDir, "release.json"), "utf8"));
      await send(res, 200, req.method === "HEAD" ? "" : JSON.stringify(publicReleaseIdentity(release)), {
        "cache-control": "no-store", "content-type": "application/json; charset=utf-8"
      });
    } catch {
      await send(res, 503, JSON.stringify({ error: "release_identity_unavailable" }), {
        "cache-control": "no-store", "content-type": "application/json; charset=utf-8"
      });
    }
    return;
  }

  const filePath = resolveAssetPath(urlPath === "/" ? "/index.html" : urlPath);
  if (!filePath) {
    await send(res, 403, "Forbidden", { "content-type": "text/plain; charset=utf-8" });
    return;
  }

  // Vite emits content-hashed /assets/ file names, so they are safe to cache
  // immutably; index.html must stay no-store to pick up each release switch.
  const requestAcceptEncoding = String(req.headers["accept-encoding"] ?? "");
  try {
    const stat = await fs.stat(filePath);
    if (!stat.isFile()) {
      throw new Error("not a file");
    }
    const ext = path.extname(filePath);
    const body = await fs.readFile(filePath);
    const immutableAsset = urlPath.startsWith("/assets/");
    await send(res, 200, body, {
      "content-type": mimeTypes.get(ext) ?? "application/octet-stream",
      "cache-control": immutableAsset
        ? "public, max-age=31536000, immutable"
        : "private, no-store"
    }, requestAcceptEncoding);
  } catch {
    if (urlPath.startsWith("/assets/")) {
      const retained = await previousReleaseAsset(urlPath);
      await send(res, retained ? 200 : 404, retained?.body ?? "Asset not found", {
        "content-type": retained ? (mimeTypes.get(retained.ext) ?? "application/octet-stream") : "text/plain; charset=utf-8",
        "cache-control": retained ? "public, max-age=31536000, immutable" : "private, no-store"
      }, requestAcceptEncoding);
      return;
    }
    const indexPath = path.join(distDir, "index.html");
    const body = await fs.readFile(indexPath);
    await send(res, 200, body, {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "private, no-store"
    }, requestAcceptEncoding);
  }
});

if (isMainModule(process.argv[1])) {
  if (!isLoopbackBindHost(host)) {
    throw new Error("PUBLIC_FRONTEND_HOST must be a loopback address");
  }
  if (!backendToken) {
    throw new Error("PUBLIC_BACKEND_TOKEN or INTERNAL_API_TOKEN must be configured");
  }
  getPublicAuth();
  server.listen(port, host, () => {
    console.log(`local public frontend listening at http://${host}:${port}`);
    console.log(`proxying API requests to ${backendOrigin}`);
    prewarmPaths.forEach((apiPath) => {
      getCachedBackendResponse(new URL(apiPath, backendOrigin)).catch(() => {});
    });
  });
}
