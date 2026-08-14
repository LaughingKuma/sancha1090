import { W, buildRail, applyUrl } from "./shell.js?v=6.44";
import { mountSearch } from "./search.js?v=6.44";

function injectCss() {
  const href = new URL("./workbench.css?v=6.44", import.meta.url).href;
  if (document.querySelector(`link[href="${href}"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = href;
  document.head.appendChild(link);
}

export function init(S) {
  if (W.rail) return; // a cache-skewed second import must not build a second rail
  W.S = S;
  injectCss();
  buildRail();
  mountSearch(W.searchHost);
  applyUrl();
  window.addEventListener("popstate", applyUrl);
}
