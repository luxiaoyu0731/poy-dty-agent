#!/usr/bin/env node
// Interactively generate PUBLIC_LOGIN_PASSWORD_HASH for the public proxy.
// Usage: node scripts/generate_public_login_hash.mjs
// The password is read once from stdin without echo safety at the Node layer;
// run this in a local terminal and paste the printed verifier into the
// runtime env file (never commit it).
import { createScryptVerifier } from "./public-password-auth.mjs";
import { readFileSync } from "node:fs";
import readline from "node:readline/promises";
import { stdin, stdout } from "node:process";

let password;
if (process.argv.includes("--stdin")) {
  password = readFileSync(0, "utf8").replace(/\r?\n$/, "");
} else {
  const rl = readline.createInterface({ input: stdin, output: stdout });
  password = await rl.question(
    "New public password (min 24 chars, at least 12 distinct characters): ",
  );
  rl.close();
}
try {
  process.stdout.write(`PUBLIC_LOGIN_PASSWORD_HASH=${await createScryptVerifier(password)}\n`);
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
