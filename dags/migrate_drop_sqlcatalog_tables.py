from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task


_SQLCATALOG_TABLES = ("public.iceberg_tables", "public.iceberg_namespace_properties")


@dag(
    dag_id="migrate_drop_sqlcatalog_tables",
    description="One-shot drop of the SqlCatalog backing tables now that Polaris is the only catalog",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 0,
    },
    tags=["sancha1090", "iceberg", "migration"],
)
def migrate_drop_sqlcatalog_tables():

    @task
    def drop_sqlcatalog_tables() -> dict:
        import sqlalchemy as sa
        from include import manifest

        eng = manifest._engine()
        before: dict[str, int | None] = {}
        with eng.begin() as conn:
            for name in _SQLCATALOG_TABLES:
                # to_regclass returns NULL for a missing table instead of erroring.
                exists = conn.execute(sa.text("SELECT to_regclass(:n)"), {"n": name}).scalar()
                before[name] = (
                    conn.execute(sa.text(f"SELECT count(*) FROM {name}")).scalar()
                    if exists is not None
                    else None
                )
        print(f"row counts before drop: {before}")

        with eng.begin() as conn:
            for name in _SQLCATALOG_TABLES:
                conn.execute(sa.text(f"DROP TABLE IF EXISTS {name}"))

        with eng.begin() as conn:
            remaining = {
                name: conn.execute(sa.text("SELECT to_regclass(:n)"), {"n": name}).scalar()
                for name in _SQLCATALOG_TABLES
            }
        still_present = [name for name, reg in remaining.items() if reg is not None]
        if still_present:
            raise RuntimeError(f"tables still present after drop: {still_present}")
        print(f"dropped {list(_SQLCATALOG_TABLES)}; confirmed gone")

        return {"before": before, "dropped": list(_SQLCATALOG_TABLES)}

    drop_sqlcatalog_tables()


migrate_drop_sqlcatalog_tables()
