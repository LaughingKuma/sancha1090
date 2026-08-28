import { VIEW_NAMES } from "../url";
import { activeView, doorway, wb, type ViewName } from "../store";

// keyed by ViewName so a view added to the vocabulary fails to compile here until it gets a tab label
const LABEL: Record<ViewName, string> = {
  overview: "home", drill: "drill", log: "log", flags: "flags",
  trends: "trends", estimates: "est", coverage: "cov",
};

export function ViewTabs() {
  const view = activeView.value;
  return (
    <div class="wb-views" role="tablist" aria-label="Workbench views">
      {VIEW_NAMES.map((v) => (
        <button key={v} type="button" class="wb-view" role="tab" aria-controls="wb-body" data-view={v}
          aria-selected={v === view}
          // the doorway carries the whole scope and the target keeps only what it reads: MIL survives
          // into the log, the class filter into flags, neither anywhere else
          onClick={() => v !== view && doorway(v, wb.peek())}>{LABEL[v]}</button>
      ))}
    </div>
  );
}
