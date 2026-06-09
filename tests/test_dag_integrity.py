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
        "schedule": "*/12 * * * *",
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"list_regions", "fetch_region", "summarize"},
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
        "task_ids": {
            "dbt_deps", "dbt_run_trino", "dbt_test_trino",
        },
    },
    "transform_adsb_silver": {
        "schedule_is_asset_triggered": True,
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"dbt_deps", "dbt_seed", "dbt_run", "dbt_test"},
    },
    "tableize_states": {
        "schedule_is_asset_triggered": True,
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"load_pending_to_iceberg"},
    },
    "tableize_adsb": {
        "schedule_is_asset_triggered": True,
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"add_pending_to_iceberg"},
    },
    "maintain_iceberg_states": {
        "schedule": "30 3 * * *",
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"expire_snapshots"},
    },
    "maintain_iceberg_marts": {
        "schedule": "30 4 * * *",
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {
            "optimize_silver", "expire_silver", "orphans_silver",
            "optimize_gold", "expire_gold", "orphans_gold",
        },
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
    "backup_polaris": {
        "schedule": "0 2 * * *",
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"dump_to_garage"},
    },
    "backfill_from_buffer": {
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"sync_r2_to_garage"},
    },
    "backfill_adsb": {
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"run_backfill", "run_beast_backfill"},
    },
    "register_bronze_in_polaris": {
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"register"},
    },
    "migrate_drop_sqlcatalog_tables": {
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"drop_sqlcatalog_tables"},
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
