import { onFocusChange, exitFocus } from "./focus.js";
import { TIER_LABEL, jstDayOf } from "./shell.js";

let barEl = null;
let ptsChip = null;

function chip(cls, txt) {
  const el = document.createElement("span");
  el.className = cls;
  el.textContent = txt;
  return el;
}

function build(inst) {
  barEl = document.createElement("div");
  barEl.className = "wb-focus";
  barEl.setAttribute("role", "region");
  barEl.setAttribute("aria-label", "Focused flight");
  barEl.append(chip("wb-kick", "FOCUS"), chip("wb-cs", inst.callsign || inst.hex || "—"));
  const meta = [inst.day || jstDayOf(inst.startTs), inst.reg, inst.o || inst.d ? `${inst.o || "?"} → ${inst.d || "?"}` : ""]
    .filter(Boolean)
    .join(" · ");
  if (meta) barEl.append(chip("wb-meta", meta));
  barEl.append(chip(`wb-tier t-${inst.tier}`, TIER_LABEL[inst.tier] || TIER_LABEL.unknown));
  ptsChip = chip("wb-meta", "loading path…");
  barEl.append(ptsChip);
  const exit = document.createElement("button");
  exit.type = "button";
  exit.className = "wb-exit";
  exit.setAttribute("aria-label", "Exit focus");
  exit.textContent = "✕";
  exit.addEventListener("click", exitFocus);
  barEl.append(exit);
  document.body.appendChild(barEl);
}

function remove() {
  if (barEl) barEl.remove();
  barEl = null;
  ptsChip = null;
}

export function mountFocusBar() {
  // a new claim always follows a null (teardown notifies before it), so "no bar" means "build one"
  onFocusChange((f) => {
    if (!f) return remove();
    if (!barEl) build(f.inst);
    if (f.n != null) ptsChip.textContent = `${f.n.toLocaleString()} pts`;
  });
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") exitFocus();
  });
}
