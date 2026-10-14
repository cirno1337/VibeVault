// Bundles src/ into dist/ (load dist/ as an unpacked extension). `--watch` rebuilds on change.
import { build, context } from "esbuild";
import { cpSync, mkdirSync, rmSync } from "node:fs";

const watch = process.argv.includes("--watch");
rmSync("dist", { recursive: true, force: true });
mkdirSync("dist");
cpSync("static", "dist", { recursive: true });

const common = { bundle: true, target: "chrome120", outdir: "dist", logLevel: "info", legalComments: "none" };
const configs = [
  // Content scripts cannot be ES modules: classic IIFE.
  { ...common, entryPoints: ["src/content.ts"], format: "iife" },
  { ...common, entryPoints: ["src/background.ts", "src/popup.ts", "src/options.ts"], format: "esm" },
];

if (watch) {
  for (const c of configs) await (await context(c)).watch();
} else {
  await Promise.all(configs.map((c) => build(c)));
}
