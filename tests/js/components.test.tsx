import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render } from "preact";
import { act } from "preact/test-utils";
import { EPOCH, KEY, flush, inst, resetStore, stubFetch } from "./_fixtures";
import type { FlagInstance } from "../../livemap/src/features/workbench/data";
import { useResource } from "../../livemap/src/features/workbench/resource";
import { focus, navigate, status, wb } from "../../livemap/src/features/workbench/store";
import { InstanceList } from "../../livemap/src/features/workbench/components/InstanceList";
import { RowList } from "../../livemap/src/features/workbench/components/RowList";
import { Pager } from "../../livemap/src/features/workbench/components/Pager";
import { Empty } from "../../livemap/src/features/workbench/components/Empty";
import { ViewTabs } from "../../livemap/src/features/workbench/components/ViewTabs";
import { RangeChips } from "../../livemap/src/features/workbench/components/RangeChips";
import { FocusBar } from "../../livemap/src/features/workbench/components/FocusBar";
import { Log } from "../../livemap/src/features/workbench/components/Log";
import { Drill } from "../../livemap/src/features/workbench/components/Drill";
import { Search } from "../../livemap/src/features/workbench/components/Search";

let host: HTMLDivElement;
const draw = (node: unknown) => act(() => render(node as never, host));

beforeEach(() => {
  resetStore();
  host = document.body.appendChild(document.createElement("div"));
});
afterEach(() => {
  act(() => render(null, host));
  host.remove();
  vi.restoreAllMocks(); // here, not at a test's tail — a failed assertion must not leak the stub
});

const rows = () => [...host.querySelectorAll<HTMLButtonElement>(".wb-inst")];

describe("InstanceList", () => {
  it("renders the vanilla row contract and focuses on click", async () => {
    draw(<InstanceList rows={[inst(), inst({ key: "x.1.B", callsign: "ANA2", mil: true, tier: "none", flightId: null })]} />);
    expect(rows()).toHaveLength(2);
    expect(rows()[0].querySelector(".wb-date")!.textContent).toBe("07-29");
    expect(rows()[0].querySelector(".wb-name")!.textContent).toBe("ANA1");
    expect(rows()[0].querySelector(".wb-sub-od")!.textContent).toBe("HND → ITM");
    expect(rows()[0].querySelector(".wb-tier")!.textContent).toBe("● STL");
    expect(rows()[0].getAttribute("title")).toContain("All Nippon Airways");
    expect(rows()[1].querySelector(".t-mil")!.textContent).toBe("MIL");
    await act(async () => {
      rows()[0].click();
      await flush();
    });
    expect(wb.value.inst).toBe(KEY);
  });

  it("the active row lights from the store, with no re-render of the list itself", async () => {
    draw(<InstanceList rows={[inst(), inst({ key: "40aa11.1.JAL2", callsign: "JAL2" })]} />);
    const before = rows()[0];
    await act(async () => navigate({ inst: KEY }));
    expect(rows().map((r) => r.classList.contains("wb-active"))).toEqual([true, false]);
    expect(rows()[0].getAttribute("aria-pressed")).toBe("true");
    expect(rows()[0]).toBe(before); // the row element itself survived — only its class moved
    await act(async () => navigate({ inst: null }));
    expect(host.querySelectorAll(".wb-active")).toHaveLength(0);
  });

  it("a tier-none row flashes in place instead of claiming focus", async () => {
    draw(<InstanceList rows={[inst({ tier: "none" })]} />);
    await act(async () => {
      rows()[0].click();
      await flush();
    });
    expect(wb.value.inst).toBeNull();
    const od = rows()[0].querySelector(".wb-sub-od")!;
    expect(od.classList.contains("wb-nopath")).toBe(true);
    expect(od.textContent).toBe("no recorded path");
    expect(status.value).toBe("no recorded path");
  });

  it("the flag feed swaps the middle cells and keeps the click", () => {
    const row: FlagInstance = { ...inst(), flagClass: "same_endpoint", detail: "dest RJNA vs modal RJGG" };
    draw(<InstanceList rows={[row]} flag />);
    expect(rows()[0].querySelector(".wb-flagcls")!.textContent).toBe("same endpoint");
    expect(rows()[0].querySelector(".wb-n")!.textContent).toBe("HND → ITM");
    expect(rows()[0].querySelector(".wb-detail")!.textContent).toBe("dest RJNA vs modal RJGG");
    expect(rows()[0].getAttribute("title")).toBeNull();
  });

  // vanilla's flashRow looked for a .wb-sub-od the flag rows do not have, so the message only reached
  // the status line; the flash is keyed by row now, so it lands in the cell the row actually has
  it("a no-path click on a flag row flashes in its own detail cell", async () => {
    const row: FlagInstance = {
      ...inst({ tier: "none" }), flagClass: "diversion", detail: "dest RJNA vs modal RJGG",
    };
    draw(<InstanceList rows={[row]} flag />);
    await act(async () => {
      rows()[0].click();
      await flush();
    });
    const cell = rows()[0].querySelector(".wb-detail")!;
    expect(cell.classList.contains("wb-nopath")).toBe(true);
    expect(cell.textContent).toBe("no recorded path");
    expect(rows()[0].querySelector(".wb-sub-od")).toBeNull();
    expect(status.value).toBe("no recorded path");
  });

  // the flags feed emits one row per (flight, class): one shared key would collapse the rows and one
  // shared flash would light every class row of the clicked flight
  it("two flag rows of one flight with different classes render separately and only the clicked row flashes", async () => {
    const feed: FlagInstance[] = [
      { ...inst({ tier: "none" }), flagClass: "diversion", detail: "first" },
      { ...inst({ tier: "none" }), flagClass: "same_endpoint", detail: "second" },
    ];
    draw(<InstanceList rows={feed} flag />);
    expect(rows()).toHaveLength(2);
    await act(async () => {
      rows()[1].click();
      await flush();
    });
    const cells = rows().map((r) => r.querySelector(".wb-detail")!);
    expect(cells[1].classList.contains("wb-nopath")).toBe(true);
    expect(cells[1].textContent).toBe("no recorded path");
    expect(cells[0].classList.contains("wb-nopath")).toBe(false);
    expect(cells[0].textContent).toBe("first");
  });

  it("an empty feed says so in its own words", () => {
    draw(<InstanceList rows={[]} />);
    expect(host.querySelector(".wb-empty")!.textContent).toBe("no instances match");
    draw(<InstanceList rows={[]} flag />);
    expect(host.querySelector(".wb-empty")!.textContent).toBe("no flagged instances");
  });
});

