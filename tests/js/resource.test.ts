import { describe, expect, it } from "vitest";
import { signal } from "@preact/signals";
import { resource } from "../../livemap/src/features/workbench/resource";
import { flush as tick } from "./_support.mjs";

// one deferred answer per fetch, so each case decides when (and whether) an answer lands
function harness() {
  const params = signal<{ q: string } | null>({ q: "a" });
  const calls: { params: unknown; signal: AbortSignal; ok: (v: string | null) => void; boom: () => void }[] = [];
  const res = resource<string, { q: string }>(() => params.value, (p, sig) =>
    new Promise((resolve, reject) => calls.push({ params: p, signal: sig, ok: resolve, boom: reject })));
  const state = () => res.value.value.state;
  return { params, calls, res, state };
}

describe("resource()", () => {
  it("first key: idle until the answer, then ok with the parsed params", async () => {
    const h = harness();
    expect(h.state()).toBe("idle");
    expect(h.calls).toHaveLength(1);
    expect(h.calls[0].params).toEqual({ q: "a" });
    h.calls[0].ok("A");
    await tick();
    expect(h.res.value.value).toEqual({ state: "ok", data: "A" });
  });

  it("a key change is superseded at once (carrying the last envelope); a retired answer never lands, the new one does", async () => {
    const h = harness();
    h.calls[0].ok("A");
    await tick();
    h.params.value = { q: "b" };
    expect(h.res.value.value).toEqual({ state: "superseded", prev: "A" }); // synchronous, and the old page stays drawable
    expect(h.calls[0].signal.aborted).toBe(true);
    expect(h.calls).toHaveLength(2);
    // supersede AGAIN while b is still pending: b's answer must not land after c has taken over
    h.params.value = { q: "c" };
    expect(h.res.value.value).toEqual({ state: "superseded", prev: "A" });
    expect(h.calls).toHaveLength(3);
    h.calls[1].ok("B-late");
    await tick();
    expect(h.res.value.value).toEqual({ state: "superseded", prev: "A" }); // the retired request's answer was dropped
    h.calls[2].ok("C");
    await tick();
    expect(h.res.value.value).toEqual({ state: "ok", data: "C" });
  });

  it("an equal key never re-fetches; a null key parks at idle", () => {
    const h = harness();
    h.params.value = { q: "a" };
    expect(h.calls).toHaveLength(1);
    h.params.value = null;
    expect(h.state()).toBe("idle");
    expect(h.calls).toHaveLength(1);
  });

  // the fetcher contract is T | null, so an empty string or a zero is an answer, not an outage
  it("a falsy non-null payload is ok: zero and the empty string both land", async () => {
    const params = signal<{ q: string } | null>({ q: "a" });
    let land: (v: number | null) => void = () => {};
    const res = resource<number, { q: string }>(() => params.value, () =>
      new Promise((resolve) => (land = resolve)));
    land(0);
    await tick();
    expect(res.value.value).toEqual({ state: "ok", data: 0 });

    res.dispose();

    const h = harness();
    h.calls[0].ok("");
    await tick();
    expect(h.res.value.value).toEqual({ state: "ok", data: "" });
  });

  it("null answer is outage, a rejection is unreachable, dispose orphans everything", async () => {
    const h = harness();
    h.calls[0].ok(null);
    await tick();
    expect(h.state()).toBe("outage");
    h.params.value = { q: "c" };
    h.calls[1].boom();
    await tick();
    expect(h.state()).toBe("unreachable");
    h.params.value = { q: "d" };
    h.res.dispose();
    expect(h.calls[2].signal.aborted).toBe(true);
    h.calls[2].ok("D");
    await tick();
    expect(h.res.value.value).toEqual({ state: "superseded" }); // frozen where dispose found it (no ok to carry) — nothing lands after
    h.params.value = { q: "e" };
    expect(h.calls).toHaveLength(3); // the effect is gone
  });
});
