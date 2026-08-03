import { panel, navigate } from "../shell.js?v=6.41";

// Slice 1 has no summary endpoint — this is the platform framing plus the two doorways that exist.
export function render(host) {
  const el = panel(host);
  el.innerHTML =
    '<div class="wb-sect">workbench</div>' +
    '<div class="wb-note">Question-first, evidence-terminal: every number is a doorway, ' +
    'every instance ends on the map as a reconstructed path.</div>' +
    '<div class="wb-sect">surfaces</div>' +
    '<div class="wb-surf">' +
    '<button type="button" class="wb-surf-go" data-view="drill">' +
    '<span class="wb-name">DRILL</span><span class="wb-note">airline → service → instance</span></button>' +
    '<button type="button" class="wb-surf-go" data-view="log">' +
    '<span class="wb-name">LOG</span><span class="wb-note">flat instance log, day / airport / type / mil</span></button>' +
    '<div class="wb-surf-ghost"><span class="wb-name">ESTIMATES</span>' +
    '<span class="wb-note">estimate error, skip mix — slice 3</span></div>' +
    '<div class="wb-surf-ghost"><span class="wb-name">COVERAGE</span>' +
    '<span class="wb-note">tier mix by day, gap distribution — slice 3</span></div></div>';
  el.addEventListener("click", (e) => {
    const btn = e.target.closest(".wb-surf-go");
    if (btn) navigate({ view: btn.dataset.view, page: 1 });
  });
}
