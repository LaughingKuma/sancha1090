import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig } from "@playwright/test";

// Fixture mode: tests/e2e/serve_fixture.py serves app.py with the workbench store and the live
// pollers stubbed — no ClickHouse or RisingWave. Ports sit clear of the running stack (:38100).
const PRIVATE = Number(process.env.E2E_PORT || 38190); // E2E_PORT lets two checkouts run side by side
const PUBLIC = PRIVATE + 1;
// the fixture server needs livemap/requirements.txt: the project venv when present, else the host python
const venv = fileURLToPath(new URL("../.venv/bin/python", import.meta.url));
const python = process.env.E2E_PYTHON || (existsSync(venv) ? venv : "python3");
const server = (port, mode = "") => ({
  command: `${python} tests/e2e/serve_fixture.py ${port} ${mode}`,
  cwd: "..",
  url: `http://127.0.0.1:${port}/healthz`,
  timeout: 15_000,
});

export default defineConfig({
  testDir: "../tests/e2e",
  // 2-worker Chromium contention blows the 30s test timeout on GH Actions' 2-vCPU runners
  workers: process.env.CI ? 1 : 2,
  reporter: "list",
  use: { baseURL: `http://127.0.0.1:${PRIVATE}`, trace: "retain-on-failure" },
  projects: [
    { name: "private", testIgnore: /public\.spec\.js/ },
    { name: "public", testMatch: /public\.spec\.js/, use: { baseURL: `http://127.0.0.1:${PUBLIC}` } },
  ],
  webServer: [server(PRIVATE), server(PUBLIC, "public")],
});
