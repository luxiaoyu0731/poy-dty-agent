import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { isMainModule } from "../scripts/local-public-server.mjs";


test("main-module detection resolves a release current symlink", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "public-entrypoint-"));
  const symlink = path.join(root, "local-public-server.mjs");
  const target = path.resolve("scripts/local-public-server.mjs");
  await fs.symlink(target, symlink);
  try {
    assert.equal(isMainModule(symlink, new URL("../scripts/local-public-server.mjs", import.meta.url)), true);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});
