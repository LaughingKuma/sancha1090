from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from conftest import RecordingCH, flat_sql

from include import ch_incremental_mvs as mv

# The two formerly-flat all-time HLL ADS-B MVs, re-grained to hourly uniqExact in v6.3.
ADSB_ACC = ("agg_country_traffic_adsb_acc", "agg_airline_traffic_adsb_acc")

# The four gold_ch specs that predate the db/view keys — their serving names are Superset-facing.
LEGACY_ACC = ("agg_hourly_traffic_acc", "agg_airline_traffic_acc",
              "agg_airline_traffic_adsb_acc", "agg_country_traffic_adsb_acc")

# All accumulate-forever _acc targets — the only tables that can't re-derive without an operator reseed.
ALL_ACC = (*LEGACY_ACC, "swim_latest_acc")


def test_acc_targets_are_fsynced():
    # #116: fsync closes the crash-loss class for the only non-rederivable state — a stray edit to any
    # target DDL must not silently drop the SETTINGS clause.
    for name in ALL_ACC:
        target = mv.SPECS[name]["target"]
        assert "fsync_after_insert = 1" in target, f"{name} target missing fsync_after_insert"
        assert "fsync_part_directory = 1" in target, f"{name} target missing fsync_part_directory"


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


# The value-gate oracle drops NULL-country rows, so the country MV must too; guards the alias-shadowing
# regression where assumeNotNull() no-op'd the NOT NULL filter and leaked untracked hexes as ''.
_UNTRACKED_SRC = "(SELECT 'zzzzzz' AS hex, toFloat64(1782698401) AS capture_ts, toUInt8(0) AS db_flags)"


def test_country_adsb_mv_drops_untracked_hex(ch_cur):
    seed = mv.SPECS["agg_country_traffic_adsb_acc"]["seed"][0]
    select_body = seed.split("\n", 1)[1]  # drop the leading 'INSERT INTO ...' line -> the SELECT
    body = select_body.replace("bronze.adsb_states", _UNTRACKED_SRC)
    ch_cur.execute(f"SELECT reg_country FROM ({body})")
    countries = [r[0] for r in ch_cur.fetchall()]
    assert countries == [], f"untracked hex leaked into a blank-country bucket: {countries!r}"


# Empirically derived from the live column types (toTypeName of the spec's own argMaxState) — the MV insert
# fails outright if any Nullable/DateTime64 spelling drifts from the bronze.swim_flightdata DDL.
SWIM_STATE = ("AggregateFunction(argMax, Tuple(Nullable(String), Nullable(String), Nullable(String), "
              "Nullable(String), Nullable(DateTime64(6, 'UTC')), Nullable(DateTime64(6, 'UTC')), "
              "Nullable(String)), Tuple(Nullable(DateTime64(6, 'UTC')), UInt64))")
# latest_tuple.1..7 in dbt is a POSITIONAL contract; the version tuple is the amendment precedence itself,
# so drift in either is silent data corruption rather than a build failure.
SWIM_VALUE_TUPLE = ("tuple(dep_point, dep_point_kind, arr_point, arr_point_kind, "
                    "filed_departure_time, filed_arrival_time, acid)")
SWIM_VERSION_TUPLE = "tuple(msg_timestamp, _dedup_fp)"


def test_swim_latest_spec_pins_the_full_argmax_contract():
    spec = mv.SPECS["swim_latest_acc"]
    assert spec["db"] == "silver_ch"
    assert spec["view"] == "swim_latest"
    assert SWIM_STATE in spec["target"], "swim target AggregateFunction type drifted from the derived text"
    assert "allow_nullable_key = 1" in spec["target"]
    assert "flight_key   Nullable(String)" in spec["target"], "flight_key must stay Nullable (not_null tripwire)"
    # mv and seed share one SELECT, but both are pinned: a divergence between them is an invisible
    # seed/stream split (the seeded history would key or version differently from everything after it).
    for body in (flat_sql(spec["mv"]), flat_sql(spec["seed"][0])):
        assert f"argMaxState({SWIM_VALUE_TUPLE}, {SWIM_VERSION_TUPLE})" in body, "argMax contract drifted"
        assert "FROM bronze.swim_flightdata" in body
        assert "WHERE acid IS NOT NULL AND trimBoth(acid) <> ''" in body
        assert "GROUP BY flight_key" in body
    read = flat_sql(spec["read"])
    assert "argMaxMerge(latest_state) AS latest_tuple" in read, "read must be merge-aware"
    assert "GROUP BY flight_key" in read


