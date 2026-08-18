import { test, expect } from "@playwright/test";
import { openWb, url, expectView } from "./helpers.js";

// view -> text every fixture-fed render must contain; "unavailable"/"not deployed" would mean the
// fixture store never reached the builders
const VIEWS = {
  overview: ["flights", "flagged", "trends", "estimates", "coverage"],
  drill: ["airlines", "All Nippon Airways"],
  log: ["ANA1", "HND → ITM"],
  flags: ["diversion", "dest RJNA vs modal RJGG 79/87"],
  trends: ["route", "HND-ITM"],
  estimates: ["config eras", "25377075", "logging stream"],
  coverage: ["tier mix per day", "largest gap", "observed fraction"],
};

for (const [view, texts] of Object.entries(VIEWS)) {
  test(`${view} renders from fixtures`, async ({ page }) => {
    const body = await openWb(page, { wb: view });
    await expectView(page, view);
    for (const t of texts) await expect(body).toContainText(t);
    await expect(body).not.toContainText(/unavailable|not deployed/);
  });
}

test("rail is fixed-position chrome", async ({ page }) => {
  await openWb(page);
  const pos = await page.locator(".wb-rail").evaluate((el) => getComputedStyle(el).position);
  expect(pos).toBe("fixed");
});

test("range presets and custom range re-render and rewrite wb_d", async ({ page }) => {
  const body = await openWb(page, { wb: "log" });
  for (const range of ["7d", "90d", "all"]) {
    await page.locator(`.wb-chip[data-range="${range}"]`).click();
    await expect(page.locator(`.wb-chip[data-range="${range}"]`)).toHaveAttribute("aria-pressed", "true");
    expect(url(page).get("wb_d")).toBe(range);
  }
  await expect(body.locator(".wb-inst")).toHaveCount(50);
  await page.locator(".wb-chip[data-custom]").click();
  await page.getByLabel("Range start").fill("2026-07-29");
  await page.getByLabel("Range end").fill("2026-07-29");
  // URL and request are two seams: a view could rewrite wb_d yet fetch with stale day params
  const fetched = page.waitForRequest((r) => r.url().includes("/workbench/instances"));
  await page.locator(".wb-chip[data-apply]").click();
  const sent = new URL((await fetched).url()).searchParams;
  expect([sent.get("day_from"), sent.get("day_to")]).toEqual(["2026-07-29", "2026-07-29"]);
  expect(url(page).get("wb_d")).toBe("2026-07-29..2026-07-29");
  await expect(page.locator(".wb-chip[data-custom]")).toHaveAttribute("aria-pressed", "true");
  await expect(body.locator(".wb-inst")).toHaveCount(22);
  await expect(body.locator(".wb-date")).toHaveText(Array(22).fill("07-29"));
});
