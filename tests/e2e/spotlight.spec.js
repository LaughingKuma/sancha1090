import { test, expect } from "@playwright/test";
import { FIX, openWb, expectFocusDrawn, wbState } from "./helpers.js";

test("a focus claim supersedes the spotlight's in-flight /path fetch", async ({ page }) => {
  // hold the spotlight's /path answer so the focus claim lands first; once released it must be ignored
  let release, calls = 0;
  const held = new Promise((r) => (release = r));
  await page.route(`**/path/${FIX.fid}`, async (route) => {
    if (calls++ === 0) await held; // the second caller is the focus row: answered at once
    return route.continue();
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

  await body.locator(".wb-inst").first().click();
  await expectFocusDrawn(page);
  // the focus row's /path has already answered, so the next response on this URL is the held spotlight one
  const late = page.waitForResponse(`**/path/${FIX.fid}`);
  release();
  await (await late).finished();
  await expect.poll(() => wbState(page)).toMatchObject({
    focusKey: FIX.key, histFlightId: null, selected: null, histPathN: 9,
  });
  await expect(page.locator(".ff-row.ff-active")).toHaveCount(0);

  // the facade's capture-phase guard swallows a bare map click before it can reach the map (or anything
  // above it): a document-level listener is the witness — it must fire only once focus is gone
  await page.evaluate(() => {
    window.__mapClicks = 0;
    document.addEventListener("click", (e) => { if (e.target.closest("#map")) window.__mapClicks++; });
  });
  await canvas.click();
  await expect.poll(() => wbState(page)).toMatchObject({ focusKey: FIX.key, selected: null });
  await expect(page.locator(".wb-focus")).toBeVisible();
  expect(await page.evaluate(() => window.__mapClicks)).toBe(0);
  await page.keyboard.press("Escape");
  await expect(page.locator(".wb-focus")).toHaveCount(0);
  await canvas.click();
  await expect.poll(() => page.evaluate(() => window.__mapClicks)).toBe(1);
});
