import { gzip as gzipCallback } from "node:zlib";
import { promisify } from "node:util";

const gzip = promisify(gzipCallback);

export function acceptsGzip(value = "") {
  return value
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .some((item) => item === "gzip" || item.startsWith("gzip;"));
}

export async function encodeProxyBody(body, headers, acceptEncoding) {
  const source = Buffer.isBuffer(body) ? body : Buffer.from(body);
  const resultHeaders = { ...headers };
  delete resultHeaders["content-length"];

  const contentType = String(resultHeaders["content-type"] ?? "");
  const compressible =
    contentType.includes("json") ||
    contentType.startsWith("text/") ||
    contentType.includes("javascript") ||
    contentType.includes("svg");

  if (!compressible || source.length < 1_024 || !acceptsGzip(acceptEncoding)) {
    resultHeaders["content-length"] = String(source.length);
    return { body: source, headers: resultHeaders };
  }

  const compressed = await gzip(source, { level: 6 });
  resultHeaders["content-encoding"] = "gzip";
  resultHeaders.vary = mergeVary(resultHeaders.vary, "Accept-Encoding");
  resultHeaders["content-length"] = String(compressed.length);
  return { body: compressed, headers: resultHeaders };
}

export function mergeVary(existing, value) {
  const values = String(existing ?? "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  if (!values.some((item) => item.toLowerCase() === value.toLowerCase())) {
    values.push(value);
  }
  return values.join(", ");
}

export function shouldServeStale(cached, now, staleTtlMs) {
  return Boolean(
    cached &&
      Number.isFinite(cached.cachedAt) &&
      now - cached.cachedAt < staleTtlMs
  );
}

export function shouldBypassProxyCache(pathname, cacheControl = "", userAgent = "") {
  if (String(userAgent).includes("poy-dty-release-gate/")) return true;
  return [
    "/api/v1/health/live",
    "/api/v1/health/ready",
    "/api/v1/health/deep",
    // This list is the recovery path after a possibly committed POST. Serving
    // an older cached list can make a successful write look lost.
    "/api/v1/predictions/observations",
    "/api/v1/information-reports",
    "/api/v1/rag-index/status",
    // Evidence pages bind a short-lived immutable snapshot. A stale first
    // page must never outlive the backend pagination identity.
    "/api/v1/forecasts/seven-product/evidence"
  ].includes(String(pathname));
}

export async function fetchWithTimeout(fetchImpl, target, init = {}, timeoutMs = 60_000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error("upstream_timeout")), timeoutMs);
  try {
    return await fetchImpl(target, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}
