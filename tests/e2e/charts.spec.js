import { test, expect } from "@playwright/test";
import { openWb, collectErrors } from "./helpers.js";

// uPlot draws the axis labels on canvas; the legend under the histogram is the same binLabel() in the DOM
const GAP_BINS = ["<1m 21", "1–5m 14", "5–15m 9", "15m–1h 7", "1–3h 5", "3–6h 3", "6–12h 2", "≥12h 1"];

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
  const body = await openWb(page, { wb: "coverage" });
  await expect(body.locator(".wb-sect")).toHaveText(["tier mix per day", "largest gap", "observed fraction (median)"]);
  await expect(body.locator(".uplot")).toHaveCount(3);
  // the gap legend below carries the same two classes, so the tier totals need the narrower selector
  await expect(body.locator(".wb-legend:not(.wb-gapbins)")).toContainText("unknown 2");
  await expect(body.locator(".wb-note", { hasText: "15m is the settled/estimated tier seam" })).toBeVisible();
  await expect(body.locator(".wb-note", { hasText: "2026-07-30 90.5% · n=22" })).toBeVisible();
  await expect(body.locator(".wb-gapbins span")).toHaveText(GAP_BINS);
});

test("view and range toggles keep the chart count flat; narrow viewport does not overflow", async ({ page }) => {
  // a count would lie: how many plots the range loop rebuilds depends on which presets still cover
  // the fixture days. Every toggle redraws under SwiftShader, which outruns 30 s on a 2-vCPU runner.
  test.slow();
  const errors = collectErrors(page);
  const body = await openWb(page, { wb: "estimates" });
  // three round trips: a leaked plot would surface as a growing count
  for (let i = 0; i < 3; i++) {
    for (const view of ["coverage", "estimates"]) {
      await page.locator(`.wb-view[data-view="${view}"]`).click();
      await expect(body.locator(".uplot")).toHaveCount(view === "coverage" ? 3 : 1);
    }
    let sawEmpty = false;
    for (const range of ["7d", "30d", "90d", "all"]) {
      // the presets are relative to today and the fixture days are fixed 2026-07, so which preset
      // empties the window moves with the calendar — read the window the server just answered
      const answered = page.waitForResponse((r) => r.url().includes("/workbench/estimates"));
      await page.locator(`.wb-chip[data-range="${range}"]`).click();
      const est = await (await answered).json();
      const days = new Set(est.daily.map((r) => r.day)).size;
      sawEmpty ||= est.headline.length === 0;
      await expect(page.locator(`.wb-chip[data-range="${range}"]`)).toHaveAttribute("aria-pressed", "true");
      await expect(body.locator(".wb-era")).toHaveCount(est.headline.length);
      await expect(body.locator(".wb-empty", { hasText: "no scored estimates in range" }))
        .toHaveCount(est.headline.length ? 0 : 1);
      // the chart needs two days to draw, so an emptied window must tear the plot down, not keep it
      await expect(body.locator(".uplot")).toHaveCount(days > 1 ? 1 : 0);
    }
    // the loop ends on "all" — the full payload back again is what proves the empty windows rebuilt
    expect(sawEmpty).toBe(true);
    await expect(body.locator(".wb-era")).toHaveCount(2);
    await expect(body.locator(".uplot")).toHaveCount(1);
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