def test_legacy_serving_views_keep_their_gold_ch_names():
    # The db/view refactor touches every P4 spec; a silently moved or renamed view breaks Superset datasets.
    for name in LEGACY_ACC:
        spec = mv.SPECS[name]
        assert "db" not in spec and "view" not in spec, f"{name} unexpectedly declares db/view"
        assert mv.SERVING_VIEWS[name] == ("gold_ch", spec["drop_old"][0], spec["read"]), \
            f"{name} serving view moved"


def _patch_client(monkeypatch, rec):
    import include.clickhouse as ch

    monkeypatch.setattr(ch, "ch_client", lambda: rec)


def test_scoped_apply_touches_only_the_named_spec(monkeypatch):
    # The deploy runs {"names": ["swim_latest_acc"]}: an unscoped apply would DROP/CREATE every gold_ch MV and
    # open a miss-window on the live lanes, so scoping must be airtight in both directions.
    rec = RecordingCH()
    _patch_client(monkeypatch, rec)
    out = mv.apply(names=["swim_latest_acc"])

    assert "TRUNCATE TABLE silver_ch.swim_latest_acc" in rec.commands
    assert any("SELECT count() FROM silver_ch.swim_latest_acc" in q for q in rec.queries)
    assert "DROP VIEW IF EXISTS silver_ch.swim_latest_acc_mv" in rec.commands
    assert "CREATE DATABASE IF NOT EXISTS silver_ch" in rec.commands
    assert not [c for c in rec.commands if "gold_ch.agg_" in c], "a scoped run touched an existing gold_ch mart"
    views = [c for c in rec.commands if c.startswith("CREATE OR REPLACE VIEW")]
    assert len(views) == 1 and views[0].startswith("CREATE OR REPLACE VIEW silver_ch.swim_latest AS")
    assert out["serving_views"] == ["silver_ch.swim_latest"]


def test_ensure_qualifies_serving_views_with_their_db(monkeypatch):
    rec = RecordingCH(seeded=mv.SPECS, existing=mv.SPECS)
    _patch_client(monkeypatch, rec)
    mv.ensure()

    assert any(c.startswith("CREATE OR REPLACE VIEW silver_ch.swim_latest AS") for c in rec.commands)
    for name in LEGACY_ACC:
        base = mv.SPECS[name]["drop_old"][0]
        assert any(c.startswith(f"CREATE OR REPLACE VIEW gold_ch.{base} AS") for c in rec.commands)
    # ensure() owns no body redeploy: dropping the MV per tick would be a 10-minutely miss-window.
    assert not [c for c in rec.commands if c.startswith("DROP ")]


def test_ensure_never_raises(monkeypatch):
    # It is a non-blocking transform_marts step; a CH-side failure must degrade, not red the DAG.
    rec = RecordingCH(fail=True)
    _patch_client(monkeypatch, rec)
    assert mv.ensure()["ok"] is False
    assert rec.closed is True


def test_swim_seed_carries_the_memory_and_time_caps():
    # ensure() can run this seed unsupervised on a fresh bootstrap; the full-history 80M-row aggregation
    # keeps its own 12 GB bound, independent of the model's 7 GB (#208), so it stops instead of hanging the tick.
    seed = flat_sql(mv.SPECS["swim_latest_acc"]["seed"][0])
    assert "SETTINGS max_memory_usage = 12000000000, max_execution_time = 900" in seed


