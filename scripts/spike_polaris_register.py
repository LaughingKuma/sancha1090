import os
import sys

import psycopg2
import requests
from pyiceberg.catalog.sql import SqlCatalog


POLARIS_BASE = "http://polaris:8181"
REALM = "POLARIS"
CATALOG = "opensky"
NAMESPACE = "bronze"
TABLE = "opensky_states"


def step(n: int, total: int, msg: str) -> None:
    print(f"[{n}/{total}] {msg}", flush=True)


def get_sqlcatalog_metadata_location() -> str:
    conn = psycopg2.connect(
        host=os.environ["ANALYTICS_PG_HOST"],
        port=os.environ["ANALYTICS_PG_PORT"],
        dbname=os.environ["ANALYTICS_PG_DB"],
        user=os.environ["ANALYTICS_PG_USER"],
        password=os.environ["ANALYTICS_PG_PASSWORD"],
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT metadata_location FROM iceberg_tables "
            "WHERE table_namespace=%s AND table_name=%s",
            (NAMESPACE, TABLE),
        )
        row = cur.fetchone()
    conn.close()
    if not row:
        sys.exit(f"SqlCatalog has no entry for {NAMESPACE}.{TABLE}")
    return row[0]


def polaris_token() -> str:
    r = requests.post(
        f"{POLARIS_BASE}/api/catalog/v1/oauth/tokens",
        data={
            "grant_type": "client_credentials",
            "client_id": os.environ["POLARIS_ROOT_CLIENT_ID"],
            "client_secret": os.environ["POLARIS_ROOT_CLIENT_SECRET"],
            "scope": "PRINCIPAL_ROLE:ALL",
        },
        headers={"Polaris-Realm": REALM},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def ensure_namespace(token: str) -> None:
    base = f"s3://{os.environ.get('S3_BUCKET', 'opensky')}/warehouse/bronze.db/"
    r = requests.post(
        f"{POLARIS_BASE}/api/catalog/v1/{CATALOG}/namespaces",
        json={
            "namespace": [NAMESPACE],
            "properties": {"location": base, "default-base-location": base},
        },
        headers={"Authorization": f"Bearer {token}", "Polaris-Realm": REALM},
        timeout=10,
    )
    if r.status_code not in (200, 409):
        sys.exit(f"namespace create failed: {r.status_code} {r.text}")


def register_table(token: str, metadata_location: str) -> int:
    r = requests.post(
        f"{POLARIS_BASE}/api/catalog/v1/{CATALOG}/namespaces/{NAMESPACE}/register",
        json={"name": TABLE, "metadata-location": metadata_location},
        headers={"Authorization": f"Bearer {token}", "Polaris-Realm": REALM},
        timeout=30,
    )
    if r.status_code == 409:
        print("     (already registered; reading current snapshot)", flush=True)
        return load_polaris_snapshot()
    if r.status_code != 200:
        sys.exit(f"registerTable failed: {r.status_code} {r.text}")
    return r.json()["metadata"]["current-snapshot-id"]


def load_sqlcatalog_snapshot() -> int:
    cat = SqlCatalog(
        "default",
        uri=(
            f"postgresql+psycopg2://{os.environ['ANALYTICS_PG_USER']}:"
            f"{os.environ['ANALYTICS_PG_PASSWORD']}@"
            f"{os.environ['ANALYTICS_PG_HOST']}:{os.environ['ANALYTICS_PG_PORT']}/"
            f"{os.environ['ANALYTICS_PG_DB']}"
        ),
        warehouse=f"s3://{os.environ.get('S3_BUCKET', 'opensky')}/warehouse",
        **{
            "s3.endpoint": f"http://{os.environ['S3_ENDPOINT']}",
            "s3.access-key-id": os.environ["S3_ACCESS_KEY"],
            "s3.secret-access-key": os.environ["S3_SECRET_KEY"],
            "s3.region": "garage",
        },
    )
    t = cat.load_table(f"{NAMESPACE}.{TABLE}")
    return t.current_snapshot().snapshot_id


def load_polaris_snapshot() -> int:
    token = polaris_token()
    r = requests.get(
        f"{POLARIS_BASE}/api/catalog/v1/{CATALOG}/namespaces/{NAMESPACE}/tables/{TABLE}",
        headers={"Authorization": f"Bearer {token}", "Polaris-Realm": REALM},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["metadata"]["current-snapshot-id"]


def main() -> None:
    step(1, 5, "Reading SqlCatalog metadata_location...")
    metadata = get_sqlcatalog_metadata_location()
    print(f"     → {metadata}")

    step(2, 5, "Obtaining Polaris OAuth2 token...")
    token = polaris_token()

    step(3, 5, "Ensuring Polaris namespace 'bronze'...")
    ensure_namespace(token)

    step(4, 5, "Registering bronze.opensky_states in Polaris...")
    polaris_snap = register_table(token, metadata)
    print(f"     → snapshot_id={polaris_snap}")

    step(5, 5, "Verifying snapshot parity...")
    sql_snap = load_sqlcatalog_snapshot()
    pol_snap = load_polaris_snapshot()
    if sql_snap != pol_snap:
        sys.exit(f"mismatch: sql={sql_snap} polaris={pol_snap}")
    print(f"     → SqlCatalog={sql_snap} Polaris={pol_snap} OK")

    print("OK")


if __name__ == "__main__":
    main()
