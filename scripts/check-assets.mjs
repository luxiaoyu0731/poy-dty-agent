import { existsSync } from "node:fs";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const rootDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const scanDirs = ["src", "design", "docs"];
const allowedExtensions = new Set([".css", ".html", ".json", ".md", ".ts", ".tsx"]);
const assetPattern = /(?:["'`(=]\s*)(\/visual-reference\/[^"'`)>\s]+|public\/visual-reference\/[^"'`)>\s]+|(?:icons|logos|decorative|illustrations|images)\/[^"'`)>\s]+\.(?:png|svg|jpg|jpeg|webp))/g;
const visualBaselineFile = path.join(rootDir, "design", "visual-baselines.json");

async function walk(dir) {
  const entries = await readdir(dir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...await walk(fullPath));
    } else if (allowedExtensions.has(path.extname(entry.name))) {
      files.push(fullPath);
    }
  }
  return files;
}

function resolveAsset(reference) {
  if (reference.startsWith("/visual-reference/")) {
    return path.join(rootDir, "public", reference);
  }
  if (reference.startsWith("public/")) {
    return path.join(rootDir, reference);
  }
  return path.join(rootDir, "public", "extracted-assets", reference);
}

async function collectVisualBaselineReferences(references) {
  const source = await readFile(visualBaselineFile, "utf8");
  const baseline = JSON.parse(source);
  const pages = Array.isArray(baseline.pages) ? baseline.pages : [];

  if (pages.length === 0) {
    throw new Error("design/visual-baselines.json must define at least one visual baseline page.");
  }

  for (const page of pages) {
    if (!page?.reference) {
      throw new Error(`Visual baseline page ${page?.id ?? "(unknown)"} is missing a reference path.`);
    }
    if (!references.has(page.reference)) references.set(page.reference, new Set());
    references.get(page.reference).add(path.relative(rootDir, visualBaselineFile));
  }
}

async function main() {
  const files = (await Promise.all(scanDirs.map((dir) => walk(path.join(rootDir, dir))))).flat();
  const references = new Map();

  for (const file of files) {
    const source = await readFile(file, "utf8");
    for (const match of source.matchAll(assetPattern)) {
      const reference = match[1];
      if (!references.has(reference)) references.set(reference, new Set());
      references.get(reference).add(path.relative(rootDir, file));
    }
  }

  await collectVisualBaselineReferences(references);

  const missing = [];
  for (const [reference, owners] of references) {
    const resolved = resolveAsset(reference);
    if (!existsSync(resolved)) {
      missing.push({ reference, owners: [...owners].sort(), resolved });
    }
  }

  if (missing.length > 0) {
    console.error(`Missing ${missing.length} referenced asset(s):`);
    for (const item of missing) {
      console.error(`- ${item.reference}`);
      console.error(`  expected: ${path.relative(rootDir, item.resolved)}`);
      console.error(`  referenced by: ${item.owners.join(", ")}`);
    }
    process.exitCode = 1;
    return;
  }

  console.log(`Asset check passed: ${references.size} referenced assets exist.`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
