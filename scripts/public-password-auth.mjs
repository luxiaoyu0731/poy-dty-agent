import {
  createHash,
  randomBytes as randomBytesCallback,
  scrypt as scryptCallback,
  timingSafeEqual
} from "node:crypto";
import { promisify } from "node:util";

const scrypt = promisify(scryptCallback);

export const SESSION_COOKIE = "__Host-poy_dty_session";
export const CSRF_COOKIE = "__Host-poy_dty_csrf";
export const LOGIN_CSRF_COOKIE = "__Host-poy_dty_login_csrf";
const SCRYPT_N = 131_072;
const SCRYPT_R = 8;
const SCRYPT_P = 1;
const SCRYPT_KEY_LENGTH = 32;

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function safeEqual(left, right) {
  const leftBuffer = Buffer.from(String(left));
  const rightBuffer = Buffer.from(String(right));
  return leftBuffer.length === rightBuffer.length && timingSafeEqual(leftBuffer, rightBuffer);
}

export function parseCookies(value = "") {
  const cookies = new Map();
  for (const item of String(value).split(";")) {
    const separator = item.indexOf("=");
    if (separator < 1) continue;
    const name = item.slice(0, separator).trim();
    const cookieValue = item.slice(separator + 1).trim();
    if (name && !cookies.has(name)) cookies.set(name, cookieValue);
  }
  return cookies;
}

export function parseScryptVerifier(value) {
  const parts = String(value ?? "").split(":");
  if (parts.length !== 6 || parts[0] !== "scrypt") {
    throw new Error("PUBLIC_LOGIN_PASSWORD_HASH must use the approved scrypt format");
  }
  const n = Number(parts[1]);
  const r = Number(parts[2]);
  const p = Number(parts[3]);
  if (n !== SCRYPT_N || r !== SCRYPT_R || p !== SCRYPT_P) {
    throw new Error("PUBLIC_LOGIN_PASSWORD_HASH uses unapproved scrypt parameters");
  }
  const salt = Buffer.from(parts[4], "base64url");
  const digest = Buffer.from(parts[5], "base64url");
  if (salt.length !== 16 || digest.length !== SCRYPT_KEY_LENGTH) {
    throw new Error("PUBLIC_LOGIN_PASSWORD_HASH has an invalid salt or digest length");
  }
  return { n, r, p, salt, digest };
}

export async function createScryptVerifier(password, salt = randomBytesCallback(16)) {
  const passwordValue = String(password);
  if (passwordValue.length < 24 || new Set(passwordValue).size < 12) {
    throw new Error("public password must contain at least 24 characters with at least 12 distinct characters");
  }
  const digest = await scrypt(passwordValue, salt, SCRYPT_KEY_LENGTH, {
    N: SCRYPT_N,
    r: SCRYPT_R,
    p: SCRYPT_P,
    maxmem: 256 * 1024 * 1024
  });
  return `scrypt:${SCRYPT_N}:${SCRYPT_R}:${SCRYPT_P}:${salt.toString("base64url")}:${Buffer.from(digest).toString("base64url")}`;
}

export async function verifyScryptPassword(password, verifier) {
  const parsed = parseScryptVerifier(verifier);
  const actual = await scrypt(String(password), parsed.salt, parsed.digest.length, {
    N: parsed.n,
    r: parsed.r,
    p: parsed.p,
    maxmem: 256 * 1024 * 1024
  });
  return timingSafeEqual(Buffer.from(actual), parsed.digest);
}

function cookie(name, value, { httpOnly = true, maxAge }) {
  const attributes = [
    `${name}=${value}`,
    "Path=/",
    "Secure",
    "SameSite=Strict",
    `Max-Age=${Math.max(0, Math.floor(maxAge))}`
  ];
  if (httpOnly) attributes.push("HttpOnly");
  return attributes.join("; ");
}

function normalizeOrigin(value) {
  const origin = new URL(String(value));
  if (origin.protocol !== "https:" || origin.username || origin.password || origin.pathname !== "/" || origin.search || origin.hash) {
    throw new Error("PUBLIC_CANONICAL_ORIGIN must be an HTTPS origin without a path");
  }
  return origin.origin;
}

function sameOriginRequest(headers, canonicalOrigin) {
  return String(headers.origin ?? "") === canonicalOrigin &&
    String(headers["sec-fetch-site"] ?? "") === "same-origin";
}

export function createPublicAccess(options) {
  if (options.mode !== "public") return createPublicPasswordAuth(options);
  const canonicalOrigin = normalizeOrigin(options.canonicalOrigin);
  const sameOrigin = (headers) => sameOriginRequest(headers, canonicalOrigin);
  return { isPublic: true, canonicalOrigin, sameOrigin, validateCsrf: sameOrigin };
}

