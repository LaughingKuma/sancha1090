"""Guard the hand-maintained copies of the Japan box against include/regions.py.

The box is duplicated by necessity in two places that can't import include/:
- scripts/vps_collector.py — shipped as a single-file cloud-init payload.
- the dbt layer (stg_states.sql + assert_states_within_japan_box.sql) — SQL,
  no Python import.
These tests pin every copy to the canonical REGIONS so the scope can't silently
diverge between ingestion, the VPS collector, and the mart staging filter.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

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
    """The dbt SQL hardcodes the box (no Python import); fail if it drifts."""
    box = _japan_box()
    # Tolerates whitespace and an optional `not` (the assertion test uses `not between`).
    lat = re.compile(rf"latitude\s+(?:not\s+)?between\s+{box['lamin']:g}\s+and\s+{box['lamax']:g}")
    lon = re.compile(rf"longitude\s+(?:not\s+)?between\s+{box['lomin']:g}\s+and\s+{box['lomax']:g}")
    lat_msg = f"latitude bound != canonical {box['lamin']:g}..{box['lamax']:g}"
    lon_msg = f"longitude bound != canonical {box['lomin']:g}..{box['lomax']:g}"
    for rel in (
        "dbt/sancha1090/models/staging/stg_states.sql",
        "dbt/sancha1090/tests/assert_states_within_japan_box.sql",
    ):
        text = (REPO / rel).read_text()
        assert lat.search(text), f"{rel}: {lat_msg}"
        assert lon.search(text), f"{rel}: {lon_msg}"