def test_every_spec_ddl_agrees_with_its_db_key():
    # The DDL strings are hardcoded while _db()/_seed_once()/apply() derive the db from the "db" key — a spec
    # whose key and text disagree would create the table in one db and TRUNCATE/seed/serve another.
    for name, spec in mv.SPECS.items():
        db = spec.get("db", "gold_ch")
        assert f"{db}.{name}" in spec["target"], f"{name} target DDL does not name {db}.{name}"
        assert f"{db}.{name}_mv" in spec["mv"], f"{name} MV DDL does not name {db}.{name}_mv"
        assert f"TO {db}.{name}" in spec["mv"], f"{name} MV does not write TO {db}.{name}"
        for s in spec["seed"]:
            assert s.startswith(f"INSERT INTO {db}.{name}"), f"{name} seed does not INSERT INTO {db}.{name}"


def test_serving_view_requires_a_name():
    # A spec with neither key would silently serve nothing; fail at import so any test run catches it.
    with pytest.raises(ValueError, match="no serving-view name"):
        mv._serving_views({"bad_acc": {"read": "SELECT 1"}})
    with pytest.raises(ValueError, match="no serving-view name"):
        mv._serving_views({"bad_acc": {"view": "", "read": "SELECT 1"}})


@pytest.mark.parametrize("raw", [None, "swim_latest_acc", [], ["swim_latest_acc", "swim_latest_acc"],
                                 ["nope"], ["swim_latest_acc", 3]])
def test_validate_names_rejects_bad_scopes(raw):
    # Degrading a typo to "apply everything" is the dangerous failure mode; a non-str element must raise
    # ValueError here, not a raw TypeError out of set().
    with pytest.raises(ValueError):
        mv.validate_names(raw, set(mv.SPECS))


def test_validate_names_accepts_a_known_scope():
    assert mv.validate_names(["swim_latest_acc"], set(mv.SPECS)) == ["swim_latest_acc"]


def test_apply_validates_its_own_names(monkeypatch):
    # apply() is callable programmatically (CLI, other code), so the guard lives in the module, not only in the DAG.
    rec = RecordingCH()
    _patch_client(monkeypatch, rec)
    with pytest.raises(ValueError):
        mv.apply(names=["nope"])
    assert rec.commands == [], "a rejected scope must not touch ClickHouse"


_EXISTS_PROBE = "SELECT count() FROM system.tables WHERE database = 'silver_ch' AND name = 'swim_latest_acc'"


def test_marker_without_the_target_it_certifies_reseeds(monkeypatch):
    # The documented rollback mistake (objects dropped, marker left behind) would otherwise recreate an empty
    # acc and skip its history forever — a just-created target makes the marker stale.
    rec = RecordingCH(seeded=["swim_latest_acc"])
    _patch_client(monkeypatch, rec)
    out = mv.apply(names=["swim_latest_acc"])

    # The existence signal must be captured BEFORE the CREATE TABLE, or it can only ever read "exists".
    assert rec.calls.index(_EXISTS_PROBE) < rec.calls.index(mv.SPECS["swim_latest_acc"]["target"])
    assert "TRUNCATE TABLE silver_ch.swim_latest_acc" in rec.commands
    assert any(c.startswith("INSERT INTO silver_ch.swim_latest_acc") for c in rec.commands)
    assert out["swim_latest_acc"]["skipped_seed"] is False