export function createPublicPasswordAuth(options, dependencies = {}) {
  if (options.mode !== "single_user_password") {
    throw new Error("PUBLIC_AUTH_MODE must be single_user_password");
  }
  const canonicalOrigin = normalizeOrigin(options.canonicalOrigin);
  parseScryptVerifier(options.passwordVerifier);
  const absoluteTtlSeconds = Number(options.absoluteTtlSeconds ?? 43_200);
  const idleTtlSeconds = Number(options.idleTtlSeconds ?? 3_600);
  const loginWindowSeconds = Number(options.loginWindowSeconds ?? 900);
  const perSourceLimit = Number(options.perSourceLimit ?? 5);
  const globalLimit = Number(options.globalLimit ?? 20);
  const maxConcurrentVerifications = Number(options.maxConcurrentVerifications ?? 2);
  const challengeWindowSeconds = Number(options.challengeWindowSeconds ?? 60);
  const perSourceChallengeLimit = Number(options.perSourceChallengeLimit ?? 30);
  const globalChallengeLimit = Number(options.globalChallengeLimit ?? 200);
  for (const [name, value] of Object.entries({
    absoluteTtlSeconds,
    idleTtlSeconds,
    loginWindowSeconds,
    perSourceLimit,
    globalLimit,
    maxConcurrentVerifications,
    challengeWindowSeconds,
    perSourceChallengeLimit,
    globalChallengeLimit
  })) {
    if (!Number.isInteger(value) || value <= 0) throw new Error(`${name} must be a positive integer`);
  }
  if (idleTtlSeconds > absoluteTtlSeconds) throw new Error("idle TTL cannot exceed absolute TTL");

  const now = dependencies.now ?? (() => Date.now());
  const randomBytes = dependencies.randomBytes ?? randomBytesCallback;
  const verifyPassword = dependencies.verifyPassword ?? verifyScryptPassword;
  const sleep = dependencies.sleep ?? ((milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)));
  const sessions = new Map();
  const loginChallenges = new Map();
  const sourceAttempts = new Map();
  const sourceChallengeIssues = new Map();
  let globalAttempts = [];
  let globalChallengeIssues = [];
  let inFlightVerifications = 0;

  function token() {
    return randomBytes(32).toString("base64url");
  }

  function sameOrigin(headers) {
    return sameOriginRequest(headers, canonicalOrigin);
  }

  function pruneTimestampMap(target, cutoff) {
    for (const [source, timestamps] of target) {
      const fresh = timestamps.filter((timestamp) => timestamp > cutoff);
      if (fresh.length) target.set(source, fresh);
      else target.delete(source);
    }
  }

  function pruneAttempts(current) {
    const cutoff = current - loginWindowSeconds * 1_000;
    globalAttempts = globalAttempts.filter((timestamp) => timestamp > cutoff);
    pruneTimestampMap(sourceAttempts, cutoff);
  }

  function loginRateState(source) {
    const current = now();
    pruneAttempts(current);
    const attempts = sourceAttempts.get(source) ?? [];
    const limited = attempts.length >= perSourceLimit || globalAttempts.length >= globalLimit;
    const retryAfter = limited
      ? Math.max(1, Math.ceil((Math.min(...[...attempts, ...globalAttempts]) + loginWindowSeconds * 1_000 - current) / 1_000))
      : 0;
    return { attempts: attempts.length, limited, retryAfter };
  }

  function reserveLoginAttempt(source) {
    const current = now();
    const attempts = sourceAttempts.get(source) ?? [];
    attempts.push(current);
    sourceAttempts.set(source, attempts);
    globalAttempts.push(current);
    return attempts.length;
  }

  function issueLoginChallenge(source = "unknown") {
    const current = now();
    for (const [key, expiresAt] of loginChallenges) {
      if (expiresAt < current) loginChallenges.delete(key);
    }
    const challengeCutoff = current - challengeWindowSeconds * 1_000;
    globalChallengeIssues = globalChallengeIssues.filter((timestamp) => timestamp > challengeCutoff);
    pruneTimestampMap(sourceChallengeIssues, challengeCutoff);
    const sourceIssues = sourceChallengeIssues.get(source) ?? [];
    const limited = loginChallenges.size >= 2_048 ||
      sourceIssues.length >= perSourceChallengeLimit ||
      globalChallengeIssues.length >= globalChallengeLimit;
    if (limited) {
      const timestamps = [...sourceIssues, ...globalChallengeIssues];
      const retryAfter = timestamps.length
        ? Math.max(1, Math.ceil((Math.min(...timestamps) + challengeWindowSeconds * 1_000 - current) / 1_000))
        : challengeWindowSeconds;
      return { limited: true, retryAfter };
    }
    sourceIssues.push(current);
    sourceChallengeIssues.set(source, sourceIssues);
    globalChallengeIssues.push(current);
    const raw = token();
    const maxAge = 600;
    loginChallenges.set(sha256(raw), now() + maxAge * 1_000);
    return {
      token: raw,
      setCookie: cookie(LOGIN_CSRF_COOKIE, raw, { maxAge }),
      limited: false
    };
  }

  function consumeLoginChallenge(cookieHeader, suppliedToken) {
    const cookieToken = parseCookies(cookieHeader).get(LOGIN_CSRF_COOKIE) ?? "";
    const key = sha256(cookieToken);
    const expiresAt = loginChallenges.get(key) ?? 0;
    loginChallenges.delete(key);
    return Boolean(cookieToken && suppliedToken && safeEqual(cookieToken, suppliedToken) && expiresAt >= now());
  }

  function createSession() {
    const rawSession = token();
    const csrf = token();
    const current = now();
    for (const [key, session] of sessions) {
      if (current - session.createdAt > absoluteTtlSeconds * 1_000 ||
          current - session.lastSeenAt > idleTtlSeconds * 1_000) {
        sessions.delete(key);
      }
    }
    while (sessions.size >= 128) sessions.delete(sessions.keys().next().value);
    sessions.set(sha256(rawSession), { csrf, createdAt: current, lastSeenAt: current });
    return {
      csrf,
      cookies: [
        cookie(SESSION_COOKIE, rawSession, { maxAge: absoluteTtlSeconds }),
        cookie(CSRF_COOKIE, csrf, { httpOnly: false, maxAge: absoluteTtlSeconds }),
        cookie(LOGIN_CSRF_COOKIE, "", { maxAge: 0 })
      ]
    };
  }

  function authorize(cookieHeader) {
    const raw = parseCookies(cookieHeader).get(SESSION_COOKIE) ?? "";
    if (!raw) return { authorized: false };
    const key = sha256(raw);
    const session = sessions.get(key);
    if (!session) return { authorized: false };
    const current = now();
    const absoluteExpired = current - session.createdAt > absoluteTtlSeconds * 1_000;
    const idleExpired = current - session.lastSeenAt > idleTtlSeconds * 1_000;
    if (absoluteExpired || idleExpired) {
      sessions.delete(key);
      return { authorized: false };
    }
    session.lastSeenAt = current;
    return { authorized: true, key, csrf: session.csrf };
  }

  function validateCsrf(headers, authorization) {
    const supplied = String(headers["x-csrf-token"] ?? "");
    const csrfCookie = parseCookies(headers.cookie).get(CSRF_COOKIE) ?? "";
    return Boolean(
      authorization.authorized &&
      sameOrigin(headers) &&
      supplied &&
      csrfCookie &&
      safeEqual(supplied, authorization.csrf) &&
      safeEqual(csrfCookie, authorization.csrf)
    );
  }

  async function login({ password, csrfToken, cookieHeader, headers, source = "unknown" }) {
    if (!sameOrigin(headers)) return { ok: false, status: 403, error: "request_not_allowed" };
    if (!consumeLoginChallenge(cookieHeader, csrfToken)) {
      return { ok: false, status: 403, error: "request_not_allowed" };
    }
    const rate = loginRateState(source);
    if (rate.limited) return { ok: false, status: 429, error: "login_rate_limited", retryAfter: rate.retryAfter };
    if (inFlightVerifications >= maxConcurrentVerifications) {
      return { ok: false, status: 429, error: "login_rate_limited", retryAfter: 1 };
    }
    const attemptNumber = reserveLoginAttempt(source);
    inFlightVerifications += 1;
    let valid;
    try {
      valid = await verifyPassword(String(password ?? ""), options.passwordVerifier);
    } finally {
      inFlightVerifications -= 1;
    }
    if (!valid) {
      await sleep(Math.min(2_000, 100 * 2 ** Math.min(attemptNumber, 4)));
      return { ok: false, status: 401, error: "invalid_credentials" };
    }
    const existing = authorize(cookieHeader);
    if (existing.authorized) sessions.delete(existing.key);
    return { ok: true, status: 303, ...createSession() };
  }

  function logout(authorization) {
    if (authorization.key) sessions.delete(authorization.key);
    return [
      cookie(SESSION_COOKIE, "", { maxAge: 0 }),
      cookie(CSRF_COOKIE, "", { httpOnly: false, maxAge: 0 })
    ];
  }

  return {
    canonicalOrigin,
    authorize,
    issueLoginChallenge,
    login,
    logout,
    sameOrigin,
    validateCsrf,
    sessionCount: () => sessions.size
  };
}
