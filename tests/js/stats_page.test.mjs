import { test, afterEach } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { Window } from "happy-dom";

const fixture = () => JSON.parse(readFileSync(
  fileURLToPath(new URL("../fixtures/stats_payload.json", import.meta.url)), "utf8"));
const statsUrl = new URL("../../livemap/static/stats/stats.js", import.meta.url).href;

const saved = {};
let seq = 0;
afterEach(() => {
  for (const [k, v] of Object.entries(saved)) globalThis[k] = v;
});

// stats.js renders on import, so each case loads a fresh copy against a fresh page and a stubbed fetch.
async function loadPage(body, { observeThrowsOnce = false } = {}) {
  const win = new Window();
  const doc = win.document;
  for (const id of ["rooftop", "database"]) {
    for (const [tag, suffix] of [["p", "asof"], ["div", "body"]]) {
      const node = doc.createElement(tag);
      node.id = `${id}-${suffix}`;
      doc.body.append(node);
    }
  }
  let thrown = !observeThrowsOnce;
  const stubs = {
    document: doc, Node: win.Node, DOMException: win.DOMException,
    ResizeObserver: class {
      observe() { if (!thrown) { thrown = true; throw new Error("observe failed"); } }
      disconnect() {}
    },
    fetch: async () => ({ ok: true, json: async () => body }),
    setInterval: () => 0,
  };
  for (const [k, v] of Object.entries(stubs)) {
    if (!(k in saved)) saved[k] = globalThis[k];
    globalThis[k] = v;
  }
  const errors = console.error;
  console.error = () => {};
  try {
    await import(`${statsUrl}?case=${seq++}`);
    await new Promise((r) => setTimeout(r, 20));
  } finally {
    console.error = errors;
  }
  const text = (id) => doc.getElementById(id).textContent;
  return { rooftop: text("rooftop-body"), database: text("database-body"), doc };
}

test("both sections render from the payload the server builds", async () => {
  const page = await loadPage(fixture());
  assert.match(page.rooftop, /A typical day, hour by hour/);
  assert.match(page.database, /Radio messages per day/);
  assert.doesNotMatch(page.rooftop + page.database, /withheld right now|unavailable right now/);
  assert.ok(page.doc.querySelector(".fact.mil .num"), "the military fact carries the red class");
});

test("a rooftop render that throws shows its unavailable card and the warehouse still renders", async () => {
  const page = await loadPage(fixture(), { observeThrowsOnce: true });
  assert.match(page.rooftop, /Rooftop numbers are withheld right now/);
  assert.equal(page.doc.getElementById("rooftop-asof").textContent, "");
  assert.match(page.database, /Radio messages per day/);
});

test("a warehouse render that throws shows its unavailable card and the rooftop still renders", async () => {
  const body = fixture();
  body.database.layers[0].layer = 5;
  const page = await loadPage(body);
  assert.match(page.database, /Warehouse numbers are unavailable right now/);
  assert.match(page.rooftop, /A typical day, hour by hour/);
});

test("an older refresh that answers last does not replace a newer one on screen", async () => {
  const win = new Window();
  const doc = win.document;
  for (const id of ["rooftop", "database"]) {
    for (const [tag, suffix] of [["p", "asof"], ["div", "body"]]) {
      const node = doc.createElement(tag);
      node.id = `${id}-${suffix}`;
      doc.body.append(node);
    }
  }
  const pending = [];
  const timers = [];
  const stubs = {
    document: doc, Node: win.Node, DOMException: win.DOMException,
    ResizeObserver: class { observe() {} disconnect() {} },
    fetch: () => new Promise((resolve) => pending.push(resolve)),
    setInterval: (fn) => { timers.push(fn); return timers.length; },
  };
  for (const [k, v] of Object.entries(stubs)) {
    if (!(k in saved)) saved[k] = globalThis[k];
    globalThis[k] = v;
  }
  await import(`${statsUrl}?case=${seq++}`);
  timers[0]();
  const reply = (name) => {
    const body = fixture();
    body.rooftop.airlines[0].name = name;
    return { ok: true, json: async () => body };
  };
  pending[1](reply("Newer Air"));
  await new Promise((r) => setTimeout(r, 20));
  pending[0](reply("Older Air"));
  await new Promise((r) => setTimeout(r, 20));
  const shown = doc.getElementById("rooftop-body").textContent;
  assert.match(shown, /Newer Air/);
  assert.doesNotMatch(shown, /Older Air/);
});
