import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render } from "preact";
import { act } from "preact/test-utils";
import { flush, resetStore, restoreFetch, stubFetch } from "./_fixtures";

// happy-dom has no canvas, so the one uPlot host is stubbed: the real option/data builders still run,
// and the stub publishes what they produced.
vi.mock("../../livemap/src/features/workbench/chart", async (orig) => ({
  ...(await orig<Record<string, unknown>>()),
  Chart: ({ class: cls, h, opts, data }: {
    class: string; h: number; opts: { series: { label?: string }[] }; data: unknown[];
  }) => (
    <div class={cls} data-h={h} data-series={opts.series.slice(1).map((s) => s.label ?? "·").join("|")}
      data-data={JSON.stringify(data)} />
  ),
}));

const { wb } = await import("../../livemap/src/features/workbench/store");
const { Overview } = await import("../../livemap/src/features/workbench/components/Overview");
const { Flags } = await import("../../livemap/src/features/workbench/components/Flags");
const { Trends } = await import("../../livemap/src/features/workbench/components/Trends");
const { Estimates } = await import("../../livemap/src/features/workbench/components/Estimates");
const { Coverage } = await import("../../livemap/src/features/workbench/components/Coverage");

let host: HTMLDivElement;
const draw = (node: unknown) => act(() => render(node as never, host));
const text = (sel: string) => [...host.querySelectorAll(sel)].map((e) => e.textContent);
const one = (sel: string) => host.querySelector(sel);
const charts = () => [...host.querySelectorAll<HTMLElement>("[data-data]")];

beforeEach(() => {
  host = document.body.appendChild(document.createElement("div"));
});
afterEach(() => {
  act(() => render(null, host));
  host.remove();
  restoreFetch();
  vi.restoreAllMocks(); // here, not at a test's tail — a failed assertion must not leak the stub
});

const instRow = {
  icao24: "86D3A1", day: "2026-07-29", start_ts: 1785421800, callsign: "ANA1",
  airline: "All Nippon Airways", origin: { icao: "RJTT", iata: "HND" }, dest: { icao: "RJOO", iata: "ITM" },
  tier: "settled", flight_id: "999",
};
const summaryBody = (over: Record<string, unknown> = {}) => ({
  flights: 1234, aircraft: 88, services: 42,
  daily: [["2026-07-28", 10], ["2026-07-29", 12], ["2026-07-30", 9]],
  flags: { available: true, flagged: 13, classes: { diversion: 3, military: 2 } },
  tiers: {
    available: true, mix: { settled: 20, estimated: 5 },
    daily: [["2026-07-28", { settled: 9, estimated: 7 }], ["2026-07-29", { settled: 10, estimated: 6 }]],
  },
  est: { available: true, err_p50_km: 3.214, n: 162, daily: [["2026-07-28", 3.1, 20], ["2026-07-29", 3.3, 22]] },
  movers: [{ key: "HND-ITM", n: 44, prev_n: 40, delta_pct: 10 }, { key: "HND-CTS", n: 30, prev_n: 33, delta_pct: -9.1 }],
  ...over,
});
const flagsBody = (over: Record<string, unknown> = {}) => ({
  available: true, flags: [{ ...instRow, flag_class: "diversion", detail: "dest RJNA vs modal RJGG" }],
  classes: { diversion: 3, military: 2 }, total: 3, limit: 50, offset: 0, ...over,
});
const trendsBody = (over: Record<string, unknown> = {}) => ({
  dim: "route",
  series: [{ key: "HND-ITM", points: [["2026-07-28", 4], ["2026-07-29", 6]] },
    { key: "HND-CTS", points: [["2026-07-29", 3]] }],
  rank: [{ key: "HND-ITM", n: 44, distinct_aircraft: 9, prev_n: 40, delta_pct: 10 }],
  total: 1, limit: 20, offset: 0, ...over,
});
const estimatesBody = (over: Record<string, unknown> = {}) => ({
  available: true,
  headline: [{ config_hash: "2537707548349448576", n: 162, p50_km: 3.21, p90_km: 9.87, first_day: "2026-07-29", last_day: "2026-07-30" }],
  daily: [{ day: "2026-07-29", config_hash: "2537707548349448576", p50_km: 3.2, p90_km: 9.8, n: 80 },
    { day: "2026-07-30", config_hash: "2537707548349448576", p50_km: 3.3, p90_km: 9.9, n: 82 }],
  mix: {
    available: true, skip: [{ value: "none", producer: "serving", n: 7 }],
    segment_kind: [{ value: "gc", producer: "serving", n: 5 }],
    uncertainty_bin: [{ value: "low", producer: "serving", n: 3 }],
  },
  outcomes: { settled: 5, awaiting: 4, ambiguous: 1 }, input_split: { provisional: 2, settled: 3 },
  ...over,
});
const GAPS = [[0, 60, 21], [60, 300, 14], [300, 900, 9], [900, 3600, 7], [3600, 10800, 5], [10800, 21600, 3],
  [21600, 43200, 2], [43200, null, 1]];
