import { test, afterEach } from "node:test";
import assert from "node:assert/strict";
import { createMapFacade } from "../../livemap/static/facade.js";
import { stubFetch, restoreFetch } from "./_support.mjs";

afterEach(restoreFetch);

// bounds default to a box the fixture path sits inside, so fit only fires where a test asks for it
function setup({ bounds = [100, 200, 0, 60], fitThrows = false } = {}) {
  const S = {
    pathFetchSeq: 0, histFlightId: "prior", histPathN: 7, histProvisional: true, histPts: [], dimLive: 0,
    mapClickGuard: false,
  };
  const fits = [];
  let selCleared = 0;
  let histCleared = 0;
  const map = {
    getBounds: () => ({
      getWest: () => bounds[0], getEast: () => bounds[1], getSouth: () => bounds[2], getNorth: () => bounds[3],
    }),
    fitBounds: (b, opts) => {
      if (fitThrows) throw new Error("bad bounds");
      fits.push({ b, opts });
    },
  };
  const setHistPath = (raw) => {
    S.histPts = (raw || []).map(([lon, lat, ts]) => ({ lon, lat, ts }));
    return S.histPts.length;
  };
  const api = createMapFacade({
    S, map, setHistPath,
    clearHistPath: () => { histCleared++; S.histPts = []; S.histFlightId = null; },
    clearSelection: () => selCleared++,
  });
  return { S, api, fits, sel: () => selCleared, cleared: () => histCleared };
}

const PTS = [[120, 30, 1], [130, 35, 2], [140, 40, 3]];

test("showFlightPath claims the pipeline synchronously, before any await", () => {
  const { S, api, cleared } = setup();
  stubFetch();
  api.showFlightPath("42");
  assert.equal(S.pathFetchSeq, 1);
  assert.equal(S.histFlightId, null);
  assert.equal(S.histPathN, 0);
  assert.equal(cleared(), 1); // the prior path drops now; the answer fills it back in
});

test("a drawn path reports ok and installs its point count", async () => {
  const { S, api } = setup();
  const calls = stubFetch();
  const p = api.showFlightPath("42");
  assert.equal(calls[0].url, "/path/42");
  calls[0].ok({ points: PTS });
  assert.deepEqual(await p, { status: "ok", n: 3 });
  assert.equal(S.histPathN, 3);
  assert.equal(S.histProvisional, false);
});

test("provisional arms the badge only when something was drawn", async () => {
  const drawn = setup();
  let calls = stubFetch();
  const a = drawn.api.showFlightPath("42");
  calls[0].ok({ points: PTS, provisional: true });
  await a;
  assert.equal(drawn.S.histProvisional, true);

  const empty = setup();
  calls = stubFetch();
  const b = empty.api.showFlightPath("42");
  calls[0].ok({ points: [], provisional: true });
  assert.deepEqual(await b, { status: "empty", n: 0 });
  assert.equal(empty.S.histProvisional, false);
});

test("fit frames the journey only when an endpoint is off-screen", async () => {
  const away = setup({ bounds: [0, 10, 0, 10] });
  let calls = stubFetch();
  const a = away.api.showFlightPath("42", { fit: true });
  calls[0].ok({ points: PTS });
  await a;
  assert.equal(away.fits.length, 1);
  assert.deepEqual(away.fits[0].b, [[120, 30], [140, 40]]);
  assert.deepEqual(away.fits[0].opts, { padding: 80, maxZoom: 11, duration: 700 });

  const inView = setup();
  calls = stubFetch();
  const b = inView.api.showFlightPath("42", { fit: true });
  calls[0].ok({ points: PTS });
  await b;
  assert.equal(inView.fits.length, 0);
});

test("an antimeridian path fits the short way round", async () => {
  const { api, fits } = setup({ bounds: [0, 10, 0, 10] });
  const calls = stubFetch();
  const p = api.showFlightPath("42", { fit: true });
  calls[0].ok({ points: [[170, 30, 1], [-170, 35, 2]] });
  await p;
  assert.deepEqual(fits[0].b, [[170, 30], [190, 35]]);
});

