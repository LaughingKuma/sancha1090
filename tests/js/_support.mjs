// Test doubles shared by the facade / focus / deep-link harnesses (not matched by the *.test.mjs glob).

// one deferred answer per fetch, so a test decides when (and whether) a call lands; `signal` is the
// caller's AbortSignal (or undefined), and `abort()` rejects the way a real fetch does once it fires
export function stubFetch() {
  const calls = [];
  globalThis.fetch = (url, init = {}) => {
    let settle, fail;
    const p = new Promise((res, rej) => { settle = res; fail = rej; });
    calls.push({
      url,
      signal: init.signal,
      ok: (body) => settle({ ok: true, json: async () => body }),
      notOk: () => settle({ ok: false, json: async () => ({}) }),
      boom: () => fail(new Error("offline")),
      abort: () => fail(new DOMException("aborted", "AbortError")),
    });
    return p;
  };
  return calls;
}

// a recording map facade whose showFlightPath answers only when a test resolves (or rejects) it
export function spyFacade() {
  const calls = [];
  const answers = [];
  const rejects = [];
  return {
    calls,
    answers,
    rejects,
    clearPath: () => calls.push("clearPath"),
    dimLive: (x) => calls.push(`dimLive:${x}`),
    guardMapClicks: (on) => calls.push(`guard:${on}`),
    clearSelection: () => calls.push("clearSelection"),
    showFlightPath: (fid, opts = {}) => {
      calls.push(`show:${fid}:${opts.fit}`);
      return new Promise((resolve, reject) => {
        answers.push(resolve);
        rejects.push(reject);
      });
    },
  };
}

export const flush = () => new Promise((r) => setTimeout(r, 0));
