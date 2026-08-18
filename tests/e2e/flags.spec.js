import { test, expect } from "@playwright/test";
import { openWb, url, expectFocusDrawn } from "./helpers.js";

test("class chips filter the feed and a row focuses", async ({ page }) => {
  const body = await openWb(page, { wb: "flags" });
  await expect(body.locator(".wb-classes .wb-chip[data-cls='']")).toHaveAttribute("aria-pressed", "true");
  await expect(body.locator(".wb-inst")).toHaveCount(13);
  const diversion = body.locator(".wb-classes .wb-chip[data-cls='diversion']");
  await diversion.click();
  await expect(diversion).toHaveAttribute("aria-pressed", "true");
  expect(url(page).get("wb_class")).toBe("diversion");
  await expect(body.locator(".wb-inst")).toHaveCount(3);
  await expect(body.locator(".wb-flagcls")).toHaveText(["diversion", "diversion", "diversion"]);
  await expect(body.locator(".wb-detail").first()).toHaveText("dest RJNA vs modal RJGG 79/87");

  await body.locator(".wb-inst").first().click();
  await expectFocusDrawn(page);
  await expect(body.locator(".wb-inst").first()).toHaveClass(/wb-active/);
  expect(url(page).get("wb_inst")).toBeTruthy();
});
