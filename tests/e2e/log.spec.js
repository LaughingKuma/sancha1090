import { test, expect } from "@playwright/test";
import { FIX, openWb, url, expectFocusDrawn, wbState } from "./helpers.js";

test("filters narrow the list and the pager walks it", async ({ page }) => {
  const body = await openWb(page, { wb: "log" });
  const rows = body.locator(".wb-inst");
  await expect(rows).toHaveCount(50);
  await expect(body.locator(".wb-pager .wb-count")).toHaveText("1–50 of 66");
  await body.locator(".wb-pager .wb-chip[data-step='1']").click();
  expect(url(page).get("wb_p")).toBe("2");
  await expect(rows).toHaveCount(16);
  await expect(body.locator(".wb-pager .wb-chip[data-step='1']")).toBeDisabled();
  await body.locator(".wb-pager .wb-chip[data-step='-1']").click();
  expect(url(page).get("wb_p")).toBeNull();

  await body.locator(".wb-f-day").fill("2026-07-29");
  expect(url(page).get("wb_d")).toBe("2026-07-29..2026-07-29");
  await expect(rows).toHaveCount(22);
  await page.locator(".wb-chip[data-range='all']").click();

  await body.locator(".wb-f-apt").fill("cts");
  await body.locator(".wb-f-apt").press("Enter");
  await expect(rows).toHaveCount(14);
  await expect(body.locator(".wb-sub-od").first()).toContainText("CTS");
  await body.locator(".wb-f-type").fill("A320");
  await body.locator(".wb-f-type").press("Enter");
  await expect(rows).toHaveCount(6);
  await body.locator(".wb-f-clear").click();
  await expect(rows).toHaveCount(50);

  await body.locator(".wb-f-mil").click();
  await expect(body.locator(".wb-f-mil")).toHaveAttribute("aria-pressed", "true");
  expect(url(page).get("wb_mil")).toBe("1");
  await expect(rows).toHaveCount(6);
  await expect(body.locator(".wb-inst .t-mil")).toHaveCount(6);
});

test("row click focuses, Esc and Back leave focus, Forward restores it", async ({ page }) => {
  const body = await openWb(page, { wb: "log" });
  const row = body.locator(".wb-inst").first();
  await row.click();
  const bar = page.locator(".wb-focus");
  await expect(bar.locator(".wb-cs")).toHaveText("ANA1");
  await expectFocusDrawn(page);
  // the shared cell is the seam the map facade will own: focus claimed, spotlight handle released
  const st = await wbState(page);
  expect(st).toMatchObject({ focusKey: FIX.key, histFlightId: null });
  expect(st.focusN).toBeGreaterThan(0);
  await expect(row).toHaveClass(/wb-active/);
  await expect(row).toHaveAttribute("aria-pressed", "true");
  expect(url(page).get("wb_inst")).toBe(FIX.key);

  await page.keyboard.press("Escape");
  await expect(bar).toHaveCount(0);
  await expect(body.locator(".wb-active")).toHaveCount(0);
  expect(url(page).get("wb_inst")).toBeNull();
  expect((await wbState(page)).focusKey).toBeNull();

  await row.click();
  await expect(bar).toBeVisible();
  await page.goBack();
  await expect(bar).toHaveCount(0);
  expect(url(page).get("wb_inst")).toBeNull();
  await page.goForward();
  await expect(bar.locator(".wb-cs")).toHaveText("ANA1");
  expect(url(page).get("wb_inst")).toBe(FIX.key);
  await page.keyboard.press("Escape");
  await expect(bar).toHaveCount(0);

  // a tier-none row has no path to draw: it flashes instead of claiming focus
  const none = body.locator(".wb-inst", { has: page.locator(".t-none") }).first();
  await none.click();
  await expect(none.locator(".wb-nopath")).toHaveText("no recorded path");
  await expect(bar).toHaveCount(0);

  // a provisional row fetches; the app classifies it past the settlement head with no bronze points, and
  // the empty answer releases the claim: flash, no bar, wb_inst dropped, cell cleared
  const prov = body.locator(".wb-inst", { has: page.locator(".t-provisional") }).first();
  const empty = page.waitForResponse((r) => /\/path\//.test(r.url()));
  await prov.click();
  expect(await (await empty).json()).toMatchObject({ provisional: true, points: [] });
  await expect(prov.locator(".wb-nopath")).toHaveText("no recorded path");
  await expect(bar).toHaveCount(0);
  await expect.poll(() => url(page).get("wb_inst")).toBeNull();
  expect((await wbState(page)).focusKey).toBeNull();
});

test("wb_inst deep link resolves to the canonical key and draws", async ({ page }) => {
  // 60 s off the fixture start and without the callsign segment: nearest-start wins, key is canonicalized
  const body = await openWb(page, { wb: "log", wb_inst: "86D3A1.1785421860" });
  const bar = page.locator(".wb-focus");
  await expect(bar.locator(".wb-cs")).toHaveText("ANA1");
  await expectFocusDrawn(page);
  await expect.poll(() => url(page).get("wb_inst")).toBe(FIX.key);
  await expect(body.locator(".wb-inst.wb-active")).toHaveCount(1);
});
