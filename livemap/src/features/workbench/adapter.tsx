import { render, type ComponentChild } from "preact";
import { effect } from "@preact/signals";
import { milAvailable, wb } from "./store";
import type { WbState } from "./url";
import type { FlagInstance } from "./data";
import { InstanceList } from "./components/InstanceList";
import { RowList, type RowSpec } from "./components/RowList";
import { Pager, type PageInfo } from "./components/Pager";

// The vanilla views' whole world: W mirrors wb through a SYNCHRONOUS effect, so a view inside the Preact
// host sees exactly what the components see; W.body is LegacyView's ref'd host. Deleted with the last view (PR7).
export const W: WbState & { milAvailable: boolean; body: HTMLElement | null } = {
  ...wb.peek(), milAvailable: milAvailable.peek(), body: null,
};

// Object.assign copies every WbState field (and only those — W.body carries no wb key), so a field
// added in PR6b cannot silently miss the mirror.
effect(() => {
  Object.assign(W, wb.value);
  W.milAvailable = milAvailable.value;
});

// Every list a view mounts is its own Preact root inside the view's DOM: the roots must come down with
// the view, or their signal subscriptions would keep painting into detached nodes.
const roots = new Set<HTMLElement>();

function mount(host: HTMLElement, node: ComponentChild) {
  const el = document.createElement("div");
  host.appendChild(el);
  roots.add(el);
  render(node, el);
}

export function unmountIslands() {
  for (const el of roots) render(null, el);
  roots.clear();
}

export const renderFlagRows = (host: HTMLElement, rows: FlagInstance[]) =>
  mount(host, <InstanceList rows={rows} flag />);
export const renderRows = (host: HTMLElement, rows: RowSpec[], onPick: (i: number) => void, empty: string) =>
  mount(host, <RowList rows={rows} onPick={onPick} empty={empty} />);
export const renderPager = (host: HTMLElement, p: PageInfo | null) => {
  if (p) mount(host, <Pager p={p} />);
};