const coverageBody = (over: Record<string, unknown> = {}) => ({
  available: true,
  tier_daily: [["2026-07-28", { settled: 9, estimated: 7, unknown: 2 }], ["2026-07-29", { settled: 10, estimated: 6 }]],
  gap_bins: GAPS.map(([ge, lt, n]) => ({ ge, lt, n })),
  observed: [{ day: "2026-07-29", median: 0.934, n: 22 }, { day: "2026-07-30", median: 0.905, n: 22 }],
  ...over,
});

// one landed answer per feed, in the order the views create their resources
async function land(calls: ReturnType<typeof stubFetch>, ...bodies: unknown[]) {
  await act(async () => {
    bodies.forEach((b, i) => calls[i].ok(b));
    await flush();
  });
}
const supersede = () => act(() => {
  wb.value = { ...wb.value, range: "7d" }; // a new fetch key with no URL write: the views see superseded
});

describe("Overview", () => {
  const mount = () => {
    resetStore("/?wb=overview&wb_d=all");
    const calls = stubFetch();
    draw(<Overview />);
    return calls;
  };

  it("draws the strip, the four panels and both feeds", async () => {
    const calls = mount();
    expect(calls.map((c) => c.url.split("?")[0])).toEqual(["/workbench/summary", "/workbench/flags"]);
    expect(calls[1].url).toContain("limit=5");
    await land(calls, summaryBody(), flagsBody());
    expect(text(".wb-strip .wb-cell-k")).toEqual(["flights", "services", "aircraft", "flagged", "est err", "tiers"]);
    expect(text(".wb-strip .wb-cell-v")).toEqual(["1,234", "42", "88", "13", "3.21 km"]);
    expect(one(".wb-microbar")!.getAttribute("title")).toBe("settled 20 · estimated 5");
    expect(text(".wb-panel-head")).toEqual(["flags▸", "trends▸", "estimates▸", "coverage▸"]);
    expect(text(".wb-strip .wb-cell-go .wb-cell-k")).toEqual(["flights", "flagged"]);
    expect(one(".wb-inst .wb-flagcls")!.textContent).toBe("diversion");
    expect(text(".wb-classes .wb-chip")).toEqual(["diversion 3", "military 2"]);
    expect(text(".wb-rank .wb-name")).toEqual(["HND-ITM", "HND-CTS"]);
    expect(text(".wb-rank .wb-delta")).toEqual(["+10.0%", "−9.1%"]);
    expect(one(".wb-note")!.textContent).toBe("p50 3.21 km · n=162");
    // two sparks (trends, estimates) and the tier stack
    expect(charts().map((c) => c.className)).toEqual(["wb-spark", "wb-spark", "wb-chart"]);
    expect(charts()[2].dataset.series).toBe("settled|estimated");
  });

  it("an outage words the whole view unavailable", async () => {
    const calls = mount();
    await act(async () => {
      calls[0].notOk();
      calls[1].notOk();
      await flush();
    });
    expect(host.textContent).toBe("overview unavailable");
  });

  it("a flags outage leaves the panel standing with its own wording", async () => {
    const calls = mount();
    await act(async () => {
      calls[0].ok(summaryBody());
      calls[1].notOk();
      await flush();
    });
    expect(one(".wb-panel .wb-empty")!.textContent).toBe("flags unavailable");
    expect(one(".wb-strip")).not.toBeNull();
  });

  it("superseded keeps the sparks drawing and blanks the strip, rows and chips", async () => {
    const calls = mount();
    await land(calls, summaryBody(), flagsBody());
    const before = charts().map((c) => c.dataset.data);
    await supersede();
    expect(charts().map((c) => c.dataset.data)).toEqual(before);
    expect(one(".wb-strip")).toBeNull();
    expect(one(".wb-rank")).toBeNull();
    expect(one(".wb-classes")).toBeNull();
    expect(one(".wb-inst")).toBeNull();
    expect(text(".wb-panel-head")).toEqual(["flags▸", "trends▸", "estimates▸", "coverage▸"]);
  });

  const DOORWAYS: [string, number, Record<string, unknown>][] = [
    [".wb-strip .wb-cell-go", 0, { view: "log", od: null, flagClass: null, airline: null, page: 1 }],
    [".wb-strip .wb-cell-go", 1, { view: "flags", flagClass: null, od: null, page: 1 }],
    [".wb-classes .wb-chip", 0, { view: "flags", flagClass: "diversion", od: null, page: 1 }],
    [".wb-rank", 0, { view: "log", od: "HND-ITM", flagClass: null, airline: null, page: 1 }],
    [".wb-panel-head", 1, { view: "trends", dim: "route", flagClass: null, od: null, page: 1 }],
  ];
  for (const [sel, i, want] of DOORWAYS) {
    it(`doorway ${sel}[${i}] carries exactly its own scope`, async () => {
      const calls = mount();
      await land(calls, summaryBody(), flagsBody());
      await act(async () => host.querySelectorAll<HTMLButtonElement>(sel)[i].click());
      expect(wb.value).toMatchObject({ range: "all", ...want });
    });
  }
});

