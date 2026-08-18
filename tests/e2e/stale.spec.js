import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test, expect } from "@playwright/test";
import { stubNetwork, expectMapUp } from "./helpers.js";

const baked = JSON.parse(readFileSync(join(__dirname, "../../livemap/wb_contract.json"), "utf8")).contract;

// the built bundle bakes wb_contract.json; a /features from another envelope generation must make it refuse
test("contract mismatch renders the stale line, no rail, map still up", async ({ page }) => {
  await stubNetwork(page);
  await page.route("**/features", (r) => r.fulfill({ json: { features: { workbench: true }, contract: baked + 1 } }));
  await page.goto("/");
  await expect(page.locator(".wb-stale")).toHaveText("workbench bundle stale — rebuild the livemap image");
  await expectMapUp(page);
  await expect(page.locator(".wb-rail")).toHaveCount(0);
});
