import assert from "node:assert/strict";
import test from "node:test";

import {
  CSRF_COOKIE,
  LOGIN_CSRF_COOKIE,
  SESSION_COOKIE,
  createPublicPasswordAuth,
  createScryptVerifier,
  parseScryptVerifier,
  verifyScryptPassword
} from "../scripts/public-password-auth.mjs";

const password = "correct horse battery staple";
const origin = "https://app.example.test";

async function fixture(overrides = {}, dependencyOverrides = {}) {
  let current = 1_700_000_000_000;
  let counter = 0;
  const verifier = await createScryptVerifier(password, Buffer.alloc(16, 7));
  const auth = createPublicPasswordAuth({
    mode: "single_user_password",
    canonicalOrigin: origin,
    passwordVerifier: verifier,
    absoluteTtlSeconds: 120,
    idleTtlSeconds: 60,
    loginWindowSeconds: 900,
    perSourceLimit: 5,
    globalLimit: 20,
    ...overrides
  }, {
    now: () => current,
    randomBytes: (size) => Buffer.alloc(size, ++counter),
    sleep: async () => {},
    ...dependencyOverrides
  });
  return { auth, advance: (milliseconds) => { current += milliseconds; } };
}

function cookieValue(setCookie, name) {
  const row = setCookie.find((value) => value.startsWith(`${name}=`));
  return row?.split(";", 1)[0].slice(name.length + 1) ?? "";
}

function sameOriginHeaders(cookie = "", csrf = "") {
  return {
    origin,
    "sec-fetch-site": "same-origin",
    cookie,
    ...(csrf ? { "x-csrf-token": csrf } : {})
  };
}

async function login(auth, suppliedPassword = password, source = "127.0.0.1") {
  const challenge = auth.issueLoginChallenge(source);
  return auth.login({
    password: suppliedPassword,
    csrfToken: challenge.token,
    cookieHeader: `${LOGIN_CSRF_COOKIE}=${challenge.token}`,
    headers: sameOriginHeaders(),
    source
  });
}

test("scrypt verifier uses frozen parameters and rejects wrong passwords", async () => {
  const verifier = await createScryptVerifier(password, Buffer.alloc(16, 3));
  assert.equal(parseScryptVerifier(verifier).digest.length, 32);
  assert.equal(await verifyScryptPassword(password, verifier), true);
  assert.equal(await verifyScryptPassword("incorrect password value", verifier), false);
  assert.throws(() => parseScryptVerifier("scrypt:2:8:1:bad:bad"), /unapproved/);
});

test("production configuration fails closed", async () => {
  const verifier = await createScryptVerifier(password, Buffer.alloc(16, 4));
  assert.throws(() => createPublicPasswordAuth({
    mode: "cloudflare_access_single_user",
    canonicalOrigin: origin,
    passwordVerifier: verifier
  }), /PUBLIC_AUTH_MODE/);
  assert.throws(() => createPublicPasswordAuth({
    mode: "single_user_password",
    canonicalOrigin: "http://app.example.test",
    passwordVerifier: verifier
  }), /HTTPS origin/);
});

