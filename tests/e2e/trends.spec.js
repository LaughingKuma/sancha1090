import { test, expect } from "@playwright/test";
import { openWb, url, expectView } from "./helpers.js";

test("dim chips switch the ranking; rank rows open the log or the drill", async ({ page }) => {
  const body = await openWb(page, { wb: "trends" });
  await expect(body.locator(".wb-dims .wb-chip[data-dim='route']")).toHaveAttribute("aria-pressed", "true");
  await expect(body.locator(".uplot")).toHaveCount(1);
  await body.locator(".wb-rank", { hasText: "HND-ITM" }).click();
  await expectView(page, "log");
  expect(url(page).get("wb_od")).toBe("HND-ITM");
  await expect(body.locator(".wb-sub-od")).toHaveText(Array(11).fill("HND → ITM"));

  await page.locator('.wb-view[data-view="trends"]').click();
  await body.locator(".wb-dims .wb-chip[data-dim='airline']").click();
  await expect(body.locator(".wb-dims .wb-chip[data-dim='airline']")).toHaveAttribute("aria-pressed", "true");
  expect(url(page).get("wb_dim")).toBe("airline");
  await body.locator(".wb-rank", { hasText: "Japan Airlines" }).click();
  await expectView(page, "drill");
  expect(url(page).get("wb_airline")).toBe("Japan Airlines");
  await expect(body.locator(".wb-crumb").last()).toHaveText("Japan Airlines");

  // airport scope rides history.state, not the query string — the crumb is the only visible proof
  await page.locator('.wb-view[data-view="trends"]').click();
  await body.locator(".wb-dims .wb-chip[data-dim='airport']").click();
  await body.locator(".wb-rank", { hasText: "ITM" }).click();
  await expectView(page, "drill");
  await expect(body.locator(".wb-crumb").last()).toHaveText("ITM");
  await expect(body.locator(".wb-sub-od")).toHaveText(Array(16).fill(/ITM/));
});
