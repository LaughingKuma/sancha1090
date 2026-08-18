import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

// wb_contract.json is the one source the Python side reads too; the bundle bakes it in and refuses to
// mount against a server whose /features carries another value.
const contract = JSON.parse(readFileSync(new URL("./wb_contract.json", import.meta.url), "utf8")).contract;

// build.json rides the build dir: public-denied by PublicStatic, and inside the dev bind mount so /healthz
// (static_build) always describes the bundle actually served.
const buildJson = () => ({
  name: "build-json",
  closeBundle() {
    const built = { sha: process.env.GIT_SHA || "unknown", contract, built_at: new Date().toISOString() };
    writeFileSync(new URL("./static/features/workbench/build.json", import.meta.url), JSON.stringify(built) + "\n");
  },
});

// The island is self-contained by construction: every module rolled into the bundle must come from
// src/features (or npm) — resolution is the ground truth no specifier-shape scan can match.
const islandDir = fileURLToPath(new URL("./src/features/", import.meta.url));
const islandOnly = () => ({
  name: "island-only",
  generateBundle(_opts, bundle) {
    const outside = Object.values(bundle)
      .flatMap((c) => c.moduleIds || [])
      .filter((id) => !id.startsWith("\0") && !id.includes("/node_modules/") && !id.startsWith(islandDir));
    if (outside.length) this.error(`workbench bundle reaches outside src/features: ${outside.join(", ")}`);
  },
});

export default defineConfig(({ mode }) => ({
  base: "/features/workbench/",
  define: { __WB_CONTRACT__: JSON.stringify(contract) },
  plugins: [buildJson(), islandOnly()],
  build: {
    outDir: "static/features/workbench",
    minify: mode !== "development",
    rollupOptions: {
      input: "src/features/workbench/index.js",
      preserveEntrySignatures: "strict", // app-mode default drops the entry's exports; the map calls init()
      output: { entryFileNames: "index.js", assetFileNames: "index[extname]" },
    },
  },
}));
