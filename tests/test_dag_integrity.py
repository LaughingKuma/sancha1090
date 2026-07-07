"""DAG parse and structure tests.

Catches the most common kinds of breakage:
- Syntax errors or bad imports in a DAG file
- Accidental schedule changes (especially the credit-budget-bound ingest)
- Tasks renamed or removed without updating the contract
"""

from __future__ import annotations

import pytest


EXPECTED_DAGS = {
    "ingest_states": {
        "schedule": "*/4 * * * *",
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"fetch_region", "summarize"},
    },
    "ingest_adsb": {
        "schedule": "5 * * * *",
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"list_remote_bundles", "select_new", "validate_and_record", "summarize_emit_asset"},
    },
    "transform_marts": {
        # Asset-triggered: schedule is a list of Asset objects, not a cron string.
        "schedule_is_asset_triggered": True,
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"dbt_run_ch", "dbt_test_ch", "ensure_ch_mvs", "push_flight_routes"},
        # dbt_run_ch builds; dbt_test_ch + ensure_ch_mvs are all_success leaves, so a run OR test failure
        # propagates (upstream_failed) and reds the run — nothing masks a dbt failure. push_flight_routes is
        # gated on dbt_test_ch (SP2 moved it here: route source is now the reconciled mart, built by this DAG).
        "downstream_task_ids": {
            "dbt_run_ch": {"dbt_test_ch", "ensure_ch_mvs"},
            "dbt_test_ch": {"push_flight_routes"},
        },
    },
    "transform_adsb_silver": {
        "schedule_is_asset_triggered": True,
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"dbt_run_ch", "dbt_test_ch"},
        "downstream_task_ids": {
            "dbt_run_ch": {"dbt_test_ch"},
        },
    },
    "tableize_states": {
        "schedule_is_asset_triggered": True,
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"load_pending_to_clickhouse"},
    },
    "tableize_adsb": {
        "schedule_is_asset_triggered": True,
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"load_adsb_to_clickhouse"},
    },
    "maintain_adsb_schema": {
        "schedule": "35 4 * * 1",
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"scan_drift"},
    },
    "refresh_risingwave_dims": {
        "schedule": "15 5 * * 1",
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"reload"},
    },
    "refresh_range_outline": {
        "schedule": "40 5 * * *",
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"refresh"},
    },
    "sync_vps_states_buffer": {
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"sync_r2_to_garage"},
    },
    "ch_incremental_mvs_init": {
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"create_and_seed"},
    },
    "ch_serving_parity": {
        "schedule": "*/15 * * * *",
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"gate", "value_gate"},
        # gate -> value_gate so the watermark advances only after bronze completeness passes (its oracle is
        # bronze) — else a meaningless pass ages discrepancies out of the recheck window.
        "downstream_task_ids": {
            "gate": {"value_gate"},
        },
        # Protection gate must run on a clean deploy without a manual unpause (compose defaults paused=true).
        "is_paused_upon_creation": False,
    },
    "ingest_flights": {
        "schedule": "30 14 * * *",
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"fetch_airport", "summarize"},
    },
    "tableize_flights": {
        "schedule_is_asset_triggered": True,
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"load_pending_to_clickhouse"},
    },
    "ingest_aircraft_db": {
        "schedule": "0 17 * * 0",
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"download_and_land", "load_to_clickhouse"},
    },
    "transform_flights": {
        "schedule_is_asset_triggered": True,
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"dbt_run_ch", "dbt_test_ch"},
        # Linear gate: build -> test. The RW route publish moved to transform_marts (SP2: route source is now
        # the reconciled mart, built there).
        "downstream_task_ids": {
            "dbt_run_ch": {"dbt_test_ch"},
        },
    },
    "maintain_bronze_dedup": {
        "schedule": "30 18 * * *",
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"optimize"},
        # Bounded-growth guarantee must run on a clean deploy without a manual unpause.
        "is_paused_upon_creation": False,
    },
    "archive_raw_to_nas": {
        "schedule": "0 19 * * *",
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"archive_pending_to_nas"},
        # Off-peak cold-archive maintenance; self-skips on hosts without the NFS mount, so it runs unpaused.
        "is_paused_upon_creation": False,
    },
    "ingest_adsblol_routes": {
        "schedule": "0 3 * * *",
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"fetch_and_land", "load_to_clickhouse"},
    },
}


def test_dagbag_has_no_import_errors(dagbag):
    """No DAG file in dags/ should fail to import."""
    assert not dagbag.import_errors, (
        f"DAG import errors:\n{dagbag.import_errors}"
    )


def test_all_expected_dags_present(dagbag):
    """Every DAG we expect is actually registered."""
    actual = set(dagbag.dag_ids)
    expected = set(EXPECTED_DAGS)
    assert actual == expected, (
        f"DAG registry mismatch.\n"
        f"  Missing:    {sorted(expected - actual)}\n"
        f"  Unexpected: {sorted(actual - expected)}"
    )


@pytest.mark.parametrize(("dag_id", "expected"), EXPECTED_DAGS.items())
def test_dag_structure(dagbag, dag_id, expected):
    """Each DAG matches its expected schedule, options, and task set."""
    dag = dagbag.dags.get(dag_id)
    assert dag is not None, f"DAG {dag_id} not found"

    if "schedule" in expected:
        schedule = getattr(dag, "schedule_interval", None) or getattr(dag, "schedule", None)
        assert str(schedule) == expected["schedule"] or expected["schedule"] in str(schedule), (
            f"{dag_id} schedule changed from {expected['schedule']!r} to {schedule!r}"
        )

    if expected.get("schedule_is_asset_triggered"):
        schedule = getattr(dag, "schedule_interval", None) or getattr(dag, "schedule", None)
        assert not (isinstance(schedule, str) and any(c in str(schedule) for c in "0123456789*/")), (
            f"{dag_id} should be asset-triggered but has a cron-like schedule: {schedule!r}"
        )

    assert dag.catchup == expected["catchup"], (
        f"{dag_id}.catchup expected {expected['catchup']}, got {dag.catchup}"
    )
    assert dag.max_active_runs == expected["max_active_runs"], (
        f"{dag_id}.max_active_runs expected {expected['max_active_runs']}, "
        f"got {dag.max_active_runs}"
    )

    actual_task_ids = {t.task_id for t in dag.tasks}
    assert actual_task_ids == expected["task_ids"], (
        f"{dag_id} task set mismatch.\n"
        f"  Expected: {sorted(expected['task_ids'])}\n"
        f"  Actual:   {sorted(actual_task_ids)}"
    )

    for task_id, downstream in expected.get("downstream_task_ids", {}).items():
        actual = dag.get_task(task_id).downstream_task_ids
        assert actual == downstream, (
            f"{dag_id}.{task_id} downstream expected {sorted(downstream)}, got {sorted(actual)}"
        )

    for task_id, trigger_rule in expected.get("trigger_rules", {}).items():
        actual = dag.get_task(task_id).trigger_rule
        assert actual == trigger_rule, (
            f"{dag_id}.{task_id} trigger_rule expected {trigger_rule!r}, got {actual!r}"
        )

    if "is_paused_upon_creation" in expected:
        assert dag.is_paused_upon_creation == expected["is_paused_upon_creation"], (
            f"{dag_id}.is_paused_upon_creation expected {expected['is_paused_upon_creation']}, "
            f"got {dag.is_paused_upon_creation}"
        )