test("login requires same-origin pre-auth CSRF and returns hardened cookies", async () => {
  const { auth } = await fixture();
  const challenge = auth.issueLoginChallenge();
  const denied = await auth.login({
    password,
    csrfToken: challenge.token,
    cookieHeader: `${LOGIN_CSRF_COOKIE}=${challenge.token}`,
    headers: { origin: "https://evil.test", "sec-fetch-site": "cross-site" },
    source: "127.0.0.1"
  });
  assert.equal(denied.status, 403);

  for (const rejectedOrigin of ["null", ""]) {
    const rejected = await auth.login({ password, csrfToken: challenge.token,
      cookieHeader: `${LOGIN_CSRF_COOKIE}=${challenge.token}`,
      headers: { origin: rejectedOrigin, "sec-fetch-site": "same-origin" } });
    assert.equal(rejected.status, 403);
  }

  const result = await login(auth);
  assert.equal(result.ok, true);
  for (const row of result.cookies) {
    assert.match(row, /Path=\//);
    assert.match(row, /Secure/);
    assert.match(row, /SameSite=Strict/);
    assert.doesNotMatch(row, /Domain=/);
  }
  assert.match(result.cookies.find((row) => row.startsWith(`${SESSION_COOKIE}=`)), /HttpOnly/);
  assert.doesNotMatch(result.cookies.find((row) => row.startsWith(`${CSRF_COOKIE}=`)), /HttpOnly/);
});

test("opaque sessions enforce expiry and restart invalidation", async () => {
  const { auth, advance } = await fixture();
  const result = await login(auth);
  const session = cookieValue(result.cookies, SESSION_COOKIE);
  assert.equal(auth.authorize(`${SESSION_COOKIE}=${session}`).authorized, true);
  advance(61_000);
  assert.equal(auth.authorize(`${SESSION_COOKIE}=${session}`).authorized, false);
  const restarted = (await fixture()).auth;
  assert.equal(restarted.authorize(`${SESSION_COOKIE}=${session}`).authorized, false);
});

test("absolute expiry is independent of idle refreshes", async () => {
  const { auth, advance } = await fixture({ absoluteTtlSeconds: 120, idleTtlSeconds: 90 });
  const result = await login(auth);
  const session = cookieValue(result.cookies, SESSION_COOKIE);
  advance(70_000);
  assert.equal(auth.authorize(`${SESSION_COOKIE}=${session}`).authorized, true);
  advance(51_000);
  assert.equal(auth.authorize(`${SESSION_COOKIE}=${session}`).authorized, false);
});

test("unsafe requests require session-bound CSRF and logout revokes", async () => {
  const { auth } = await fixture();
  const result = await login(auth);
  const session = cookieValue(result.cookies, SESSION_COOKIE);
  const csrf = cookieValue(result.cookies, CSRF_COOKIE);
  const cookie = `${SESSION_COOKIE}=${session}; ${CSRF_COOKIE}=${csrf}`;
  const authorization = auth.authorize(cookie);
  assert.equal(auth.validateCsrf(sameOriginHeaders(cookie, csrf), authorization), true);
  assert.equal(auth.validateCsrf(sameOriginHeaders(cookie, "wrong"), authorization), false);
  assert.equal(auth.validateCsrf({ ...sameOriginHeaders(cookie, csrf), origin: "https://evil.test" }, authorization), false);
  auth.logout(authorization);
  assert.equal(auth.authorize(cookie).authorized, false);
});

test("login failures are uniform and trigger source rate limiting", async () => {
  const { auth } = await fixture({ perSourceLimit: 2 });
  const first = await login(auth, "wrong password value");
  const second = await login(auth, "another wrong value");
  const third = await login(auth, password);
  assert.deepEqual([first.status, first.error], [401, "invalid_credentials"]);
  assert.deepEqual([second.status, second.error], [401, "invalid_credentials"]);
  assert.deepEqual([third.status, third.error], [429, "login_rate_limited"]);
  assert.ok(third.retryAfter > 0);
});

test("global login limit applies across independent sources", async () => {
  const { auth } = await fixture({ perSourceLimit: 5, globalLimit: 2 });
  assert.equal((await login(auth, "wrong password value one", "one")).status, 401);
  assert.equal((await login(auth, "wrong password value two", "two")).status, 401);
  assert.equal((await login(auth, password, "three")).status, 429);
});

test("successful reauthentication rotates the existing session", async () => {
  const { auth } = await fixture();
  const first = await login(auth);
  const firstSession = cookieValue(first.cookies, SESSION_COOKIE);
  const challenge = auth.issueLoginChallenge("one");
  const second = await auth.login({
    password,
    csrfToken: challenge.token,
    cookieHeader: `${SESSION_COOKIE}=${firstSession}; ${LOGIN_CSRF_COOKIE}=${challenge.token}`,
    headers: sameOriginHeaders(),
    source: "one"
  });
  assert.equal(second.ok, true);
  assert.equal(auth.authorize(`${SESSION_COOKIE}=${firstSession}`).authorized, false);
  assert.notEqual(cookieValue(second.cookies, SESSION_COOKIE), firstSession);
});

test("concurrent login verification is bounded before entering scrypt", async () => {
  const releases = [];
  let entered = 0;
  const verifyPassword = () => new Promise((resolve) => {
    entered += 1;
    releases.push(resolve);
  });
  const { auth } = await fixture({ maxConcurrentVerifications: 2 }, { verifyPassword });
  const challengeOne = auth.issueLoginChallenge("one");
  const challengeTwo = auth.issueLoginChallenge("two");
  const challengeThree = auth.issueLoginChallenge("three");
  const headers = sameOriginHeaders();
  const first = auth.login({
    password, csrfToken: challengeOne.token,
    cookieHeader: `${LOGIN_CSRF_COOKIE}=${challengeOne.token}`, headers, source: "one"
  });
  const second = auth.login({
    password, csrfToken: challengeTwo.token,
    cookieHeader: `${LOGIN_CSRF_COOKIE}=${challengeTwo.token}`, headers, source: "two"
  });
  const third = await auth.login({
    password, csrfToken: challengeThree.token,
    cookieHeader: `${LOGIN_CSRF_COOKIE}=${challengeThree.token}`, headers, source: "three"
  });
  assert.equal(entered, 2);
  assert.deepEqual([third.status, third.error], [429, "login_rate_limited"]);
  releases.forEach((resolve) => resolve(false));
  await Promise.all([first, second]);
});

test("anonymous login challenge issuance is bounded", async () => {
  const { auth } = await fixture({ perSourceChallengeLimit: 2, globalChallengeLimit: 3 });
  assert.equal(auth.issueLoginChallenge("one").limited, false);
  assert.equal(auth.issueLoginChallenge("one").limited, false);
  assert.equal(auth.issueLoginChallenge("one").limited, true);
  assert.equal(auth.issueLoginChallenge("two").limited, false);
  assert.equal(auth.issueLoginChallenge("three").limited, true);
});

test("login challenges are one-time and expire", async () => {
  const { auth, advance } = await fixture();
  const challenge = auth.issueLoginChallenge("one");
  const request = {
    password: "wrong but sufficiently shaped password",
    csrfToken: challenge.token,
    cookieHeader: `${LOGIN_CSRF_COOKIE}=${challenge.token}`,
    headers: sameOriginHeaders(),
    source: "one"
  };
  assert.equal((await auth.login(request)).status, 401);
  assert.equal((await auth.login(request)).status, 403);
  const expired = auth.issueLoginChallenge("two");
  advance(601_000);
  assert.equal((await auth.login({ ...request, csrfToken: expired.token, cookieHeader: `${LOGIN_CSRF_COOKIE}=${expired.token}`, source: "two" })).status, 403);
});