describe("RowList", () => {
  it("renders name/count/sub/tiers/delta and dispatches by index", () => {
    const picked: number[] = [];
    draw(
      <RowList
        rows={[
          { key: "a", name: "All Nippon Airways", n: "1,234", sub: "12 svc", tiers: { settled: 3, none: 1 } },
          { key: "b", name: "HND-ITM", n: "44", sub: "9 aircraft", delta: "+1.0%", cls: "wb-rank" },
        ]}
        onPick={(i) => picked.push(i)}
        empty="no airlines"
      />,
    );
    const list = [...host.querySelectorAll<HTMLButtonElement>(".wb-row")];
    expect(list[0].querySelector(".wb-name")!.textContent).toBe("All Nippon Airways");
    expect(list[0].querySelector(".wb-n")!.textContent).toBe("1,234");
    expect(list[0].querySelector(".wb-tmix")!.getAttribute("title")).toBe("settled 3 · none 1");
    expect(list[1].classList.contains("wb-rank")).toBe(true);
    expect(list[1].querySelector(".wb-delta")!.textContent).toBe("+1.0%");
    list[1].click();
    expect(picked).toEqual([1]);
  });

  it("an empty list keeps the caller's wording", () => {
    draw(<RowList rows={[]} onPick={() => {}} empty="no trends in range" />);
    expect(host.querySelector(".wb-empty")!.textContent).toBe("no trends in range");
  });

  // the callers key rows on a name (`r.name`/`r.callsign`), which repeats and can be blank
  it("rows with a repeated or blank name all render and still dispatch by index", () => {
    const picked: number[] = [];
    draw(
      <RowList
        rows={[{ key: "", name: "", n: "1" }, { key: "ANA1", name: "ANA1", n: "2" },
          { key: "ANA1", name: "ANA1", n: "3" }]}
        onPick={(i) => picked.push(i)}
        empty="no services"
      />,
    );
    const list = [...host.querySelectorAll<HTMLButtonElement>(".wb-row")];
    expect(list.map((b) => b.querySelector(".wb-n")!.textContent)).toEqual(["1", "2", "3"]);
    expect(list[0].querySelector(".wb-name")!.textContent).toBe("—");
    list[2].click();
    expect(picked).toEqual([2]);
  });
});