def test_marker_with_a_pre_existing_target_skips_the_seed(monkeypatch):
    rec = RecordingCH(seeded=["swim_latest_acc"], existing=["swim_latest_acc"])
    _patch_client(monkeypatch, rec)
    out = mv.apply(names=["swim_latest_acc"])

    assert "TRUNCATE TABLE silver_ch.swim_latest_acc" not in rec.commands
    assert not [c for c in rec.commands if c.startswith("INSERT INTO silver_ch.swim_latest_acc")]
    assert out["swim_latest_acc"]["skipped_seed"] is True
    # The per-tick path must not flap the Superset-facing view: no seed, no drop.
    assert "DROP VIEW IF EXISTS silver_ch.swim_latest" not in rec.commands
    # No row-count heuristic: it raced the live MV — one insert landing between CREATE and count() made a stale
    # marker look coherent, silently dropping the entire pre-MV history.
    assert "SELECT count() FROM silver_ch.swim_latest_acc" not in rec.queries


@pytest.mark.parametrize("existing", [(), ("swim_latest_acc",)])
def test_absent_marker_seeds_whether_or_not_the_target_existed(monkeypatch, existing):
    rec = RecordingCH(existing=existing)
    _patch_client(monkeypatch, rec)
    out = mv.apply(names=["swim_latest_acc"])

    assert "TRUNCATE TABLE silver_ch.swim_latest_acc" in rec.commands
    assert out["swim_latest_acc"]["skipped_seed"] is False


_DROP_VIEW = "DROP VIEW IF EXISTS silver_ch.swim_latest"          # the serving view, not the _acc_mv
_TRUNCATE = "TRUNCATE TABLE silver_ch.swim_latest_acc"
_MARKER_OUT = "DELETE FROM gold_ch.ch_mv_seeded WHERE name = 'swim_latest_acc'"


def _published_view(calls):
    return [c for c in calls if c.startswith("CREATE OR REPLACE VIEW silver_ch.swim_latest ")]


def test_failed_reseed_leaves_no_serving_view_over_the_truncated_acc(monkeypatch):
    # Withholding the re-publish isn't enough when the spec was ALREADY serving: the old view would survive the
    # truncate and read as legitimately empty, so dbt builds vacuously-green marts. Drop it before truncating.
    rec = RecordingCH(seeded=["swim_latest_acc"], existing=["swim_latest_acc"],
                      fail_on="INSERT INTO silver_ch.swim_latest_acc")
    _patch_client(monkeypatch, rec)
    with pytest.raises(RuntimeError):
        mv.apply(reseed=True, names=["swim_latest_acc"])

    assert rec.calls.index(_MARKER_OUT) < rec.calls.index(_DROP_VIEW) < rec.calls.index(_TRUNCATE)
    assert _published_view(rec.commands) == []


def test_healthy_reseed_gets_its_serving_view_back(monkeypatch):
    rec = RecordingCH(seeded=["swim_latest_acc"], existing=["swim_latest_acc"])
    _patch_client(monkeypatch, rec)
    mv.apply(reseed=True, names=["swim_latest_acc"])

    assert rec.calls.index(_DROP_VIEW) < rec.calls.index(_TRUNCATE)
    assert len(_published_view(rec.calls)) == 1
    assert rec.calls.index(_published_view(rec.calls)[0]) > rec.calls.index(_DROP_VIEW)


def test_ensure_drops_a_pre_existing_view_when_its_reseed_dies(monkeypatch):
    # Same seam through ensure(): target present but marker gone (a partial earlier run) re-seeds under a live
    # serving view — its gate only withholds the re-publish, so the drop is what makes the failure loud.
    rec = RecordingCH(seeded=LEGACY_ACC, existing=ALL_ACC,
                      fail_on="INSERT INTO silver_ch.swim_latest_acc")
    _patch_client(monkeypatch, rec)
    out = mv.ensure()

    assert out["swim_latest_acc"] == {"error": True}
    assert rec.calls.index(_DROP_VIEW) < rec.calls.index(_TRUNCATE)
    assert _published_view(rec.commands) == []


