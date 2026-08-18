import { test, expect } from "@playwright/test";
import { openWb, collectErrors } from "./helpers.js";

const GAP_LABELS = ["<1m", "1–5m", "5–15m", "15m–1h", "1–3h", "3–6h", "6–12h", "≥12h"];

// uPlot draws axis labels on canvas: subclass the vendored constructor as it lands so the spec can
// read the axis formatter back off the instance instead of the pixels
const hookPlots = (page) =>
  page.addInitScript(() => {
    let real;
    Object.defineProperty(globalThis, "uPlot", {
      configurable: true,
      get: () => real,
      set: (v) => {
        real = class extends v {
          constructor(...a) {
            super(...a);
            (globalThis.__plots ||= []).push(this);
          }
        };
      },
    });
  });

test("estimates: eras with full hashes, per-era series, mix facets, logging strip", async ({ page }) => {
  const body = await openWb(page, { wb: "estimates" });
  const eras = body.locator(".wb-era");
  await expect(eras).toHaveCount(2);
  await expect(eras.first()).toHaveClass(/wb-era-cur/);
  await expect(eras.first()).toHaveAttribute("title", "config 2537707548349448576");
  await expect(eras.first()).toContainText(/p50 \d+\.\d\d km/);
  await expect(eras.first()).toContainText(/p90 \d+\.\d\d km/);
  await expect(eras.first()).toContainText("2026-07-29 → 2026-07-30");
  await expect(eras.first()).toContainText("n=162");
  await expect(body.locator(".uplot")).toHaveCount(1);
  await expect(body.locator(".wb-note", { hasText: "solid p50 · dashed p90" })).toBeVisible();
  for (const dim of ["skip", "segment_kind", "uncertainty_bin"]) {
    const rows = body.locator(`.wb-mix-${dim} .wb-kv`);
    await expect(rows.locator(".wb-kv-p")).toContainText(["serving", "serving-private", "serving-public"]);
  }
  await expect(body.locator(".wb-note", { hasText: "UTC-day grain" })).toBeVisible();
  await expect(body.locator(".wb-strip .wb-cell-k")).toHaveText(["settled", "awaiting", "ambiguous", "prov in", "settled in"]);
});

test("coverage: tier mix, all eight gap bins, observed trend", async ({ page }) => {
  await hookPlots(page);
  const body = await openWb(page, { wb: "coverage" });
  await expect(body.locator(".wb-sect")).toHaveText(["tier mix per day", "largest gap", "observed fraction (median)"]);
  await expect(body.locator(".uplot")).toHaveCount(3);
  await expect(body.locator(".wb-legend")).toContainText("unknown 2");
  await expect(body.locator(".wb-note", { hasText: "15m is the settled/estimated tier seam" })).toBeVisible();
  await expect(body.locator(".wb-note", { hasText: "2026-07-30 90.5% · n=22" })).toBeVisible();
  const labels = await page.evaluate(() => {
    const u = globalThis.__plots.find((p) => p.data[1].length === 8);
    return u.axes[0].values(u, u.data[0]);
  });
  expect(labels).toEqual(GAP_LABELS);
});

test("view and range toggles keep the chart count flat; narrow viewport does not overflow", async ({ page }) => {
  test.slow(); // 18 chart re-renders under SwiftShader outrun the 30 s budget on the 2-vCPU CI runner
  const errors = collectErrors(page);
  const body = await openWb(page, { wb: "estimates" });
  // three round trips: a leaked plot would surface as a growing count
  for (let i = 0; i < 3; i++) {
    for (const view of ["coverage", "estimates"]) {
      await page.locator(`.wb-view[data-view="${view}"]`).click();
      await expect(body.locator(".uplot")).toHaveCount(view === "coverage" ? 3 : 1);
    }
    for (const range of ["7d", "30d", "90d", "all"]) {
      await page.locator(`.wb-chip[data-range="${range}"]`).click();
      await expect(page.locator(`.wb-chip[data-range="${range}"]`)).toHaveAttribute("aria-pressed", "true");
      await expect(body.locator(".uplot")).toHaveCount(1);
    }
  }
  expect(errors).toEqual([]);

  await page.setViewportSize({ width: 600, height: 800 });
  await page.locator('.wb-view[data-view="coverage"]').click();
  await expect(body.locator(".uplot")).toHaveCount(3);
  const overflow = await page.evaluate(() => ({
    doc: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    rail: document.querySelector(".wb-rail").scrollWidth - document.querySelector(".wb-rail").clientWidth,
  }));
  expect(overflow).toEqual({ doc: 0, rail: 0 });
});