describe("Pager", () => {
  it("hides on a single page, walks the URL, and disables at the ends", async () => {
    draw(<Pager p={{ rows: Array(3).fill(0), total: 3, offset: 0 }} />);
    expect(host.querySelector(".wb-pager")).toBeNull();
    draw(<Pager p={{ rows: Array(50).fill(0), total: 66, offset: 0 }} />);
    expect(host.querySelector(".wb-count")!.textContent).toBe("1–50 of 66");
    expect(host.querySelector<HTMLButtonElement>(".wb-chip[data-step='-1']")!.disabled).toBe(true);
    await act(async () => host.querySelector<HTMLButtonElement>(".wb-chip[data-step='1']")!.click());
    expect(wb.value.page).toBe(2);
    expect(new URLSearchParams(location.search).get("wb_p")).toBe("2");
    draw(<Pager p={{ rows: Array(16).fill(0), total: 66, offset: 50 }} />);
    expect(host.querySelector(".wb-count")!.textContent).toBe("51–66 of 66");
    expect(host.querySelector<HTMLButtonElement>(".wb-chip[data-step='1']")!.disabled).toBe(true);
  });
});

describe("Empty", () => {
  it("words an outage before an empty window", () => {
    draw(<Empty what="instances" state="outage" />);
    expect(host.textContent).toBe("instances unavailable");
    draw(<Empty what="instances" state="unreachable" />);
    expect(host.textContent).toBe("instances unavailable");
    draw(<Empty what="instances" state="ok" empty="no instances match" />);
    expect(host.textContent).toBe("no instances match");
    // a caller with only a sentence (RowList) must never have it read as a noun: "no trends in range unavailable"
    draw(<Empty empty="no trends in range" state="outage" />);
    expect(host.textContent).toBe("unavailable");
  });
});

describe("ViewTabs", () => {
  it("marks the active tab and opens a doorway that drops what the target cannot undo", async () => {
    await act(async () => navigate({ view: "log", mil: true, airline: "ANA", flagClass: null }));
    draw(<ViewTabs />);
    const tab = (v: string) => host.querySelector<HTMLButtonElement>(`.wb-view[data-view="${v}"]`)!;
    expect(tab("log").getAttribute("aria-selected")).toBe("true");
    await act(async () => tab("flags").click());
    expect(wb.value).toMatchObject({ view: "flags", mil: false, airline: null, page: 1 });
    expect(tab("flags").getAttribute("aria-selected")).toBe("true");
  });
});

describe("RangeChips", () => {
  it("presets rewrite wb_d; the custom row opens, prefills and applies", async () => {
    draw(<RangeChips />);
    const chip = (sel: string) => host.querySelector<HTMLButtonElement>(sel)!;
    await act(async () => chip('.wb-chip[data-range="7d"]').click());
    expect(wb.value.range).toBe("7d");
    expect(chip('.wb-chip[data-range="7d"]').getAttribute("aria-pressed")).toBe("true");
    expect(host.querySelector<HTMLElement>(".wb-custom")!.hidden).toBe(true);
    await act(async () => chip(".wb-chip[data-custom]").click());
    expect(host.querySelector<HTMLElement>(".wb-custom")!.hidden).toBe(false);
    const [from, to] = [...host.querySelectorAll<HTMLInputElement>(".wb-custom input")];
    from.value = "2026-07-28";
    to.value = "2026-07-30";
    await act(async () => chip(".wb-chip[data-apply]").click());
    expect(wb.value.range).toBe("2026-07-28..2026-07-30");
    expect(chip(".wb-chip[data-custom]").getAttribute("aria-pressed")).toBe("true");
    expect([...host.querySelectorAll<HTMLInputElement>(".wb-custom input")].map((i) => i.value))
      .toEqual(["2026-07-28", "2026-07-30"]);
    await act(async () => chip('.wb-chip[data-range="all"]').click());
    expect(host.querySelector<HTMLElement>(".wb-custom")!.hidden).toBe(true);
  });

  // the chips subscribe to the range alone: a row click or a pager step must not re-render them, because a
  // re-render re-applies value= over whatever the user has typed but not yet applied
  it("a half-typed custom date survives a focus click and a pager step", async () => {
    draw(<RangeChips />);
    const start = host.querySelector<HTMLInputElement>('input[aria-label="Range start"]')!;
    start.value = "2026-08-01";
    await act(async () => navigate({ inst: KEY }));
    await act(async () => navigate({ page: 3 }));
    expect(start.value).toBe("2026-08-01");
    await act(async () => navigate({ range: "7d" }));
    expect(start.value).toBe(""); // the range itself moving is the one re-sync that is wanted
  });
});

