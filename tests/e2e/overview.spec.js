import { test, expect } from "@playwright/test";
import { openWb, url, expectView } from "./helpers.js";

// every number is a doorway: each must land on exactly the scope it was computed under
const DOORWAYS = [
  ["flights cell", ".wb-strip .wb-cell-go", "flights", { wb: "log", wb_class: null }],
  ["flagged cell", ".wb-strip .wb-cell-go", "flagged", { wb: "flags", wb_class: null }],
  ["diversion chip", ".wb-panel .wb-classes .wb-chip", "diversion", { wb: "flags", wb_class: "diversion" }],
  ["mover row", ".wb-panel .wb-rank", "HND-ITM", { wb: "log", wb_od: "HND-ITM" }],
  ["trends head", ".wb-panel-head", "trends", { wb: "trends" }],
];

for (const [label, sel, hasText, want] of DOORWAYS) {
  test(`doorway ${label} → ${want.wb}`, async ({ page }) => {
    const body = await openWb(page, { wb: "overview" });
    await body.locator(sel, { hasText }).click();
    await expectView(page, want.wb);
    for (const [k, v] of Object.entries(want)) expect(url(page).get(k)).toBe(v);
    expect(url(page).get("wb_d")).toBe("all");
  });
}

test("estimates/coverage heads carry a custom range; Back/Forward walk the views with it", async ({ page }) => {
  const body = await openWb(page, { wb: "overview", wb_d: "2026-07-28..2026-07-30" });
  for (const view of ["estimates", "coverage"]) {
    await body.locator(".wb-panel-head", { hasText: view }).click();
    await expectView(page, view);
    expect(url(page).get("wb_d")).toBe("2026-07-28..2026-07-30");
    await expect(page.locator(".wb-chip[data-custom]")).toHaveAttribute("aria-pressed", "true");
    await page.goBack();
    await expectView(page, "overview");
    expect(url(page).get("wb_d")).toBe("2026-07-28..2026-07-30");
    await expect(body.locator(".wb-strip")).toBeVisible();
    await page.goForward();
    await expectView(page, view);
    expect(url(page).get("wb_d")).toBe("2026-07-28..2026-07-30");
    await page.goBack();
    await expect(body.locator(".wb-strip")).toBeVisible();
  }
});