test("a path with no points is empty, not a failure", async () => {
  const { S, api } = setup();
  const calls = stubFetch();
  const p = api.showFlightPath("42");
  calls[0].ok({ points: [] });
  assert.deepEqual(await p, { status: "empty", n: 0 });
  assert.equal(S.histPathN, 0);
});

test("an HTTP error and an unreachable server both read as failed", async () => {
  const bad = setup();
  let calls = stubFetch();
  const a = bad.api.showFlightPath("42");
  calls[0].notOk();
  assert.deepEqual(await a, { status: "failed", n: 0 });

  const gone = setup();
  calls = stubFetch();
  const b = gone.api.showFlightPath("42");
  calls[0].boom();
  assert.deepEqual(await b, { status: "failed", n: 0 });
});

test("a malformed body is failed, never a rejection", async () => {
  const { api } = setup();
  const calls = stubFetch();
  const p = api.showFlightPath("42");
  calls[0].ok({ points: {} });
  assert.deepEqual(await p, { status: "failed", n: 0 });
  const rows = stubFetch();
  const q = api.showFlightPath("43");
  rows[0].ok({ points: [1] }); // an array of non-rows throws inside the installer
  assert.deepEqual(await q, { status: "failed", n: 0 });
});

test("a map error while framing keeps the drawn path", async () => {
  const { S, api } = setup({ bounds: [0, 10, 0, 10], fitThrows: true });
  const calls = stubFetch();
  const p = api.showFlightPath("42", { fit: true });
  calls[0].ok({ points: PTS });
  assert.deepEqual(await p, { status: "ok", n: 3 });
  assert.equal(S.histPathN, 3);
});

test("a second request supersedes the first, which touches nothing on arrival", async () => {
  const { S, api } = setup();
  const calls = stubFetch();
  const first = api.showFlightPath("1");
  assert.equal(calls[0].signal.aborted, false);
  const second = api.showFlightPath("2");
  assert.equal(calls[0].signal.aborted, true); // the superseded download is cut, not just its answer
  assert.equal(calls[1].signal.aborted, false);
  calls[1].ok({ points: PTS });
  assert.deepEqual(await second, { status: "ok", n: 3 });
  calls[0].ok({ points: [[0, 0, 1]] }); // a body that still lands after the abort changes nothing
  assert.deepEqual(await first, { status: "superseded", n: 0 });
  assert.equal(S.histPathN, 3); // the winner's geometry stands
  assert.equal(S.histPts.length, 3);
});

test("an aborted fetch reads as superseded, never as failed", async () => {
  const { S, api } = setup();
  const calls = stubFetch();
  const first = api.showFlightPath("1");
  api.showFlightPath("2");
  calls[0].abort(); // the browser's own rejection once the signal fires
  assert.deepEqual(await first, { status: "superseded", n: 0 });
  assert.equal(S.pathFetchSeq, 2);
});

test("clearPath orphans an in-flight fetch", async () => {
  const { S, api, cleared } = setup();
  const calls = stubFetch();
  const p = api.showFlightPath("42");
  api.clearPath();
  assert.equal(calls[0].signal.aborted, true);
  assert.equal(cleared(), 2);
  assert.equal(S.histPathN, 0);
  calls[0].ok({ points: PTS });
  assert.deepEqual(await p, { status: "superseded", n: 0 });
  assert.equal(S.histPathN, 0);
});

test("the map click guard is a flag on the shared cell, no DOM listener", () => {
  const { S, api } = setup();
  api.guardMapClicks(true);
  api.guardMapClicks(true);
  assert.equal(S.mapClickGuard, true);
  api.guardMapClicks(false);
  api.guardMapClicks(false);
  assert.equal(S.mapClickGuard, false);
  api.guardMapClicks(1); // truthy input lands as a boolean, so the click handler's test stays a strict one
  assert.equal(S.mapClickGuard, true);
});

test("dimLive and clearSelection pass straight through", () => {
  const { S, api, sel } = setup();
  api.dimLive(0.85);
  assert.equal(S.dimLive, 0.85);
  api.clearSelection();
  assert.equal(sel(), 1);
});
