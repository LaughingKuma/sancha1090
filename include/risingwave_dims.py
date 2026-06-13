from __future__ import annotations

import csv
import sys

from psycopg2.extras import execute_values

from include.db import rw_connect

# the same dbt seed CSVs that feed silver — one data source, no live/batch dim drift
# dim_hex_country stays LAST: it expands into dim_hex_country_buckets, and the --if-empty guard
# treats bucket rows as proof a prior seed fully completed — so every other table must load first.
SEEDS = {
    "dim_airlines": ("dim_airlines.csv", ("icao", "iata", "name", "callsign", "country", "active")),
    "dim_aircraft_types": ("dim_aircraft_types.csv", ("typecode", "engines", "body_class", "model_name")),
    "dim_hex_country": ("dim_hex_country.csv", ("block_lo", "block_hi", "country")),
}
INT_COLS = {"block_lo", "block_hi", "engines"}
BUCKET_BITS = 12  # 4096-address buckets; must match the /4096 in mv_current_aircraft's join
SEEDS_DIR = "/opt/airflow/dbt/sancha1090/seeds"


def load_dims(only_if_empty: bool = False) -> dict[str, int]:
    conn = rw_connect()
    loaded: dict[str, int] = {}

    def swap(cur, table: str, cols: tuple[str, ...], rows: list[tuple]) -> None:
        if not rows:
            raise RuntimeError(f"0 rows for {table} — refusing to empty it")
        # DELETE+INSERT, not ALTER TABLE SWAP: RW MVs bind to tables by object id, so a
        # staging swap leaves the MV joining the stale table (verified on v2.8.4). Rows
        # are parsed BEFORE the delete, so a bad CSV can't empty a table.
        cur.execute(f"DELETE FROM {table}")
        execute_values(cur, f"INSERT INTO {table} ({', '.join(cols)}) VALUES %s", rows)
        # RW DML is async-applied; FLUSH makes the swap visible before we verify
        cur.execute("FLUSH")
        cur.execute(f"SELECT count(*) FROM {table}")
        got = cur.fetchone()[0]
        if got != len(rows):
            raise RuntimeError(f"{table}: loaded {got} rows, expected {len(rows)}")
        loaded[table] = got

    with conn, conn.cursor() as cur:
        if only_if_empty:
            # buckets load last, so rows there mean a prior seed fully completed
            cur.execute("SELECT count(*) FROM dim_hex_country_buckets")
            if cur.fetchone()[0] > 0:
                print("dims already seeded — skipping (--if-empty)")
                return loaded
        for table, (fname, cols) in SEEDS.items():
            with open(f"{SEEDS_DIR}/{fname}", newline="", encoding="utf-8") as f:
                rows = [
                    tuple(int(r[c]) if c in INT_COLS else (r[c] or None) for c in cols)
                    for r in csv.DictReader(f)
                ]
            swap(cur, table, cols, rows)
            if table == "dim_hex_country":
                # bucket expansion for the MV's streaming equi-join (see 02_dims.sql)
                buckets = [
                    (b, lo, hi, country)
                    for lo, hi, country in rows
                    for b in range(lo >> BUCKET_BITS, (hi >> BUCKET_BITS) + 1)
                ]
                swap(cur, "dim_hex_country_buckets",
                     ("bucket", "block_lo", "block_hi", "country"), buckets)
    print(f"reloaded: {loaded}")
    return loaded


if __name__ == "__main__":
    load_dims(only_if_empty="--if-empty" in sys.argv)
