"""Guard the hand-maintained copies of the Japan box against include/regions.py.

The box is duplicated by necessity in two places that can't import include/:
- scripts/vps_collector.py — shipped as a single-file cloud-init payload.
- the dbt layer (japan_box_* vars in dbt_project.yml rendered via the in_japan_box macro, +
  assert_states_within_japan_box.sql which still hardcodes it) — SQL, no Python import.
These tests pin every copy to the canonical REGIONS so the scope can't silently
diverge between ingestion, the VPS collector, and the mart staging filter.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

from include.regions import REGIONS as CANONICAL

REPO = Path(__file__).resolve().parents[1]


def _vps_collector_regions() -> list:
    src = (REPO / "scripts" / "vps_collector.py").read_text()
    for node in ast.parse(src).body:
        # Plain `REGIONS = [...]` (Assign) or annotated `REGIONS: ... = [...]` (AnnAssign).
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        if any(getattr(t, "id", None) == "REGIONS" for t in targets) and node.value is not None:
            return ast.literal_eval(node.value)
    raise AssertionError("REGIONS assignment not found in scripts/vps_collector.py")


def _japan_box() -> dict:
    box = next((r for r in CANONICAL if r["name"] == "japan"), None)
    if box is None:
        raise AssertionError("region 'japan' not found in include/regions.py REGIONS")
    return box


def test_vps_collector_regions_match_canonical():
    assert _vps_collector_regions() == CANONICAL


def test_dbt_japan_box_matches_canonical():
    """The dbt layer duplicates the box (no Python import); fail if any copy drifts."""
    box = _japan_box()
    # Pinning the vars pins every in_japan_box renderer (stg_states, box_observed) at once — no raw-SQL pin needed for macro callers.
    proj = yaml.safe_load((REPO / "dbt" / "sancha1090" / "dbt_project.yml").read_text())
    v = proj["vars"]
    assert (v["japan_box_lamin"], v["japan_box_lamax"]) == (box["lamin"], box["lamax"]), \
        f"dbt japan_box lat vars != canonical {box['lamin']:g}..{box['lamax']:g}"
    assert (v["japan_box_lomin"], v["japan_box_lomax"]) == (box["lomin"], box["lomax"]), \
        f"dbt japan_box lon vars != canonical {box['lomin']:g}..{box['lomax']:g}"
    # No var behind this file — it hardcodes `not between` bounds — so only a raw-text pin can catch drift.
    lat = re.compile(rf"latitude\s+(?:not\s+)?between\s+{box['lamin']:g}\s+and\s+{box['lamax']:g}")
    lon = re.compile(rf"longitude\s+(?:not\s+)?between\s+{box['lomin']:g}\s+and\s+{box['lomax']:g}")
    rel = "dbt/sancha1090/tests/assert_states_within_japan_box.sql"
    text = (REPO / rel).read_text()
    assert lat.search(text), f"{rel}: latitude bound != canonical {box['lamin']:g}..{box['lamax']:g}"
    assert lon.search(text), f"{rel}: longitude bound != canonical {box['lomin']:g}..{box['lomax']:g}"


def test_dbt_japan_box_macro_wiring():
    """Vars alone don't prove scope: a consumer reverting to hardcoded bounds would pass the var pin."""
    macro = (REPO / "dbt" / "sancha1090" / "macros" / "japan_box.sql").read_text()
    for var in ("japan_box_lamin", "japan_box_lamax", "japan_box_lomin", "japan_box_lomax"):
        assert f"var('{var}')" in macro, f"macros/japan_box.sql: {var} not rendered by in_japan_box"
    for rel in (
        "dbt/sancha1090/models/staging/stg_states.sql",
        "dbt/sancha1090/models/marts/fct_flights_reconciled.sql",
        "dbt/sancha1090/models/silver/stg_vrs_routes.sql",
        "dbt/sancha1090/models/silver/int_swim_opinion.sql",
    ):
        assert "in_japan_box(" in (REPO / rel).read_text(), f"{rel}: no longer calls in_japan_box"
