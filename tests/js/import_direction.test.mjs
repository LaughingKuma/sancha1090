import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const livemap = fileURLToPath(new URL("../../livemap/", import.meta.url));
const featuresDir = join(livemap, "src/features");
const mapSrcDir = join(livemap, "src/map");
const staticDir = join(livemap, "static");

const CODE = /\.(?:js|ts|d\.ts)$/;
const walk = (dir) =>
  readdirSync(dir, { recursive: true, withFileTypes: true })
    .filter((d) => d.isFile() && CODE.test(d.name))
    .map((d) => join(d.parentPath, d.name));
const flat = (dir) =>
  readdirSync(dir, { withFileTypes: true }).filter((d) => d.isFile() && CODE.test(d.name)).map((d) => join(dir, d.name));

// static `from "x"` / `import "x"` and dynamic `import("x")` alike; no template-literal specifiers exist here
const SPEC_RE = /(?:\bfrom\s*|\bimport\s*\(?\s*)["']([^"']+)["']/g;
const TYPE_RE = /\b(?:import|export)\s+type\b[^;]*?\bfrom\s*["']([^"']+)["']/g;
const specs = (text) => [...text.matchAll(SPEC_RE)].map((m) => m[1]);
const typeSpecs = (text) => new Set([...text.matchAll(TYPE_RE)].map((m) => m[1]));
const inside = (dir, p) => p === dir || p.startsWith(dir + "/");
// Vite resolves a root-absolute specifier against the project root, so "/static/x.js" is a map import too
const target = (file, spec) => (spec.startsWith("/") ? join(livemap, spec) : resolve(dirname(file), spec));

test("a feature island imports nothing outside itself but map types", () => {
  const bad = [];
  for (const file of walk(featuresDir)) {
    const text = readFileSync(file, "utf8");
    const types = typeSpecs(text);
    for (const spec of specs(text)) {
      if (!/^[./]/.test(spec)) continue; // bare npm specifiers are the gate script's allowlist
      const to = target(file, spec);
      if (inside(featuresDir, to)) continue;
      // the facade contract is the one seam: types only, never runtime code
      if (types.has(spec) && inside(mapSrcDir, to)) continue;
      bad.push(`${file} → ${spec}`);
    }
  }
  assert.deepEqual(bad, []);
});

test("the map never imports a feature, beyond the one advertised dynamic gate", () => {
  const gate = join(staticDir, "map.js");
  const bad = [];
  for (const file of [...walk(mapSrcDir), ...flat(staticDir)]) {
    const text = readFileSync(file, "utf8");
    for (const spec of specs(text)) {
      if (!/^[./]/.test(spec)) continue;
      const to = target(file, spec);
      if (!inside(join(staticDir, "features"), to) && !inside(featuresDir, to)) continue;
      if (file === gate && spec === "./features/workbench/index.js") continue;
      bad.push(`${file} → ${spec}`);
    }
  }
  assert.deepEqual(bad, []);
});
