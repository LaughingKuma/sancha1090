import { useLayoutEffect, useRef } from "preact/hooks";
import { destroyCharts } from "../chart.js";
import { W, unmountIslands } from "../adapter";
import { viewKey, wb, type ViewName } from "../store";
import * as overview from "../views/overview.js";
import * as flags from "../views/flags.js";
import * as trends from "../views/trends.js";
import * as estimates from "../views/estimates.js";
import * as coverage from "../views/coverage.js";

// log and drill are Preact components now; the remaining five still paint through the adapter host, keyed
// so a still-vanilla view fails to compile here until it is wired
type LegacyName = Exclude<ViewName, "log" | "drill">;
const VIEWS: Record<LegacyName, { render: (host: HTMLElement) => void }> = {
  overview, flags, trends, estimates, coverage,
};

// The adapter host: the seven vanilla views still paint here, but the element is Preact's and the view is
// re-run only when its own fetch key moves — a focus click (wb.inst) leaves the list alone.
export function LegacyView() {
  const host = useRef<HTMLDivElement>(null);
  const key = viewKey.value;
  useLayoutEffect(() => {
    const el = host.current!;
    W.body = el;
    el.replaceChildren();
    (VIEWS[wb.peek().view as LegacyName] || VIEWS.overview).render(el);
    // the cleanup runs before the next key's paint and on unmount: lists and charts die with the paint
    return () => {
      unmountIslands();
      destroyCharts(); // replaceChildren only detaches DOM — uPlot instances must be torn down explicitly
      W.body = null;
    };
  }, [key]);
  return <div class="wb-body" id="wb-body" role="tabpanel" ref={host} />;
}
