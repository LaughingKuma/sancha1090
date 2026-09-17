import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render } from "preact";
import { act } from "preact/test-utils";

// happy-dom has no canvas, so uPlot itself is a recording double: what this suite pins is the create-vs-setData
// decision and the teardown, which is everything the component owns.
const made = vi.hoisted(() => [] as Plot[]);

interface Plot {
  opts: Record<string, never>;
  data: unknown;
  target: HTMLElement;
  width: number;
  height: number;
  setData: ReturnType<typeof vi.fn>;
  setSize: ReturnType<typeof vi.fn>;
  destroy: ReturnType<typeof vi.fn>;
}

vi.mock("uplot", () => {
  class FakeUPlot {
    opts: Record<string, never>;
    data: unknown;
    target: HTMLElement;
    width: number;
    height: number;
    setData = vi.fn((d: unknown) => {
      this.data = d;
    });
    setSize = vi.fn((s: { width: number; height: number }) => {
      this.width = s.width;
      this.height = s.height;
    });
    destroy = vi.fn();
    constructor(opts: Record<string, never>, data: unknown, target: HTMLElement) {
      this.opts = opts;
      this.data = data;
      this.target = target;
      this.width = Number(opts.width);
      this.height = Number(opts.height);
      made.push(this as unknown as Plot);
    }
  }
  return { default: FakeUPlot };
});

const { Chart, HUE } = await import("../../livemap/src/features/workbench/chart");
type ChartOpts = Parameters<typeof Chart>[0]["opts"];
type Data = Parameters<typeof Chart>[0]["data"];

// happy-dom's ResizeObserver never fires, so the box is driven by hand rather than by layout
const observers: FakeRO[] = [];
class FakeRO {
  cb: () => void;
  seen: Element[] = [];
  live = true;
  constructor(cb: () => void) {
    this.cb = cb;
    observers.push(this);
  }
  observe(el: Element) {
    this.seen.push(el);
  }
  unobserve() {}
  disconnect() {
    this.live = false;
  }
}

let host: HTMLDivElement;
let box = { w: 0, h: 0 };
const draw = (node: unknown) => act(() => render(node as never, host));
const opts = (over: Record<string, unknown> = {}) => ({ series: [{}, {}], ...over }) as ChartOpts;
const sketch = (o: ChartOpts, data: Data, h = 160) => <Chart class="wb-chart" h={h} opts={o} data={data} />;

beforeEach(() => {
  made.length = 0;
  observers.length = 0;
  box = { w: 420, h: 0 };
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(() => box.w);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(() => box.h);
  vi.stubGlobal("ResizeObserver", FakeRO);
  host = document.body.appendChild(document.createElement("div"));
});
afterEach(() => {
  act(() => render(null, host));
  host.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks(); // here, not at a test's tail — a failed assertion must not leak the stub
});

describe("Chart", () => {
  it("mounts one plot into its own host, at the measured box", () => {
    box = { w: 420, h: 300 };
    draw(sketch(opts(), [[0, 1], [2, 3]]));
    expect(made).toHaveLength(1);
    expect(host.querySelectorAll(".wb-chart")).toHaveLength(1);
    expect(made[0].target).toBe(host.querySelector(".wb-chart"));
    expect(made[0].opts.width).toBe(420);
    expect(made[0].opts.height).toBe(300);
    expect(made[0].data).toEqual([[0, 1], [2, 3]]);
  });

  it("falls back to the h prop when the host measures nothing", () => {
    box = { w: 0, h: 0 };
    draw(sketch(opts(), [[0], [1]], 96));
    expect(made[0].opts.width).toBe(288);
    expect(made[0].opts.height).toBe(96);
  });

  it("carries the legend/cursor defaults and spreads AXIS under every axis", () => {
    draw(sketch(opts({ axes: [{ show: false }, {}] }), [[0], [1]]));
    const o = made[0].opts as Record<string, never>;
    expect(o.legend).toEqual({ show: false });
    expect(o.cursor).toEqual({ y: false });
    const axes = o.axes as unknown as Record<string, unknown>[];
    expect(axes).toHaveLength(2);
    expect(axes[0].show).toBe(false); // the caller's axis wins over the shared spread
    expect(axes[0].stroke).toBe(HUE.dim);
    expect(axes[1].stroke).toBe(HUE.dim);
    expect(axes[1].grid).toEqual({ stroke: HUE.grid, width: 1 });
  });

  it("a caller with no axes still gets the two spread defaults", () => {
    draw(sketch(opts(), [[0], [1]]));
    expect(made[0].opts.axes).toHaveLength(2);
  });

  it("a data-only change reaches setData once and builds nothing new", () => {
    const o = opts();
    draw(sketch(o, [[0], [1]]));
    draw(sketch(o, [[0, 1], [1, 2]]));
    expect(made).toHaveLength(1);
    expect(made[0].setData).toHaveBeenCalledTimes(1);
    expect(made[0].setData).toHaveBeenCalledWith([[0, 1], [1, 2]]);
    expect(made[0].destroy).not.toHaveBeenCalled();
  });

  it("the same data identity never reaches setData", () => {
    const o = opts();
    const data: Data = [[0], [1]];
    draw(sketch(o, data));
    draw(sketch(o, data));
    draw(sketch(o, data));
    expect(made).toHaveLength(1);
    expect(made[0].setData).not.toHaveBeenCalled();
  });

  it("a new opts identity rebuilds, and the new plot draws the newest data", () => {
    draw(sketch(opts(), [[0], [1]]));
    draw(sketch(opts({ series: [{}, {}, {}] }), [[0, 1], [1, 2], [3, 4]]));
    expect(made).toHaveLength(2);
    expect(made[0].destroy).toHaveBeenCalledTimes(1);
    expect(made[1].data).toEqual([[0, 1], [1, 2], [3, 4]]);
  });

  it("unmount destroys the plot and drops the observer", () => {
    draw(sketch(opts(), [[0], [1]]));
    draw(null);
    expect(made[0].destroy).toHaveBeenCalledTimes(1);
    expect(observers[0].live).toBe(false);
  });

  it("the observer resizes on a moved box and stands still on the same one", () => {
    box = { w: 420, h: 300 };
    draw(sketch(opts(), [[0], [1]]));
    expect(observers[0].seen).toEqual([host.querySelector(".wb-chart")]);
    act(() => observers[0].cb());
    expect(made[0].setSize).not.toHaveBeenCalled();
    box = { w: 500, h: 300 };
    act(() => observers[0].cb());
    expect(made[0].setSize).toHaveBeenCalledWith({ width: 500, height: 300 });
    act(() => observers[0].cb());
    expect(made[0].setSize).toHaveBeenCalledTimes(1);
  });
});
