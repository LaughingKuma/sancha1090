from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task


@dag(
    dag_id="backup_polaris",
    description="Daily pg_dump of Polaris's JDBC schema into Garage (v2.10)",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    schedule="0 2 * * *",
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
    },
    tags=["sancha1090", "polaris", "backup", "v2"],
)
def backup_polaris():

    @task
    def dump_to_garage() -> str:
        import gzip
        import os
        import subprocess

        from include.s3_helpers import get_bucket, get_s3fs

        # postgres-analytics also backs Polaris's catalog metastore; this schema is it
        schema = os.environ.get("POLARIS_DB_SCHEMA", "polaris_schema")
        stamp = pendulum.now("UTC").format("YYYYMMDD-HHmm")
        key = f"{get_bucket()}/backups/polaris/polaris-{stamp}.sql.gz"

        env = {**os.environ, "PGPASSWORD": os.environ["ANALYTICS_PG_PASSWORD"]}
        proc = subprocess.run(
            [
                "pg_dump",
                "-h", os.environ["ANALYTICS_PG_HOST"],
                "-p", os.environ.get("ANALYTICS_PG_PORT", "5432"),
                "-U", os.environ["ANALYTICS_PG_USER"],
                "-d", os.environ["ANALYTICS_PG_DB"],
                "-n", schema,
            ],
            env=env,
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pg_dump of {schema} failed: {proc.stderr.decode(errors='replace')}")

        # the image's pg_dump (18) emits SET transaction_timeout (a PG17+ GUC) which the
        # PG15 postgres-analytics server rejects; drop it so the dump restores cleanly
        sql = b"\n".join(
            ln for ln in proc.stdout.split(b"\n")
            if not ln.startswith(b"SET transaction_timeout")
        )

        # the catalog metastore is small; buffer + gzip in memory, push via the proven s3fs path
        fs = get_s3fs()
        with fs.open(key, "wb") as f:
            f.write(gzip.compress(sql))

        uri = f"s3://{key}"
        print(f"Backed up {schema} to {uri} ({len(proc.stdout)} raw bytes)")
        return uri

    dump_to_garage()


backup_polaris()
