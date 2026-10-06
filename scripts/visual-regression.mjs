import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import { createConnection } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import pixelmatch from "pixelmatch";
import { chromium } from "playwright";
import { PNG } from "pngjs";

const rootDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const host = process.env.PLAYWRIGHT_HOST ?? "127.0.0.1";
const apiPort = process.env.PLAYWRIGHT_API_PORT ?? "18001";
const webPort = process.env.PLAYWRIGHT_WEB_PORT ?? "15174";
const apiBaseUrl = process.env.PLAYWRIGHT_API_BASE_URL ?? `http://${host}:${apiPort}`;
const visualRuntimeDir = mkdtempSync(path.join(tmpdir(), "poy-dty-visual-"));

function readArg(name, fallback = undefined) {
  const prefix = `--${name}=`;
  const value = args.find((item) => item.startsWith(prefix));
  return value ? value.slice(prefix.length) : fallback;
}

function flag(name) {
  return args.includes(`--${name}`);
}

function pct(value) {
  return `${value.toFixed(2)}%`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function resolveProjectPath(value) {
  return path.resolve(rootDir, value);
}

function downsamplePng(source, factor) {
  if (factor <= 1) return source;

  const width = Math.floor(source.width / factor);
  const height = Math.floor(source.height / factor);
  const output = new PNG({ width, height });

  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      let r = 0;
      let g = 0;
      let b = 0;
      let a = 0;
      let count = 0;

      for (let yy = 0; yy < factor; yy += 1) {
        for (let xx = 0; xx < factor; xx += 1) {
          const sx = x * factor + xx;
          const sy = y * factor + yy;
          const sourceIndex = (sy * source.width + sx) * 4;
          r += source.data[sourceIndex];
          g += source.data[sourceIndex + 1];
          b += source.data[sourceIndex + 2];
          a += source.data[sourceIndex + 3];
          count += 1;
        }
      }

      const outputIndex = (y * width + x) * 4;
      output.data[outputIndex] = Math.round(r / count);
      output.data[outputIndex + 1] = Math.round(g / count);
      output.data[outputIndex + 2] = Math.round(b / count);
      output.data[outputIndex + 3] = Math.round(a / count);
    }
  }

  return output;
}

async function loadBaselines() {
  const file = resolveProjectPath("design/visual-baselines.json");
  return JSON.parse(await readFile(file, "utf8"));
}

async function waitForServer(url, timeoutMs = 15000) {
  const target = new URL(url);
  const port = Number(target.port || (target.protocol === "https:" ? 443 : 80));
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    if (await canConnect(target.hostname, port)) return true;
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  return false;
}

function canConnect(host, port) {
  return new Promise((resolve) => {
    const socket = createConnection({ host, port });
    const done = (ok) => {
      socket.removeAllListeners();
      socket.destroy();
      resolve(ok);
    };
    socket.setTimeout(700);
    socket.once("connect", () => done(true));
    socket.once("timeout", () => done(false));
    socket.once("error", () => done(false));
  });
}

async function ensureProcessServer({ name, url, command, args: commandArgs, cwd, env = {}, timeoutMs = 30000 }) {
  if (await waitForServer(url, 1500)) return null;

  const server = spawn(command, commandArgs, {
    cwd,
    env: { ...process.env, ...env, BROWSER: "none" },
    stdio: "ignore",
    detached: process.platform !== "win32"
  });

  const ready = await waitForServer(url, timeoutMs);
  if (!ready) {
    server.kill();
    throw new Error(`${name} server did not respond at ${url}`);
  }

  return server;
}

async function ensureBackendServer() {
  const command = process.platform === "win32" ? "uv.cmd" : "uv";
  return ensureProcessServer({
    name: "FastAPI",
    url: `${apiBaseUrl}/api/v1/health/live`,
    command,
    args: ["run", "uvicorn", "app.main:app", "--host", host, "--port", apiPort],
    cwd: path.join(rootDir, "server"),
    env: {
      SQLITE_PATH: path.join(visualRuntimeDir, "visual.db"),
      INTRADAY_PRICE_SCHEDULER_ENABLED: "0"
    },
    timeoutMs: 60000
  });
}