describe("Flags", () => {
  const mount = (url = "/?wb=flags&wb_d=all") => {
    resetStore(url);
    const calls = stubFetch();
    draw(<Flags />);
    return calls;
  };

  it("draws the chips, the feed and the pager", async () => {
    const calls = mount();
    expect(calls[0].url).toMatch(/^\/workbench\/flags\?/);
    await land(calls, flagsBody({ total: 66 }));
    expect(one(".wb-chip[data-cls='']")!.textContent).toBe("all 5");
    expect(one(".wb-chip[data-cls='']")!.getAttribute("aria-pressed")).toBe("true");
    expect(one(".wb-chip[data-cls='diversion']")!.textContent).toBe("diversion 3");
    expect(one(".wb-chip[data-cls='one_sided_intl']")!.textContent).toBe("one sided intl 0");
    expect(one(".wb-inst .wb-detail")!.textContent).toBe("dest RJNA vs modal RJGG");
    expect(one(".wb-pager .wb-count")!.textContent).toBe("1–1 of 66");
  });

  it("a class chip navigates with the class and the first page", async () => {
    const calls = mount("/?wb=flags&wb_d=all&wb_p=3");
    await land(calls, flagsBody());
    await act(async () => one<HTMLButtonElement>(".wb-chip[data-cls='diversion']")!.click());
    expect(wb.value).toMatchObject({ view: "flags", flagClass: "diversion", page: 1 });
  });

  it("an outage words the view unavailable", async () => {
    const calls = mount();
    await act(async () => {
      calls[0].notOk();
      await flush();
    });
    expect(host.textContent).toBe("flags unavailable");
  });

  it("a supersession blanks the chipless view entirely", async () => {
    const calls = mount();
    await land(calls, flagsBody());
    expect(one(".wb-inst")).not.toBeNull();
    await supersede();
    expect(host.textContent).toBe("");
  });
});

