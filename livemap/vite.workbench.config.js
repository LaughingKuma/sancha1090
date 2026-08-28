import { readFileSync, writeFileSync } from "node:fs";
import { gzipSync } from "node:zlib";
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

// size mode: the entry alone, uPlot left external, nothing written — the plan's "≤ 30 KB gz excluding uPlot"
const ENTRY_GZ_LIMIT = 30 * 1024;
const sizeGate = () => ({
  name: "size-gate",
  generateBundle(_opts, bundle) {
    for (const c of Object.values(bundle)) {
      if (c.type !== "chunk" || !c.isEntry) continue;
      const gz = gzipSync(c.code).length;
      console.log(`entry ${c.fileName}: ${gz} bytes gz (uplot external, limit ${ENTRY_GZ_LIMIT})`);
      if (gz > ENTRY_GZ_LIMIT) this.error(`entry exceeds the ${ENTRY_GZ_LIMIT} B gz budget`);
    }
  },
});

export default defineConfig(({ mode }) => {
  const size = mode === "size";
  return {
    base: "/features/workbench/",
    define: { __WB_CONTRACT__: JSON.stringify(contract) },
    plugins: size ? [islandOnly(), sizeGate()] : [buildJson(), islandOnly()],
    build: {
      outDir: "static/features/workbench",
      write: !size,
      minify: mode !== "development",
      rollupOptions: {
        input: "src/features/workbench/index.tsx",
        external: size ? ["uplot"] : [],
        preserveEntrySignatures: "strict", // app-mode default drops the entry's exports; the map calls init()
        output: { entryFileNames: "index.js", assetFileNames: "index[extname]" },
      },
    },
  };
});