describe("FocusBar", () => {
  it("appears with the claim, fills in the point count, and Escape leaves focus", async () => {
    draw(<FocusBar />);
    expect(host.querySelector(".wb-focus")).toBeNull();
    await act(async () => {
      focus.value = { inst: inst(), n: null };
    });
    expect(host.querySelector(".wb-cs")!.textContent).toBe("ANA1");
    expect(host.querySelector(".wb-tier")!.textContent).toBe("● STL");
    expect([...host.querySelectorAll(".wb-meta")].pop()!.textContent).toBe("loading path…");
    await act(async () => {
      focus.value = { inst: inst(), n: 1234 };
    });
    expect([...host.querySelectorAll(".wb-meta")].pop()!.textContent).toBe("1,234 pts");
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(host.querySelector(".wb-focus")).toBeNull();
  });
});

// the lifecycle the spike's read-only review pass rated material: the resource must be created by a COMMITTED effect
const params = () => ({ q: "a" });
let seen: { signal: AbortSignal }[] = [];
const fetcher = (_p: unknown, signal: AbortSignal) => {
  seen.push({ signal });
  return new Promise<string | null>(() => {});
};
const Probe = () => {
  const st = useResource(params, fetcher);
  return <span>{st.state}</span>;
};

describe("useResource", () => {
  beforeEach(() => {
    seen = [];
  });

  it("fetches once the mount commits and aborts on unmount", () => {
    draw(<Probe />);
    expect(seen).toHaveLength(1);
    expect(seen[0].signal.aborted).toBe(false);
    act(() => render(null, host));
    expect(seen[0].signal.aborted).toBe(true);
  });

  it("a mount and unmount inside one frame never creates a resource", () => {
    act(() => {
      render(<Probe />, host);
      render(null, host);
    });
    expect(seen).toEqual([]);
  });

  it("an equal key across re-renders does not re-fetch", () => {
    draw(<Probe />);
    draw(<Probe />);
    expect(seen).toHaveLength(1);
  });
});

