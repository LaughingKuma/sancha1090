import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render } from "preact";
import { act } from "preact/test-utils";
import { resetStore } from "./_fixtures";

// the two seams the adapter owns: a still-vanilla view's render() and uPlot teardown. log/drill are Preact
// components now, so a remaining adapter view (flags) stands in for the vanilla render path here.
const mocks = vi.hoisted(() => ({ renderView: vi.fn(), destroyCharts: vi.fn() }));
vi.mock("../../livemap/src/features/workbench/views/flags.js", () => ({
  render: (host: HTMLElement) => {
    mocks.renderView();
    const el = document.createElement("div");
    el.className = "vanilla-view";
    host.appendChild(el);
  },
}));
vi.mock("../../livemap/src/features/workbench/chart.js", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  destroyCharts: mocks.destroyCharts,
}));

const { W, renderFlagRows, unmountIslands } = await import("../../livemap/src/features/workbench/adapter");
const { milAvailable, navigate } = await import("../../livemap/src/features/workbench/store");
const { LegacyView } = await import("../../livemap/src/features/workbench/components/LegacyView");

let host: HTMLDivElement;
const body = () => host.querySelector<HTMLElement>(".wb-body")!;

beforeEach(() => {
  resetStore("/?wb=flags&wb_d=all");
  mocks.renderView.mockClear();
  mocks.destroyCharts.mockClear();
  host = document.body.appendChild(document.createElement("div"));
});
afterEach(() => {
  act(() => render(null, host));
  host.remove();
});

describe("the W ↔ signal bridge", () => {
  it("mirrors every field the vanilla views read, synchronously", () => {
    navigate({ view: "drill", range: "7d", airline: "ANA", service: "ANA1", od: "HND-ITM", mil: true,
      flagClass: "diversion", dim: "airline", page: 3, hex: "86d3a1", apt: "HND", type: "B788", inst: "x" });
    // read immediately: a view rendered by this navigation must not see the previous state
    expect(W).toMatchObject({
      view: "drill", range: "7d", airline: "ANA", service: "ANA1", od: "HND-ITM", mil: true,
      flagClass: "diversion", dim: "airline", page: 3, hex: "86d3a1", apt: "HND", type: "B788", inst: "x",
    });
    milAvailable.value = false;
    expect(W.milAvailable).toBe(false);
  });
});

describe("LegacyView", () => {
  it("renders the view into the ref'd host and names it W.body", () => {
    act(() => render(<LegacyView />, host));
    expect(body().id).toBe("wb-body");
    expect(body().getAttribute("role")).toBe("tabpanel");
    expect(W.body).toBe(body());
    expect(mocks.renderView).toHaveBeenCalledTimes(1);
    expect(body().querySelector(".vanilla-view")).not.toBeNull();
  });

  it("re-runs the view when its fetch key moves, and destroys the charts first", () => {
    act(() => render(<LegacyView />, host));
    act(() => navigate({ page: 2 })); // page is part of the flags fetch key
    expect(mocks.renderView).toHaveBeenCalledTimes(2);
    // the old paint's charts go before the new paint lands — order, not count, is the contract
    expect(mocks.destroyCharts).toHaveBeenCalledTimes(1);
    expect(mocks.destroyCharts.mock.invocationCallOrder[0]).toBeLessThan(mocks.renderView.mock.invocationCallOrder[1]);
    expect(body().querySelectorAll(".vanilla-view")).toHaveLength(1); // replaceChildren, not append
  });

  it("leaves the view alone when only the focus link moves", () => {
    act(() => render(<LegacyView />, host));
    act(() => navigate({ inst: "86d3a1.1785421800.ANA1" }));
    expect(mocks.renderView).toHaveBeenCalledTimes(1); // a row click must never re-fetch the list
  });

  it("re-runs the view when the military-filter availability flips", () => {
    act(() => render(<LegacyView />, host));
    act(() => {
      milAvailable.value = false;
    });
    expect(mocks.renderView).toHaveBeenCalledTimes(2); // milAvailable is part of every view's fetch key
  });

  it("a parent re-render never reconciles the vanilla view's DOM away", () => {
    const Shell = ({ tick }: { tick: number }) => (
      <div>
        <span class="tick">{tick}</span>
        <LegacyView />
      </div>
    );
    act(() => render(<Shell tick={1} />, host));
    const drawn = body().querySelector(".vanilla-view");
    act(() => render(<Shell tick={2} />, host));
    expect(host.querySelector(".tick")!.textContent).toBe("2");
    expect(body().querySelector(".vanilla-view")).toBe(drawn);
    expect(mocks.renderView).toHaveBeenCalledTimes(1);
  });

  it("unmounting tears down the charts and the lists the view mounted", () => {
    act(() => render(<LegacyView />, host));
    renderFlagRows(body(), []);
    expect(body().querySelector(".wb-empty")).not.toBeNull();
    act(() => render(null, host));
    expect(mocks.destroyCharts).toHaveBeenCalledTimes(1); // nothing to destroy at mount; once at unmount
    expect(W.body).toBeNull();
  });

  it("the mounted lists come down with the view, not with the DOM they painted", () => {
    act(() => render(<LegacyView />, host));
    const nested = document.createElement("div");
    renderFlagRows(nested, []);
    expect(nested.querySelector(".wb-empty")).not.toBeNull();
    unmountIslands();
    expect(nested.querySelector(".wb-empty")).toBeNull();
  });
});
