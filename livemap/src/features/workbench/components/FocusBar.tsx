import { useEffect } from "preact/hooks";
import { exitFocus, focus } from "../store";
import { TierPill, instDay, instOd } from "./InstanceList";

export function FocusBar() {
  // the one global key owner: Esc anywhere leaves focus (the search box stops its own Esc first)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") exitFocus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const f = focus.value;
  if (!f) return null;
  const inst = f.inst;
  const meta = [instDay(inst), inst.reg, inst.o || inst.d ? instOd(inst) : ""].filter(Boolean).join(" · ");
  return (
    <div class="wb-focus" role="region" aria-label="Focused flight">
      <span class="wb-kick">FOCUS</span>
      <span class="wb-cs">{inst.callsign || inst.hex || "—"}</span>
      {meta && <span class="wb-meta">{meta}</span>}
      <TierPill tier={inst.tier} />
      <span class="wb-meta">{f.n == null ? "loading path…" : `${f.n.toLocaleString()} pts`}</span>
      <button type="button" class="wb-exit" aria-label="Exit focus" onClick={exitFocus}>✕</button>
    </div>
  );
}
