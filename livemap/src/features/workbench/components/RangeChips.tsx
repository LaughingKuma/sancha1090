import { useRef, useState } from "preact/hooks";
import { RANGE_PRESETS } from "../url";
import { activeRange, customRange, navigate } from "../store";

const RANGES = [...Object.keys(RANGE_PRESETS), "all"];

export function RangeChips() {
  const range = activeRange.value; // not wb.value: a re-render re-applies value= over a half-typed date
  const custom = customRange.value;
  const [open, setOpen] = useState(false);
  const from = useRef<HTMLInputElement>(null);
  const to = useRef<HTMLInputElement>(null);
  // a preset is now the range — an open editor would read as still-pending input
  const preset = (r: string) => {
    setOpen(false);
    navigate({ range: r, page: 1 });
  };
  const apply = () => {
    const f = from.current!.value;
    const t = to.current!.value;
    // an inverted pair is a slip, not intent — order it rather than fetch an impossible window
    if (f && t) navigate({ range: f <= t ? `${f}..${t}` : `${t}..${f}`, page: 1 });
  };
  return (
    <div class="wb-range" title="date range applies to instance lists">
      {RANGES.map((r) => (
        <button key={r} type="button" class="wb-chip" data-range={r} aria-pressed={r === range}
          onClick={() => preset(r)}>{r}</button>
      ))}
      <button type="button" class="wb-chip" data-custom="1" aria-pressed={!!custom}
        onClick={() => setOpen(!open)}>custom</button>
      <span class="wb-custom" hidden={!custom && !open}>
        <input type="date" aria-label="Range start" ref={from} value={custom ? custom[1] : ""} />
        <input type="date" aria-label="Range end" ref={to} value={custom ? custom[2] : ""} />
        <button type="button" class="wb-chip" data-apply="1" onClick={apply}>apply</button>
      </span>
    </div>
  );
}
