import { W, buildRail, applyUrl } from "./shell.js";
import { initFocus } from "./focus.js";
import { mountFocusBar } from "./focusbar.js";
import { mountSearch } from "./search.js";
// Vite links no stylesheet for a JS-only entry: the build emits workbench.css as index.css, linked here.
import cssHref from "./workbench.css?url";

function injectCss() {
  if (document.querySelector(`link[href="${cssHref}"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = cssHref;
  document.head.appendChild(link);
}

export function init(mapApi, features) {
  if (W.rail) return; // a cache-skewed second import must not build a second rail
  injectCss();
  // the bundle bakes wb_contract.json at build time; a server on another envelope generation gets one
  // line instead of a rail that would misread its payloads
  if (!features || features.contract !== __WB_CONTRACT__) {
    document.body.insertAdjacentHTML("beforeend",
      '<aside class="wb-stale" role="status">workbench bundle stale — rebuild the livemap image</aside>');
    return;
  }
  initFocus(mapApi);
  mountFocusBar();
  buildRail();
  mountSearch(W.searchHost);
  applyUrl();
  window.addEventListener("popstate", applyUrl);
}