async function ensureFrontendServer(baseUrl) {
  const viteBin = path.join(rootDir, "node_modules", "vite", "bin", "vite.js");
  const command = existsSync(viteBin) ? process.execPath : process.platform === "win32" ? "npm.cmd" : "npm";
  const commandArgs = existsSync(viteBin)
    ? [viteBin, "--host", host, "--port", webPort]
    : ["run", "dev", "--", "--host", host, "--port", webPort];

  return ensureProcessServer({
    name: "Vite",
    url: baseUrl,
    command,
    args: commandArgs,
    cwd: rootDir,
    env: { ...process.env, VITE_PROXY_TARGET: apiBaseUrl },
    timeoutMs: 30000
  });
}

function pixelDiffPercent(reference, actual, threshold, diffPath) {
  const diff = new PNG({ width: reference.width, height: reference.height });
  const mismatchPixels = pixelmatch(reference.data, actual.data, diff.data, reference.width, reference.height, {
    threshold,
    includeAA: false,
    alpha: 0.45,
    diffColor: [255, 62, 58],
    diffColorAlt: [46, 136, 255]
  });

  if (diffPath) writeFileSync(diffPath, PNG.sync.write(diff));

  return {
    mismatchPixels,
    diffPercent: (mismatchPixels / (reference.width * reference.height)) * 100
  };
}

function comparePngs(referencePath, actualPath, diffPath, rawDiffPath, options) {
  const reference = PNG.sync.read(readFileSync(referencePath));
  const actual = PNG.sync.read(readFileSync(actualPath));

  if (reference.width !== actual.width || reference.height !== actual.height) {
    return {
      ok: false,
      dimensionsMatch: false,
      mismatchPixels: null,
      diffPercent: 100,
      rawMismatchPixels: null,
      rawDiffPercent: 100,
      referenceSize: `${reference.width}x${reference.height}`,
      actualSize: `${actual.width}x${actual.height}`
    };
  }

  const raw = pixelDiffPercent(reference, actual, options.rawPixelThreshold, rawDiffPath);
  const scaledReference = downsamplePng(reference, options.analysisScale);
  const scaledActual = downsamplePng(actual, options.analysisScale);
  const perceptual = pixelDiffPercent(scaledReference, scaledActual, options.pixelThreshold, diffPath);

  return {
    ok: true,
    dimensionsMatch: true,
    mismatchPixels: perceptual.mismatchPixels,
    diffPercent: perceptual.diffPercent,
    rawMismatchPixels: raw.mismatchPixels,
    rawDiffPercent: raw.diffPercent,
    referenceSize: `${reference.width}x${reference.height}`,
    actualSize: `${actual.width}x${actual.height}`,
    metricSize: `${scaledReference.width}x${scaledReference.height}`
  };
}

async function capturePage(page, entry, baseUrl, actualPath) {
  await page.setViewportSize(entry.viewport);
  await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
  await page.locator(".delivery-workbench").waitFor({ state: "visible" });
  // The overview no longer renders a page heading or the retired status strip.
  // Wait for its actual decision and risk sections; do not alter reference pixels.
  await page.locator(".overview-decision").waitFor({ state: "visible", timeout: 60000 });
  await page.locator(".overview-risk").waitFor({ state: "visible", timeout: 60000 });
  await page.locator('#main-content[aria-busy="false"]').waitFor({ state: "visible", timeout: 60000 });
  if (entry.device === "phone") {
    await page.locator("html.mobile-desktop-view").waitFor({ state: "attached" });
    const frame = await page.locator(".delivery-workbench").boundingBox();
    if (!frame || Math.abs(frame.width - 1440) > 1) {
      throw new Error("Phone visual capture must retain the approved 1440px desktop frame.");
    }
  }
  await page.locator(".ant-notification-notice").waitFor({ state: "hidden", timeout: 15000 }).catch(() => {});
  await page.evaluate(async () => {
    if ("fonts" in document) await document.fonts.ready;
  });
  await page.waitForTimeout(350);
  await page.screenshot({
    path: actualPath,
    fullPage: entry.device !== "phone",
    animations: "disabled",
    caret: "hide",
    scale: "css"
  });
}

