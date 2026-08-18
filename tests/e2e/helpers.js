import { expect } from "@playwright/test";

const CARTO_STYLE = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";
const FONTS = /fonts\.(googleapis|gstatic)\.com/;
const EMPTY_STYLE = { version: 8, sources: {}, layers: [] };

// one shared identity for the ANA1 fixture flight (rows/instances.json[0]) so specs and server can't drift
export const FIX = { fid: "12345678901234000000", hex: "86d3a1", key: "86d3a1.1785421800.ANA1" };

// The map page reaches out for a basemap style and web fonts; neither may leave the sandbox.
export async function stubNetwork(page) {
  await page.route(CARTO_STYLE, (r) => r.fulfill({ json: EMPTY_STYLE }));
  await page.route(FONTS, (r) => r.abort());
}

// Every private spec lands on the rail with wb_d=all: the fixture rows carry fixed 2026-07 days,
// so a relative preset would honestly show nothing.
export async function openWb(page, params = {}) {
  await stubNetwork(page);
  await page.goto(`/?${new URLSearchParams({ wb_d: "all", ...params })}`);
  // the rail mounts through /features + a 16-module dynamic import beside a SwiftShader map; the 5 s
  // default flakes on a loaded host or a CI runner — this is the budget for the mount, not slack
  await expect(page.locator(".wb-rail")).toBeVisible({ timeout: 15_000 });
  return page.locator("#wb-body");
}

export const url = (page) => new URL(page.url()).searchParams;

export const expectView = (page, view) =>
  expect(page.locator(`.wb-view[data-view="${view}"]`)).toHaveAttribute("aria-selected", "true");

export const expectFocusDrawn = (page) =>
  expect(page.locator(".wb-focus .wb-meta").last()).toHaveText(/^[1-9]\d* pts$/);

// Focus is the workbench's own state now, read where a user would see it (the bar + the URL); the shared
// cell answers only for what the MAP drew, which the facade owns.
export const wbState = (page) =>
  page.evaluate(() => {
    const S = globalThis.__sancha_state;
    const bar = document.querySelector(".wb-focus");
    const pts = bar ? [...bar.querySelectorAll(".wb-meta")].pop() : null;
    return {
      focusKey: bar ? new URLSearchParams(location.search).get("wb_inst") : null,
      focusN: Number((pts?.textContent || "").replace(/[^0-9]/g, "")) || 0, // 0 while "loading path…"
      histFlightId: S.histFlightId,
      histPathN: S.histPathN,
      selected: S.selected?.hex ?? null,
    };
  });

// registered before navigation so load-time errors count; only the aborted fonts route may log an error
export function collectErrors(page) {
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error" && !FONTS.test(m.location().url || "")) errors.push(m.text());
  });
  return errors;
}

export async function expectMapUp(page) {
  await expect(page.locator(".masthead")).toBeVisible();
  await expect(page.locator("#map")).toBeAttached();
}
