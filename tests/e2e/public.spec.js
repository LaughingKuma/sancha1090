import { test, expect } from "@playwright/test";
import { stubNetwork, expectMapUp } from "./helpers.js";

// runs under the "public" project (its own baseURL); that fixture boots with LADD unloaded, so /track and
// /aircraft are not exercised here — the in-process 404 contract lives in tests/test_livemap_workbench.py
test("public instance serves the map with zero workbench surface", async ({ page }) => {
  await stubNetwork(page);
  const leaks = [];
  page.on("request", (r) => {
    if (/\/features\/|\/workbench/.test(r.url())) leaks.push(r.url());
  });
  // the map's /features probe must have run and been refused before the DOM assertion means anything
  const probe = page.waitForResponse((r) => new URL(r.url()).pathname === "/features");
  await page.goto("/");
  await expectMapUp(page);
  expect((await probe).status()).toBe(404);
  await expect(page.locator('[class*="wb-"], #wb-rail')).toHaveCount(0);
  expect(leaks).toEqual([]);
});
