from __future__ import annotations

import re
from pathlib import Path

from include import adsblol_trace_utils as tu

REPO = Path(__file__).resolve().parents[1]


def test_num_keeps_only_real_numbers():
    assert tu.num(3) == 3.0 and tu.num(2.5) == 2.5
    assert tu.num("ground") is None and tu.num(None) is None and tu.num("12") is None


def test_trace_preamble_rejects_empty_synthetic_and_unstamped_docs():
    assert tu.trace_preamble({"icao": "abc123", "timestamp": 1, "trace": []}) is None
    assert tu.trace_preamble({"icao": "~abc123", "timestamp": 1, "trace": [[0]]}) is None
    assert tu.trace_preamble({"icao": "abc123", "trace": [[0]]}) is None
    assert tu.trace_preamble({"icao": "ABC123", "timestamp": 7, "trace": [[0]]}) == ([[0]], "abc123", 7)


def test_backfill_and_routes_share_the_public_helpers():
    # The two lanes used to reach into each other's underscore helpers; the shared module is the one home.
    for rel in ("include/adsblol_backfill.py", "include/adsblol_routes.py"):
        src = (REPO / rel).read_text()
        assert re.search(r"^from include\.adsblol_trace_utils import num, trace_preamble$", src, re.M), rel
        assert not re.search(r"^from include\.adsblol_backfill import _", src, re.M), rel