describe("Trends", () => {
  const mount = (url = "/?wb=trends&wb_d=all") => {
    resetStore(url);
    const calls = stubFetch();
    draw(<Trends />);
    return calls;
  };

  it("draws the dim chips, the top-5 line and the rank rows", async () => {
    const calls = mount();
    expect(calls[0].url).toContain("limit=20");
    await land(calls, trendsBody());
    expect(text(".wb-dims .wb-chip")).toEqual(["route", "airline", "airport"]);
    expect(one(".wb-dims .wb-chip[data-dim='route']")!.getAttribute("aria-pressed")).toBe("true");
    expect(charts()[0].dataset.series).toBe("HND-ITM|HND-CTS");
    // a key idle on a day is a gap, never a zero
    expect(JSON.parse(charts()[0].dataset.data!)[2]).toEqual([null, 3]);
    expect(one(".wb-rank .wb-name")!.textContent).toBe("HND-ITM");
    expect(one(".wb-rank .wb-row-sub")!.textContent).toBe("9 aircraft+10.0%");
  });

  it("a dim chip navigates with the dim and the first page", async () => {
    const calls = mount("/?wb=trends&wb_d=all&wb_p=2");
    await land(calls, trendsBody());
    await act(async () => one<HTMLButtonElement>(".wb-dims .wb-chip[data-dim='airline']")!.click());
    expect(wb.value).toMatchObject({ view: "trends", dim: "airline", page: 1 });
  });

  const PICKS: [string, Record<string, unknown>][] = [
    ["route", { view: "log", od: "HND-ITM" }],
    ["airline", { view: "drill", airline: "HND-ITM" }],
    ["airport", { view: "drill", apt: "HND-ITM" }],
  ];
  for (const [dim, want] of PICKS) {
    it(`a ${dim} rank row opens its own doorway`, async () => {
      const calls = mount(`/?wb=trends&wb_d=all&wb_dim=${dim}`);
      await land(calls, trendsBody({ dim }));
      await act(async () => one<HTMLButtonElement>(".wb-rank")!.click());
      expect(wb.value).toMatchObject(want);
    });
  }

  it("an outage keeps the chips it drew before the fetch", async () => {
    const calls = mount();
    await act(async () => {
      calls[0].notOk();
      await flush();
    });
    expect(one(".wb-dims")).not.toBeNull();
    expect(one(".wb-empty")!.textContent).toBe("trends unavailable");
  });

  it("a supersession keeps the line and the chips", async () => {
    const calls = mount();
    await land(calls, trendsBody());
    const before = charts()[0].dataset.data;
    await supersede();
    expect(charts()[0].dataset.data).toBe(before);
    // the chips read the URL, so a misclick stays correctable while the next fetch is in flight
    expect(one(".wb-dims .wb-chip[data-dim='route']")!.getAttribute("aria-pressed")).toBe("true");
    expect(one(".wb-rank")).toBeNull();
    expect(one(".wb-pager")).toBeNull();
  });
});

