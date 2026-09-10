import { test, expect } from "@playwright/test";
import { FIX, openWb, expectFocusDrawn, wbState } from "./helpers.js";

test("a focus claim supersedes the spotlight's in-flight /path fetch", async ({ page }) => {
  // hold the spotlight's /path answer so the focus claim lands first; the claim aborts it outright
  let release, calls = 0;
  const held = new Promise((r) => (release = r));
  await page.route(`**/path/${FIX.fid}`, async (route) => {
    if (calls++ === 0) await held; // the second caller is the focus row: answered at once
    return route.continue().catch(() => {}); // the held request is gone by the time it is released
  });
  const body = await openWb(page, { wb: "log" });
  // the fixture parks the plane on the map center; deck picks it once a frame has drawn it
  const canvas = page.locator("#map canvas").first();
  await expect.poll(async () => {
    await canvas.click();
    return (await wbState(page)).selected;
  }, { timeout: 15_000 }).toBe(FIX.hex);
  const sighting = page.locator(".ff-row.ff-clickable").first();
  await expect(sighting).toContainText("HND → ITM");
  await sighting.click();
  await expect(sighting).toHaveAttribute("aria-pressed", "true");
  expect((await wbState(page)).histFlightId).toBe(FIX.fid);

  // the focus claim cuts the spotlight's in-flight download loose (no body downloaded, none parsed):
  // the held request fails client-side before the route ever releases it
  const aborted = page.waitForEvent("requestfailed", (r) => r.url().includes(`/path/${FIX.fid}`));
  await body.locator(".wb-inst").first().click();
  await expectFocusDrawn(page);
  expect((await aborted).failure()?.errorText).toContain("ABORTED");
  release();
  await expect.poll(() => wbState(page)).toMatchObject({
    focusKey: FIX.key, histFlightId: null, selected: null, histPathN: 9,
  });
  await expect(page.locator(".ff-row.ff-active")).toHaveCount(0);

  // enterFocus dropped the selection, so the witness plants one: with the guard disabled a bare click clears
  // it and clearSelection wipes the drawn path (histPathN 9 → 0), which is what the flag exists to prevent.
  const plant = () => page.evaluate((hex) => { globalThis.__sancha_state.selected = { hex, pts: [], mil: false }; }, FIX.hex);
  await plant();
  await canvas.click();
  await expect.poll(() => wbState(page)).toMatchObject({ focusKey: FIX.key, selected: FIX.hex, histPathN: 9 });
  await expect(page.locator(".wb-focus")).toBeVisible();
  // the zoom buttons live inside #map: the retired capture-phase swallow starved them for the whole focus
  await page.evaluate(() => {
    window.__zoomClicks = 0;
    document.querySelector(".maplibregl-ctrl-zoom-in").addEventListener("click", () => window.__zoomClicks++);
  });
  await page.locator(".maplibregl-ctrl-zoom-in").click();
  expect(await page.evaluate(() => window.__zoomClicks)).toBe(1);
  await expect.poll(() => wbState(page)).toMatchObject({ focusKey: FIX.key, histPathN: 9 });
  await page.keyboard.press("Escape");
  await expect(page.locator(".wb-focus")).toHaveCount(0);
  await plant(); // the guard left with the focus: a bare click clears a selection again
  await canvas.click();
  await expect.poll(() => wbState(page)).toMatchObject({ focusKey: null, selected: null });
});
