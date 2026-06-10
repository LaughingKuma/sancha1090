"""Geographic scope for the OpenSky states pull.

Why one Japan box, not a global sweep?
1. The platform is antenna-centric: the rooftop receiver over Tokyo is the
   protagonist, and OpenSky is its wide-context layer — "what's flying over
   Japan, including beyond my horizon." Aircraft outside Japan never tie to
   the receiver, the range outline, or the livemap, so the world sweep was
   pure credit + storage spend with no narrative payoff.
2. The box deliberately overshoots the antenna's reception footprint (the
   v4.5 range outline, ~Kanto + offshore Pacific): the gap between the two is
   "beyond my horizon," and it shrinks as the antenna's placement improves.
3. The east edge reaches 165 to catch the trans-Pacific / NOPAC oceanic
   traffic the antenna already receives over the water.

Cost: any bbox > 400 sq deg is OpenSky's top flat tier (4 credits/call), so a
generous Japan+ocean box costs the same as a continent — one call, 4 credits.
Splitting it finer would only add calls without cutting per-call cost. See
tests/test_credit_budget.py for the enforced budget.

Kept as a list (not a bare dict) so ingest_states' dynamic task mapping is
unchanged and sub-regions can return later without a DAG rewrite.

Bounding box format: (lamin, lomin, lamax, lomax)
  Latitudes: -90 (south pole) to 90 (north pole)
  Longitudes: -180 (west) to 180 (east)
"""

from __future__ import annotations

REGIONS: list[dict[str, float | str]] = [
    {"name": "japan", "lamin": 20.0, "lomin": 122.0, "lamax": 50.0, "lomax": 165.0},
]