def test_ensure_withholds_the_serving_view_when_a_seed_raises(monkeypatch):
    # Fail closed: a spec whose seed blew up has an empty/partial acc, and a view over it reads as legitimately
    # empty — downstream dbt would then build vacuously-green marts. Healthy siblings must still be published.
    rec = RecordingCH(seeded=LEGACY_ACC, existing=LEGACY_ACC,
                      fail_on="INSERT INTO silver_ch.swim_latest_acc")
    _patch_client(monkeypatch, rec)
    out = mv.ensure()

    assert out["swim_latest_acc"] == {"error": True}
    assert not [c for c in rec.commands if c.startswith("CREATE OR REPLACE VIEW silver_ch.swim_latest ")]
    for name in LEGACY_ACC:
        base = mv.SPECS[name]["drop_old"][0]
        assert any(c.startswith(f"CREATE OR REPLACE VIEW gold_ch.{base} AS") for c in rec.commands)


def test_ensure_withholds_the_serving_view_when_the_marker_never_lands(monkeypatch):
    # The marker row IS the "fully seeded" certificate, so the gate reads it back rather than inferring it from
    # "the block did not raise" — a swallowed marker write must leave every view untouched.
    rec = RecordingCH(existing=mv.SPECS, responses={"ch_mv_seeded WHERE name": [[0]]})
    _patch_client(monkeypatch, rec)
    mv.ensure()

    assert not [c for c in rec.commands if c.startswith("CREATE OR REPLACE VIEW")]


def test_ensure_publishes_every_view_on_the_healthy_path(monkeypatch):
    rec = RecordingCH(seeded=mv.SPECS, existing=mv.SPECS)
    _patch_client(monkeypatch, rec)
    out = mv.ensure()

    assert all(out[name]["skipped_seed"] is True for name in mv.SPECS)
    for db, base, _ in mv.SERVING_VIEWS.values():
        assert any(c.startswith(f"CREATE OR REPLACE VIEW {db}.{base} AS") for c in rec.commands)


def test_ensure_creates_the_databases_it_needs(monkeypatch):
    # ensure() owns the fresh-bootstrap promise: a blank warehouse has neither gold_ch (the marker's home)
    # nor silver_ch, and the marker DDL would fail first.
    rec = RecordingCH(seeded=mv.SPECS, existing=mv.SPECS)
    _patch_client(monkeypatch, rec)
    mv.ensure()

    assert rec.commands[0] == "CREATE DATABASE IF NOT EXISTS gold_ch"
    assert "CREATE DATABASE IF NOT EXISTS silver_ch" in rec.commands
    assert rec.commands.index("CREATE DATABASE IF NOT EXISTS gold_ch") < \
        next(i for i, c in enumerate(rec.commands) if "ch_mv_seeded" in c)


_INIT_DAG = Path(__file__).parents[1] / "dags" / "ch_incremental_mvs_init.py"


def _init_module():
    spec = importlib.util.spec_from_file_location("ch_incremental_mvs_init_mod", _INIT_DAG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_init_conf_must_name_its_scope():
    # An accidental unscoped rerun DROP/CREATEs every MV under the live insert lanes; rows in that gap are gone.
    mod = _init_module()
    known = set(mv.SPECS)
    for conf in ({}, {"reseed": True}, {"all": False}):
        with pytest.raises(ValueError, match="must scope the run"):
            mod._scoped_names(conf, known)
    assert mod._scoped_names({"all": True}, known) is None
    assert mod._scoped_names({"all": "true"}, known) is None
    assert mod._scoped_names({"names": ["swim_latest_acc"]}, known) == ["swim_latest_acc"]
    with pytest.raises(ValueError):
        mod._scoped_names({"names": []}, known)


def test_init_conf_rejects_a_contradictory_scope():
    # Letting {"all": true} silently win over an explicit names list broadens a deliberately narrow run into
    # the miss-window-carrying redeploy of every MV; the CLI already refuses the same pair.
    mod = _init_module()
    known = set(mv.SPECS)
    for conf in ({"all": True, "names": ["swim_latest_acc"]},
                 {"names": ["swim_latest_acc"], "all": "true"}):
        with pytest.raises(ValueError, match="BOTH"):
            mod._scoped_names(conf, known)
