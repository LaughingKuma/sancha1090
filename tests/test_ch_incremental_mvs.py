from __future__ import annotations

from include import ch_incremental_mvs as mv

# The two formerly-flat all-time HLL ADS-B MVs, re-grained to hourly uniqExact in v6.3.
ADSB_ACC = ("agg_country_traffic_adsb_acc", "agg_airline_traffic_adsb_acc")


def test_adsb_mvs_use_exact_not_hll():
    # v6.3: re-grain from uniq (HLL, ~0.5%/country error) to hourly uniqExact — exact, replay-immune, and
    # bounded by the 90d TTL. A stray uniqState/uniqMerge would silently re-introduce the HLL approximation.
    for name in ADSB_ACC:
        spec = mv.SPECS[name]
        assert "AggregateFunction(uniqExact," in spec["target"], f"{name} target lost its uniqExact state"
        assert "AggregateFunction(uniq," not in spec["target"], f"{name} still carries an HLL (uniq) state"
        assert "uniqExactState" in spec["mv"] and "uniqState" not in spec["mv"], f"{name} MV not exact"
        for s in spec["seed"]:
            assert "uniqState" not in s, f"{name} seed not exact"
        assert "uniqExactMerge" in spec["read"] and "uniqMerge" not in spec["read"], f"{name} read not exact"


def test_adsb_mvs_are_hour_grained_with_90d_ttl():
    # Hour buckets make uniqExact affordable (each (hex,capture_ts) lands in one disjoint hour) and the 90d TTL
    # bounds the otherwise-unbounded all-time state; the read collapses hours so the served per-group number and
    # the Superset shape are unchanged (merge over a group's hours == the exact group total).
    for name in ADSB_ACC:
        spec = mv.SPECS[name]
        assert "snapshot_hour" in spec["target"], f"{name} target has no hour grain"
        assert "TTL snapshot_hour + INTERVAL 90 DAY" in spec["target"], f"{name} missing the 90d TTL"
        assert "toStartOfHour(toDateTime(" in spec["mv"], f"{name} MV does not bucket capture_ts by hour"
        # The read collapses hours (no GROUP BY snapshot_hour) but enforces the 90d window at query time (the TTL
        # drops lazily on merge, so the WHERE is what makes the served number deterministic).
        assert "GROUP BY snapshot_hour" not in spec["read"], f"{name} read must collapse hours before serving"
        assert "snapshot_hour >= now('UTC') - INTERVAL 90 DAY" in spec["read"], \
            f"{name} read must enforce the 90d served window at query time"


def test_adsb_country_mv_reads_baked_db_flags_not_raw_json():
    # _raw_json is eliminated from CH in v6.3; military decodes the baked db_flags integer column instead.
    spec = mv.SPECS["agg_country_traffic_adsb_acc"]
    assert "_raw_json" not in spec["mv"], "country MV still reads _raw_json (eliminated in v6.3)"
    assert all("_raw_json" not in s for s in spec["seed"]), "country seed still reads _raw_json"
    assert "db_flags" in spec["mv"], "country MV must decode the baked db_flags column"
