import { test, expect } from "@playwright/test";
import { openWb, url } from "./helpers.js";

test("airline → service → instances, crumbs and od-chip toggle", async ({ page }) => {
  const body = await openWb(page, { wb: "drill" });
  await body.locator(".wb-row", { hasText: "All Nippon Airways" }).click();
  await expect(body.locator(".wb-sect")).toHaveText("services");
  await expect(body.locator(".wb-crumb")).toHaveText(["all", "All Nippon Airways"]);
  expect(url(page).get("wb_airline")).toBe("All Nippon Airways");
  await expect(body.locator(".wb-row .wb-name")).toHaveText(["ANA85", "ANA20", "ANA1"]);

  await body.locator(".wb-row", { hasText: "ANA1" }).click();
  await expect(body.locator(".wb-sect")).toHaveText("instances");
  await expect(body.locator(".wb-crumb")).toHaveText(["all", "All Nippon Airways", "ANA1"]);
  await expect(body.locator(".wb-crumb").last()).toHaveAttribute("aria-current", "page");
  expect(url(page).get("wb_svc")).toBe("ANA1");
  await expect(body.locator(".wb-inst")).toHaveCount(7);

  const chip = body.locator(".wb-odchips .wb-chip").first();
  await expect(chip).toHaveText("HND-ITM 5");
  await chip.click();
  await expect(chip).toHaveAttribute("aria-pressed", "true");
  expect(url(page).get("wb_od")).toBe("HND-ITM");
  await expect(body.locator(".wb-inst")).toHaveCount(5);
  await chip.click();
  expect(url(page).get("wb_od")).toBeNull();
  await expect(body.locator(".wb-inst")).toHaveCount(7);

  await body.locator(".wb-crumb", { hasText: "All Nippon Airways" }).click();
  await expect(body.locator(".wb-sect")).toHaveText("services");
  expect(url(page).get("wb_svc")).toBeNull();
});
