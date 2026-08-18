import { test, expect } from "@playwright/test";
import { openWb, url, expectView } from "./helpers.js";

test("typing opens the grouped dropdown, Enter drills, Escape dismisses", async ({ page }) => {
  await openWb(page);
  const q = page.locator(".wb-q");
  const drop = page.locator(".wb-drop");
  // "ja": Japan Airlines by name, JAL101 and the JA-registered airframes by prefix; no airport matches
  await q.fill("ja");
  await expect(drop).toBeVisible();
  await expect(q).toHaveAttribute("aria-expanded", "true");
  await expect(drop.locator(".wb-group")).toHaveText(["airlines", "services", "airframes"]);
  await expect(drop.locator(".wb-opt[role='option']").first()).toHaveAttribute("aria-selected", "false");
  await q.fill("hnd");
  await expect(drop.locator(".wb-group")).toHaveText(["airports"]);
  await q.fill("ja");
  await expect(drop.locator(".wb-group")).toHaveCount(3);

  await q.press("Enter");
  await expect(drop).toBeHidden();
  await expectView(page, "drill");
  expect(url(page).get("wb_airline")).toBe("Japan Airlines");
  await expect(q).toHaveValue("");

  await q.fill("jal");
  await expect(drop).toBeVisible();
  await q.press("Escape");
  await expect(drop).toBeHidden();
  await expect(q).toHaveAttribute("aria-expanded", "false");
  // the search Escape must not bubble into the global focus-exit — the drill state stays put
  expect(url(page).get("wb_airline")).toBe("Japan Airlines");
});
