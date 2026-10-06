import { defineConfig, devices } from "@playwright/test";

function testPort(value: string | undefined, fallback: number): number {
  const parsed = Number(value ?? fallback);
  if (!Number.isInteger(parsed) || parsed < 1024 || parsed > 65535) {
    throw new Error("Playwright test ports must be integers between 1024 and 65535");
  }
  return parsed;
}

const apiPort = testPort(process.env.PLAYWRIGHT_API_PORT, 18000);
const webPort = testPort(process.env.PLAYWRIGHT_WEB_PORT, 15173);
const host = process.env.PLAYWRIGHT_HOST ?? "127.0.0.1";
if (host !== "127.0.0.1") {
  throw new Error("PLAYWRIGHT_HOST must be 127.0.0.1");
}
const apiBaseUrl = process.env.PLAYWRIGHT_API_BASE_URL ?? `http://${host}:${apiPort}`;
const webBaseUrl = process.env.PLAYWRIGHT_BASE_URL ?? `http://${host}:${webPort}`;
const reuseExistingServer = process.env.PLAYWRIGHT_REUSE_EXISTING === "true";

export default defineConfig({
  testDir: "./tests",
  timeout: 60_000,
  workers: Number(process.env.PLAYWRIGHT_WORKERS ?? "1"),
  // 重试仅吸收共享 e2e 服务器下的时序抖动（如账本网格重挂、双面板 strict
  // 冲突）；真实回归在两次运行中都失败，仍会失败。
  retries: Number(process.env.PLAYWRIGHT_RETRIES ?? "2"),
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: webBaseUrl,
    screenshot: "only-on-failure",
    trace: "on-first-retry"
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } }
  ],
  webServer: [
    {
      command: `bash scripts/run-e2e-backend.sh ${host} ${apiPort}`,
      port: apiPort,
      reuseExistingServer,
      timeout: 120_000
    },
    {
      command: `VITE_PROXY_TARGET=${apiBaseUrl} npm run ${process.env.PLAYWRIGHT_PREVIEW === "true" ? "preview" : "dev"} -- --host ${host} --port ${webPort}`,
      port: webPort,
      reuseExistingServer,
      timeout: 120_000
    }
  ]
});
