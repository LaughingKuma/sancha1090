import { render } from "preact";
import type { MapFacade } from "../../map/facade";
import type { Features } from "./wire";
import { Rail } from "./components/Rail";
import { FocusBar } from "./components/FocusBar";
import { applyUrl, initStore } from "./store";
// Vite links no stylesheet for a JS-only entry: the build emits workbench.css as index.css, linked here.
import cssHref from "./workbench.css?url";

function injectCss() {
  if (document.querySelector(`link[href="${cssHref}"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = cssHref;
  document.head.appendChild(link);
}

const App = () => (
  <>
    <Rail />
    <FocusBar />
  </>
);

export function init(mapApi: MapFacade, features: Features) {
  if (document.getElementById("wb-host")) return; // a cache-skewed second import must not build a second rail
  injectCss();
  // the bundle bakes wb_contract.json at build time; a server on another envelope generation gets one
  // line instead of a rail that would misread its payloads
  if (!features || features.contract !== __WB_CONTRACT__) {
    if (document.querySelector(".wb-stale")) return; // a second init on the stale path must not stack banners
    const stale = document.createElement("aside");
    stale.className = "wb-stale";
    stale.setAttribute("role", "status");
    stale.textContent = "workbench bundle stale — rebuild the livemap image";
    document.body.append(stale);
    return;
  }
  // the island owns its own host: rendering into body would let Preact reconcile the map's DOM away
  const host = document.createElement("div");
  host.id = "wb-host";
  document.body.append(host);
  initStore(mapApi);
  render(<App />, host);
  applyUrl();
}
