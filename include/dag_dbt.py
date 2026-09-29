from __future__ import annotations

from airflow.providers.standard.operators.bash import BashOperator

# One slot: the weekly full-refresh and hourly builds write the same tables, so never together.
ADSB_SILVER_POOL = "adsb_silver_single_writer"

_DBT_CH = "cd /opt/airflow/dbt/sancha1090 && dbt {cmd} --profiles-dir . --target clickhouse --no-use-colors"


# selection is the full selector clause (--select/--exclude ...) so run and test stay byte-identical.
# dbt test (same selection as the run) gates the build — a run or data-quality failure reds the run.
def dbt_run_test(
    selection: str, run_flags: str = "", pool: str | None = None
) -> tuple[BashOperator, BashOperator]:
    # run_flags reach `dbt run` only: `dbt test` rejects run-only flags such as --full-refresh.
    run_cmd = f"run {selection} {run_flags}" if run_flags else f"run {selection}"
    # Only the writer takes the pool; omitted, the operator keeps its default pool.
    pool_kw = {"pool": pool} if pool else {}
    dbt_run_ch = BashOperator(
        task_id="dbt_run_ch",
        bash_command=_DBT_CH.format(cmd=run_cmd),
        **pool_kw,
    )
    dbt_test_ch = BashOperator(
        task_id="dbt_test_ch",
        bash_command=_DBT_CH.format(cmd=f"test {selection}"),
    )
    dbt_run_ch >> dbt_test_ch
    return dbt_run_ch, dbt_test_ch
