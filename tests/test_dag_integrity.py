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
    "transform_marts": {
        # Asset-triggered: schedule is a list of Asset objects, not a cron string.
        "schedule_is_asset_triggered": True,
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"load_states_to_pg", "dbt_deps", "dbt_run", "dbt_test"},
    },
    "tableize_states": {
        "schedule_is_asset_triggered": True,
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"load_pending_to_iceberg"},
    },
    "maintain_iceberg_states": {
        "schedule": "30 3 * * *",
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"expire_snapshots"},
    },
    "backfill_from_buffer": {
        "catchup": False,
        "max_active_runs": 1,
        "task_ids": {"sync_r2_to_garage"},
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
    missing = set(EXPECTED_DAGS) - set(dagbag.dag_ids)
    assert not missing, f"Missing DAGs: {missing}"


@pytest.mark.parametrize("dag_id, expected", list(EXPECTED_DAGS.items()))
def test_dag_structure(dagbag, dag_id, expected):
    """Each DAG matches its expected schedule, options, and task set."""
    dag = dagbag.get_dag(dag_id)
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
