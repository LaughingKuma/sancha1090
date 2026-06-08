from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_iceberg_module_has_no_sqlcatalog_import():
    src = (REPO_ROOT / "include" / "iceberg.py").read_text()
    assert "SqlCatalog" not in src, (
        "include/iceberg.py still references SqlCatalog; Polaris is the only catalog after v2.4-5."
    )


def test_tableize_states_has_single_task(dagbag):
    # .dags reads the parsed bag (DB-free); get_dag() would hit the metadata DB and need a live stack.
    dag = dagbag.dags.get("tableize_states")
    assert dag is not None, "tableize_states DAG not found"
    task_ids = {t.task_id for t in dag.tasks}
    assert task_ids == {"load_pending_to_iceberg"}, (
        f"tableize_states should be single-task after sync_polaris removal; got {sorted(task_ids)}"
    )