const typeInto = (el: HTMLInputElement, v: string) =>
  act(() => {
    el.value = v;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// one instance wire row (the fields data.ts coerces) so the list mints a real key and O/D
const wireRow = {
  icao24: "86D3A1", day: "2026-07-29", start_ts: EPOCH, end_ts: EPOCH + 3600, callsign: "ANA1",
  airline: "All Nippon Airways", registration: "JA801A", typecode: "B788",
  origin: { icao: "RJTT", iata: "HND", city: "Tokyo" }, dest: { icao: "RJOO", iata: "ITM", city: "Osaka" },
  tier: "settled", n_points: 340, flight_id: "999",
};
const instBody = (over: Record<string, unknown> = {}) => ({
  instances: [wireRow], total: 66, limit: 50, offset: 0, od_breakdown: [], military_filter_available: true, ...over,
});

describe("Log", () => {
  it("draws the filter bar and the fetched list with a pager", async () => {
    const calls = stubFetch();
    draw(<Log />);
    expect(host.querySelector(".wb-filters")).not.toBeNull();
    expect(calls[0].url).toMatch(/^\/workbench\/instances\?/);
    await act(async () => {
      calls[0].ok(instBody());
      await flush();
    });
    expect(host.querySelectorAll(".wb-inst")).toHaveLength(1);
    expect(host.querySelector(".wb-inst .wb-sub-od")!.textContent).toBe("HND → ITM");
    expect(host.querySelector(".wb-pager .wb-count")!.textContent).toBe("1–1 of 66");
  });

  it("an outage envelope words the list unavailable, keeping the bar", async () => {
    const calls = stubFetch();
    draw(<Log />);
    await act(async () => {
      calls[0].notOk();
      await flush();
    });
    expect(host.querySelector(".wb-filters")).not.toBeNull();
    expect(host.querySelector(".wb-empty")!.textContent).toBe("instances unavailable");
  });

  // the #185 hazard on the log's own inputs: a re-render behind value= would wipe half-typed apt/type text,
  // so the bar must not subscribe to inst or page — only an unrelated field moving must leave the box alone
  it("a half-typed airport and type survive an unrelated focus click and pager step", async () => {
    stubFetch();
    draw(<Log />);
    const apt = host.querySelector<HTMLInputElement>(".wb-f-apt")!;
    const type = host.querySelector<HTMLInputElement>(".wb-f-type")!;
    apt.value = "ct"; // typed, not yet committed (no change/Enter)
    type.value = "a32";
    await act(async () => navigate({ inst: KEY }));
    await act(async () => navigate({ page: 3 }));
    expect(apt.value).toBe("ct");
    expect(type.value).toBe("a32");
    // committing uppercases and replaces the entry — the vanilla contract
    await act(async () => {
      apt.value = "cts";
      apt.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(wb.value).toMatchObject({ apt: "CTS", type: "A32", page: 1 });
  });

  it("the day input rewrites the range to a single JST day", async () => {
    stubFetch();
    draw(<Log />);
    const day = host.querySelector<HTMLInputElement>(".wb-f-day")!;
    await act(async () => {
      day.value = "2026-07-29";
      day.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(wb.value.range).toBe("2026-07-29..2026-07-29");
  });
});

describe("Drill", () => {
  it("airlines level: section, crumbs and a row that drills in", async () => {
    resetStore("/?wb=drill&wb_d=all");
    const calls = stubFetch();
    draw(<Drill />);
    expect(calls[0].url).toMatch(/^\/workbench\/airlines\?/);
    await act(async () => {
      calls[0].ok({
        airlines: [{ name: "All Nippon Airways", n_flights: 1234, n_services: 12, first_day: "2026-07-01", last_day: "2026-07-29", tiers: { settled: 3 } }],
        total: 1,
      });
      await flush();
    });
    expect(host.querySelector(".wb-sect")!.textContent).toBe("airlines");
    expect(host.querySelector(".wb-crumb")!.textContent).toBe("all");
    expect(host.querySelector(".wb-row .wb-name")!.textContent).toBe("All Nippon Airways");
    await act(async () => host.querySelector<HTMLButtonElement>(".wb-row")!.click());
    expect(wb.value).toMatchObject({ view: "drill", airline: "All Nippon Airways" });
  });

  it("instances level: od chips toggle the filter", async () => {
    resetStore("/?wb=drill&wb_svc=ANA1&wb_d=all");
    const calls = stubFetch();
    draw(<Drill />);
    expect(calls[0].url).toMatch(/^\/workbench\/instances\?/);
    await act(async () => {
      calls[0].ok(instBody({ instances: [], od_breakdown: [{ o: "HND", d: "ITM", n: 5 }] }));
      await flush();
    });
    expect(host.querySelector(".wb-sect")!.textContent).toBe("instances");
    const chip = host.querySelector<HTMLButtonElement>(".wb-odchips .wb-chip")!;
    expect(chip.textContent).toBe("HND-ITM 5");
    expect(chip.getAttribute("aria-pressed")).toBe("false");
    await act(async () => chip.click());
    expect(wb.value).toMatchObject({ od: "HND-ITM", page: 1 });
  });
});

describe("Search", () => {
  it("a debounced query opens the grouped dropdown and Enter drills into the first hit", async () => {
    const calls = stubFetch();
    draw(<Search />);
    const q = host.querySelector<HTMLInputElement>(".wb-q")!;
    await typeInto(q, "ja");
    await sleep(220); // clear the 180 ms debounce, then the search fetch is in flight
    await act(async () => {
      calls[0].ok({
        airlines: [{ name: "Japan Airlines", n_flights: 100 }],
        services: [{ callsign: "JAL101", airline: "Japan Airlines", n_instances: 5 }],
        airframes: [], airports: [],
      });
      await flush();
    });
    const drop = host.querySelector<HTMLElement>(".wb-drop")!;
    expect(drop.hidden).toBe(false);
    expect(q.getAttribute("aria-expanded")).toBe("true");
    expect([...drop.querySelectorAll(".wb-group")].map((g) => g.textContent)).toEqual(["airlines", "services"]);
    await act(async () => {
      q.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    });
    expect(wb.value).toMatchObject({ view: "drill", airline: "Japan Airlines" });
    expect(q.value).toBe("");
    expect(host.querySelector<HTMLElement>(".wb-drop")!.hidden).toBe(true);
  });

  // Escape dismisses the search and must not bubble to the global focus-exit listener on window
  it("Escape closes the box and stops propagation", async () => {
    stubFetch();
    draw(<Search />);
    const q = host.querySelector<HTMLInputElement>(".wb-q")!;
    let bubbled = false;
    const onWin = () => (bubbled = true);
    window.addEventListener("keydown", onWin);
    try {
      await act(async () => {
        q.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      });
      expect(bubbled).toBe(false);
      expect(q.getAttribute("aria-expanded")).toBe("false");
    } finally {
      window.removeEventListener("keydown", onWin);
    }
  });
});

// guard the one test double the suite leans on: an unanswered fetch must never resolve
it("the deferred fetch stub answers only when a test says so", async () => {
  const calls = stubFetch();
  let landed = false;
  fetch("/x").then(() => (landed = true));
  await flush();
  expect(landed).toBe(false);
  calls[0].ok({});
  await flush();
  expect(landed).toBe(true);
});
