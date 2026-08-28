import { useEffect, useRef, useState } from "preact/hooks";
import { activeView, status } from "../store";
import { Drill } from "./Drill";
import { LegacyView } from "./LegacyView";
import { Log } from "./Log";
import { RangeChips } from "./RangeChips";
import { Search } from "./Search";
import { ViewTabs } from "./ViewTabs";

// log and drill are components over resource(); the other five views still paint through the adapter host.
// The tabpanel id is the ViewTabs' aria-controls target and the e2e body locator, so every branch carries it.
function RailBody() {
  const view = activeView.value;
  if (view === "log" || view === "drill")
    return <div class="wb-body" id="wb-body" role="tabpanel">{view === "log" ? <Log /> : <Drill />}</div>;
  return <LegacyView />;
}

export function Rail() {
  const [collapsed, setCollapsed] = useState(false);
  const collapseBtn = useRef<HTMLButtonElement>(null);
  const tab = useRef<HTMLButtonElement>(null);
  const moveFocus = useRef(false); // set by the toggle clicks only — a mount must not steal focus from the map
  const toggle = (to: boolean) => {
    moveFocus.current = true;
    setCollapsed(to);
  };
  // the target button is hidden until the state commits, so keyboard focus follows the toggle afterwards
  useEffect(() => {
    if (!moveFocus.current) return;
    moveFocus.current = false;
    (collapsed ? tab : collapseBtn).current!.focus();
  }, [collapsed]);
  return (
    <>
      <aside class="wb-rail" id="wb-rail" aria-label="Workbench" hidden={collapsed}>
        <div class="wb-head">
          <span class="wb-title">WORKBENCH</span>
          <button type="button" class="wb-collapse" aria-label="Collapse workbench" ref={collapseBtn}
            onClick={() => toggle(true)}>◂</button>
        </div>
        <ViewTabs />
        <RangeChips />
        <Search />
        <RailBody />
        <div class="wb-sr" role="status" aria-live="polite">{status}</div>
      </aside>
      <button type="button" class="wb-tab" hidden={!collapsed} ref={tab}
        onClick={() => toggle(false)}>WORKBENCH ▸</button>
    </>
  );
}
