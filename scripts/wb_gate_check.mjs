// Build-layer public gate: the bundle lands only as static/features/workbench/{index.js,index.css,build.json}, it
// is self-contained (its only input is the facade passed at init), and imports stay allowlisted.
import { readdirSync, readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const rel = (p) => relative(root, p);
const fail = (msg) => { console.error(`wb_gate_check: ${msg}`); process.exit(1); };
const files = (dir) =>
  readdirSync(dir, { recursive: true, withFileTypes: true }).filter((d) => d.isFile()).map((d) => join(d.parentPath, d.name));
const git = (...args) => execFileSync("git", args, { cwd: root, encoding: "utf8" }).trim();
const ALLOWED = new Set(["preact", "@preact/signals", "uplot"]);

const staticDir = join(root, "livemap/static");
const outDir = join(staticDir, "features/workbench");
const srcDir = join(root, "livemap/src");
let out;
try { out = files(outDir).map((p) => relative(outDir, p)).sort(); } catch { fail("no build output — run make livemap-build"); }
if (out.join(",") !== "build.json,index.css,index.js") fail(`build output must be exactly index.js + index.css + build.json, got: ${out.join(", ")}`);
if (git("ls-files", "--", "livemap/static/features/workbench")) fail("build output is tracked");
const leaked = git("ls-files", "--others", "--", "livemap/static").split("\n").filter((l) => l && !l.startsWith("livemap/static/features/workbench/"));
if (leaked.length) fail(`untracked files under livemap/static (a build leak):\n${leaked.join("\n")}`);
// a build that rewrote a tracked map file would enter the image; CI checkouts are clean, so any change is the build
const touched = git("diff", "--name-only", "--", "livemap/static");
if (touched && process.env.CI) fail(`tracked files under livemap/static modified by the build:\n${touched}`);

// module identity: the map carries one pin; the built island imports nothing (source-side seam rules live in
// tests/js/import_direction.test.mjs and the island-only build plugin — resolution, not specifier shape)
const mapFiles = files(staticDir).filter((p) => /\.(js|html)$/.test(p) && !p.includes("/vendor/") && !p.startsWith(outDir));
// one read serves both the pin scan and the framework-leak scan below
const mapTexts = mapFiles.map((p) => [p, readFileSync(p, "utf8")]);
const mapPins = new Set(mapTexts.flatMap(([, text]) => [...text.matchAll(/"[^"]*\?v=([^"]+)"/g)].map((m) => m[1])));
if (mapPins.size !== 1) fail(`the map must carry exactly one ?v= pin, found: ${[...mapPins].join(", ") || "none"}`);
const [pin] = mapPins;
const specs = (text) => [...text.matchAll(/\b(?:from|import)\s*(['"])([^'"]+)\1/g)].map((m) => m[2]);
const srcSpecs = files(srcDir).flatMap((p) => specs(readFileSync(p, "utf8")));
const built = readFileSync(join(outDir, "index.js"), "utf8");
const emitted = [...new Set(specs(built))].sort();
if (emitted.length) fail(`built index.js must be self-contained, but imports: ${emitted.join(", ")}`);
if (/\bimport\s*\(/.test(built)) fail("built index.js carries a dynamic import() — the island loads nothing at runtime");

// public-gate layer 4: the framework ships INSIDE the island and nowhere else on the map's static surface.
// `.__k`/`.__H` are Preact's mangled `_children`/`_hooks` (pinned by its committed mangle map), and
// `preact-signals` is the signals brand symbol.
const MARKERS = { preact: /\.__k\b/, hooks: /\.__H\b/, signals: /preact-signals/ };
const marks = (text) => Object.keys(MARKERS).filter((k) => MARKERS[k].test(text));
const missing = Object.keys(MARKERS).filter((k) => !MARKERS[k].test(built));
if (missing.length) fail(`built index.js carries no ${missing.join("/")} code — the island did not bundle the framework`);
const leakedMarkers = mapTexts.map(([p, text]) => [rel(p), marks(text)]).filter(([, m]) => m.length);
if (leakedMarkers.length) fail(`framework code outside the island: ${leakedMarkers.map(([p, m]) => `${p} (${m})`).join(", ")}`);

// supply chain: bare imports (bundled) and runtime deps stay within the plan's allowlist
// a scoped package's name is two segments ("@preact/signals"), a subpath import's is one ("preact/hooks")
const pkgOf = (s) => s.split("/").slice(0, s.startsWith("@") ? 2 : 1).join("/");
const bare = [...new Set(srcSpecs.filter((s) => !/^[./]/.test(s)))].filter((s) => !ALLOWED.has(pkgOf(s)));
if (bare.length) fail(`imports outside the runtime allowlist: ${bare.join(", ")}`);
const deps = Object.keys(JSON.parse(readFileSync(join(root, "livemap/package.json"), "utf8")).dependencies || {}).filter((d) => !ALLOWED.has(d));
if (deps.length) fail(`runtime dependencies outside the allowlist: ${deps.join(", ")}`);
console.log(`wb_gate_check: ok (pin ${pin}, bundle self-contained)`);
