// Test doubles shared by the facade / focus / deep-link harnesses (not matched by the *.test.mjs glob).

// one deferred answer per fetch, so a test decides when (and whether) a call lands
export function stubFetch() {
  const calls = [];
  globalThis.fetch = (url) => {
    let settle, fail;
    const p = new Promise((res, rej) => { settle = res; fail = rej; });
    calls.push({
      url,
      ok: (body) => settle({ ok: true, json: async () => body }),
      notOk: () => settle({ ok: false, json: async () => ({}) }),
      boom: () => fail(new Error("offline")),
    });
    return p;
  };
  return calls;
}

// a recording map facade whose showFlightPath answers only when a test resolves it
export function spyFacade() {
  const calls = [];
  const answers = [];
  return {
    calls,
    answers,
    clearPath: () => calls.push("clearPath"),
    dimLive: (x) => calls.push(`dimLive:${x}`),
    guardMapClicks: (on) => calls.push(`guard:${on}`),
    clearSelection: () => calls.push("clearSelection"),
    showFlightPath: (fid, opts = {}) => {
      calls.push(`show:${fid}:${opts.fit}`);
      return new Promise((resolve) => answers.push(resolve));
    },
  };
}

export const flush = () => new Promise((r) => setTimeout(r, 0));