describe("Estimates", () => {
  const mount = () => {
    resetStore("/?wb=estimates&wb_d=all");
    const calls = stubFetch();
    draw(<Estimates />);
    return calls;
  };

  it("draws the eras, the p50/p90 arms, the strip and the mix panels", async () => {
    const calls = mount();
    await land(calls, estimatesBody());
    expect(text(".wb-sect")).toEqual(["config eras", "logging stream", "skips", "segment kind", "uncertainty bin"]);
    expect(one(".wb-era")!.className).toBe("wb-era wb-era-cur");
    expect(one(".wb-era")!.getAttribute("title")).toBe("config 2537707548349448576");
    expect(one(".wb-era .wb-name")!.textContent).toBe("25377075");
    expect(text(".wb-era .wb-era-p")).toEqual(["p50 3.21 km", "p90 9.87 km"]);
    expect(one(".wb-era .wb-row-sub")!.textContent).toBe("2026-07-29 → 2026-07-30n=162");
    expect(charts()[0].dataset.series).toBe("25377075 p50|25377075 p90");
    expect(text(".wb-strip .wb-cell-k")).toEqual(["settled", "awaiting", "ambiguous", "prov in", "settled in"]);
    expect(text(".wb-mix-skip .wb-kv")).toEqual(["noneserving7"]);
    expect([...host.querySelectorAll(".wb-note")].pop()!.textContent)
      .toBe("mix counts are UTC-day grain; the series above is JST");
  });

  it("an absent ledger says so", async () => {
    const calls = mount();
    await land(calls, estimatesBody({ available: false }));
    expect(host.textContent).toBe("estimate ledger not deployed");
  });

  it("an absent breakdown mart says so under its own section", async () => {
    const calls = mount();
    await land(calls, estimatesBody({ mix: { available: false } }));
    expect([...host.querySelectorAll(".wb-sect")].pop()!.textContent).toBe("mix");
    expect(one(".wb-empty")!.textContent).toBe("breakdown mart not deployed");
  });

  it("an outage words the view unavailable", async () => {
    const calls = mount();
    await act(async () => {
      calls[0].notOk();
      await flush();
    });
    expect(host.textContent).toBe("estimates unavailable");
  });

  it("a supersession keeps only the line", async () => {
    const calls = mount();
    await land(calls, estimatesBody());
    const before = charts()[0].dataset.data;
    await supersede();
    expect(charts()[0].dataset.data).toBe(before);
    expect(one(".wb-era")).toBeNull();
    expect(one(".wb-strip")).toBeNull();
    expect(one(".wb-mix-skip")).toBeNull();
  });
});

describe("Coverage", () => {
  const mount = () => {
    resetStore("/?wb=coverage&wb_d=all");
    const calls = stubFetch();
    draw(<Coverage />);
    return calls;
  };

  it("draws the three sections, the tier legend and the observed note", async () => {
    const calls = mount();
    await land(calls, coverageBody());
    expect(text(".wb-sect")).toEqual(["tier mix per day", "largest gap", "observed fraction (median)"]);
    expect(charts().map((c) => c.className)).toEqual(["wb-chart", "wb-chart", "wb-chart"]);
    expect(charts()[0].dataset.series).toBe("settled|estimated|unknown");
    expect(text(".wb-tmix.wb-legend:not(.wb-gapbins) span")).toEqual(["settled 19", "estimated 13", "unknown 2"]);
    expect(text(".wb-note")).toEqual(["15m is the settled/estimated tier seam", "2026-07-30 90.5% · n=22"]);
  });

  // the axis labels live on canvas; the legend is the same binLabel() in the DOM, so they cannot drift
  it("the gap legend reads one span per bin, label and count", async () => {
    const calls = mount();
    await land(calls, coverageBody());
    expect(text(".wb-gapbins span")).toEqual([
      "<1m 21", "1–5m 14", "5–15m 9", "15m–1h 7", "1–3h 5", "3–6h 3", "6–12h 2", "≥12h 1",
    ]);
  });

  // open at both ends spans every gap there is, so naming a range would state something untrue
  it("a bin open at both ends reads as the no-value glyph", async () => {
    const calls = mount();
    await land(calls, coverageBody({ gap_bins: [{ ge: 0, lt: null, n: 5 }] }));
    expect(text(".wb-gapbins span")).toEqual(["— 5"]);
  });

  it("an outage words the view unavailable", async () => {
    const calls = mount();
    await act(async () => {
      calls[0].notOk();
      await flush();
    });
    expect(host.textContent).toBe("coverage unavailable");
  });

  it("a supersession keeps only the charts", async () => {
    const calls = mount();
    await land(calls, coverageBody());
    const before = charts().map((c) => c.dataset.data);
    await supersede();
    expect(charts().map((c) => c.dataset.data)).toEqual(before);
    expect(one(".wb-legend")).toBeNull();
    expect(text(".wb-note")).toEqual([]);
    expect(text(".wb-sect")).toEqual(["tier mix per day", "largest gap", "observed fraction (median)"]);
  });
});
