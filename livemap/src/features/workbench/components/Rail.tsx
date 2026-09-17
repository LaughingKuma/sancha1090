import type { FunctionComponent } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
import { activeView, status, type ViewName } from "../store";
import { Coverage } from "./Coverage";
import { Drill } from "./Drill";
import { Estimates } from "./Estimates";
import { Flags } from "./Flags";
import { Log } from "./Log";
import { Overview } from "./Overview";
import { RangeChips } from "./RangeChips";
import { Search } from "./Search";
import { Trends } from "./Trends";
import { ViewTabs } from "./ViewTabs";

// keyed by ViewName so a view added to the vocabulary fails to compile here until it has a component
const VIEWS: Record<ViewName, FunctionComponent> = {
  overview: Overview, drill: Drill, log: Log, flags: Flags,
  trends: Trends, estimates: Estimates, coverage: Coverage,
};

// The tabpanel id is the ViewTabs' aria-controls target and the e2e body locator, so it is the one host.
function RailBody() {
  const View = VIEWS[activeView.value];
  return <div class="wb-body" id="wb-body" role="tabpanel"><View /></div>;
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