function reportHtml(results, outputDir) {
  const rows = results.map((result) => {
    const status = result.pass ? "PASS" : "REVIEW";
    const actual = path.relative(outputDir, result.actualPath);
    const diff = path.relative(outputDir, result.diffPath);
    const rawDiff = path.relative(outputDir, result.rawDiffPath);
    const reference = path.relative(outputDir, result.referencePath);
    return `<tr>
      <td>${escapeHtml(result.id)}</td>
      <td>${escapeHtml(result.title)}</td>
      <td>${escapeHtml(result.viewport)}</td>
      <td class="${result.pass ? "pass" : "review"}">${status}</td>
      <td>${pct(result.diffPercent)}</td>
      <td>${pct(result.rawDiffPercent)}</td>
      <td>${escapeHtml(result.mismatchPixels ?? "dimension mismatch")}</td>
      <td><a href="${escapeHtml(actual)}">actual</a></td>
      <td><a href="${escapeHtml(diff)}">diff</a></td>
      <td><a href="${escapeHtml(rawDiff)}">raw diff</a></td>
      <td><a href="${escapeHtml(reference)}">reference</a></td>
    </tr>`;
  }).join("\n");

  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>POY/DTY Agent Visual Regression</title>
  <style>
    body { margin: 0; background: #07111d; color: #e9f2fb; font-family: Inter, "PingFang SC", sans-serif; }
    main { padding: 28px; }
    h1 { margin: 0 0 8px; font-size: 26px; }
    p { color: #9fb0c1; }
    table { width: 100%; border-collapse: collapse; margin-top: 22px; background: rgba(8,24,39,.92); }
    th, td { border-bottom: 1px solid rgba(75,132,183,.28); padding: 12px 14px; text-align: left; font-size: 13px; }
    th { color: #9fb0c1; font-weight: 700; }
    a { color: #78b9ff; }
    .pass { color: #28d56e; font-weight: 800; }
    .review { color: #ffb21a; font-weight: 800; }
  </style>
</head>
<body>
  <main>
    <h1>POY/DTY Agent Visual Regression</h1>
    <p>Generated from local golden screenshots because no original Figma source is available.</p>
    <table>
      <thead>
        <tr><th>ID</th><th>页面</th><th>视口</th><th>状态</th><th>感知差异</th><th>原始像素差</th><th>差异像素</th><th>当前图</th><th>感知差异图</th><th>原始差异图</th><th>参考图</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  </main>
</body>
</html>`;
}

function reportMarkdown(results, generatedAt) {
  const lines = [
    "# Visual Regression Report",
    "",
    `Generated at: ${generatedAt}`,
    "",
    "| ID | 页面 | 视口 | 状态 | 感知差异 | 原始像素差 | 差异像素 | 当前图 | 感知差异图 | 原始差异图 | 参考图 |",
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
  ];

  for (const result of results) {
    lines.push([
      result.id,
      result.title,
      result.viewport,
      result.pass ? "PASS" : "REVIEW",
      pct(result.diffPercent),
      pct(result.rawDiffPercent),
      result.mismatchPixels ?? "dimension mismatch",
      path.relative(result.outputDir, result.actualPath),
      path.relative(result.outputDir, result.diffPath),
      path.relative(result.outputDir, result.rawDiffPath),
      path.relative(result.outputDir, result.referencePath)
    ].join(" | ").replace(/^/, "| ").replace(/$/," |"));
  }

  lines.push("");
  lines.push("Thresholds are configured in `design/visual-baselines.json`. The report compares the real React UI against the saved visual references.");
  return lines.join("\n");
}

async function main() {
  const baselineConfig = await loadBaselines();
  const selectedPage = readArg("page");
  const baseUrl = readArg("url", process.env.PLAYWRIGHT_BASE_URL ?? `http://${host}:${webPort}`);
  const ci = flag("ci");
  const updateMobileBaseline = flag("update-mobile-baseline");
  if (ci && updateMobileBaseline) {
    throw new Error("CI cannot update reference images; baseline changes require explicit review.");
  }
  const comparisonConfig = baselineConfig.comparison ?? {};
  const pixelThreshold = Number(readArg("pixel-threshold", String(comparisonConfig.pixelThreshold ?? 0.4)));
  const rawPixelThreshold = Number(readArg("raw-pixel-threshold", String(comparisonConfig.rawPixelThreshold ?? 0.12)));
  const analysisScale = Number(readArg("analysis-scale", String(comparisonConfig.analysisScale ?? 4)));
  const now = new Date().toISOString().replace(/[:.]/g, "-");
  const outputRoot = resolveProjectPath(".visual-regression");
  const outputDir = path.join(outputRoot, now);
  const latestDir = path.join(outputRoot, "latest");
  const actualDir = path.join(outputDir, "actual");
  const diffDir = path.join(outputDir, "diff");
  const rawDiffDir = path.join(outputDir, "raw-diff");

  let pages = baselineConfig.pages;
  if (selectedPage) {
    pages = pages.filter((entry) => entry.slug === selectedPage || entry.title === selectedPage || entry.id === selectedPage);
    if (pages.length === 0) throw new Error(`No baseline page matched: ${selectedPage}`);
  }

  rmSync(latestDir, { recursive: true, force: true });
  await mkdir(actualDir, { recursive: true });
  await mkdir(diffDir, { recursive: true });
  await mkdir(rawDiffDir, { recursive: true });

  const servers = [
    await ensureBackendServer(),
    await ensureFrontendServer(baseUrl)
  ].filter(Boolean);
  const browser = await chromium.launch();
  const results = [];

  try {
    for (const entry of pages) {
      const safeName = `${entry.id}-${entry.slug}`;
      const actualPath = path.join(actualDir, `${safeName}.png`);
      const diffPath = path.join(diffDir, `${safeName}.png`);
      const rawDiffPath = path.join(rawDiffDir, `${safeName}.png`);
      const referencePath = resolveProjectPath(entry.reference);

      const context = await browser.newContext({
        viewport: entry.viewport,
        colorScheme: "dark",
        deviceScaleFactor: 1,
        locale: "zh-CN",
        reducedMotion: "reduce",
        isMobile: entry.device === "phone",
        hasTouch: entry.device === "phone"
      });
      try {
        const page = await context.newPage();
        await capturePage(page, entry, baseUrl, actualPath);
      } finally {
        await context.close();
      }
      if (updateMobileBaseline && entry.device === "phone") {
        await copyFile(actualPath, referencePath);
      }
      if (!existsSync(referencePath)) throw new Error(`Missing reference image: ${entry.reference}`);
      const comparison = comparePngs(referencePath, actualPath, diffPath, rawDiffPath, {
        analysisScale,
        pixelThreshold,
        rawPixelThreshold
      });
      const maxDiffPercent = entry.maxDiffPercent ?? baselineConfig.defaultMaxDiffPercent;
      const pass = comparison.dimensionsMatch && comparison.diffPercent <= maxDiffPercent;

      results.push({
        ...comparison,
        id: entry.id,
        slug: entry.slug,
        title: entry.title,
        viewport: `${entry.viewport.width}x${entry.viewport.height}`,
        maxDiffPercent,
        pass,
        outputDir,
        referencePath,
        actualPath,
        diffPath,
        rawDiffPath
      });
    }
  } finally {
    await browser.close();
    for (const server of [...servers].reverse()) {
      if (process.platform === "win32") server.kill();
      else {
        try {
          process.kill(-server.pid, "SIGTERM");
        } catch {
          server.kill();
        }
      }
    }
  }

  const generatedAt = new Date().toISOString();
  const report = { generatedAt, baseUrl, comparison: { mode: "perceptual", analysisScale, pixelThreshold, rawPixelThreshold }, results };
  await writeFile(path.join(outputDir, "report.json"), JSON.stringify(report, null, 2));
  await writeFile(path.join(outputDir, "report.md"), reportMarkdown(results, generatedAt));
  await writeFile(path.join(outputDir, "report.html"), reportHtml(results, outputDir));

  rmSync(latestDir, { recursive: true, force: true });
  await mkdir(outputRoot, { recursive: true });
  await import("node:fs/promises").then(({ cp }) => cp(outputDir, latestDir, { recursive: true }));

  const summary = results.map((result) => `${result.id} ${result.title}: ${pct(result.diffPercent)} perceptual / ${pct(result.rawDiffPercent)} raw ${result.pass ? "PASS" : "REVIEW"}`).join("\n");
  console.log(summary);
  console.log(`Report: ${path.join(latestDir, "report.html")}`);

  if (ci && results.some((result) => !result.pass)) {
    process.exitCode = 1;
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
}).finally(() => {
  rmSync(visualRuntimeDir, { recursive: true, force: true });
});
