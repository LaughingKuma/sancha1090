from __future__ import annotations

from airflow.providers.standard.operators.bash import BashOperator

_DBT_CH = "cd /opt/airflow/dbt/sancha1090 && dbt {cmd} --profiles-dir . --target clickhouse --no-use-colors"


# selection is the full selector clause (--select/--exclude ...) so run and test stay byte-identical.
# dbt test (same selection as the run) gates the build — a run or data-quality failure reds the run.
def dbt_run_test(selection: str) -> tuple[BashOperator, BashOperator]:
    dbt_run_ch = BashOperator(
        task_id="dbt_run_ch",
        bash_command=_DBT_CH.format(cmd=f"run {selection}"),
    )
    dbt_test_ch = BashOperator(
        task_id="dbt_test_ch",
        bash_command=_DBT_CH.format(cmd=f"test {selection}"),
    )
    dbt_run_ch >> dbt_test_ch
    return dbt_run_ch, dbt_test_ch
