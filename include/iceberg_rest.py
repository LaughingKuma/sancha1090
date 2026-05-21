from __future__ import annotations

import os
from typing import Optional

import requests
from pyiceberg.catalog.rest import RestCatalog


CATALOG = "opensky"
REALM = "POLARIS"
NAMESPACE = "bronze"
TABLE = "opensky_states"


def _base() -> str:
    return os.environ.get("POLARIS_URL", "http://polaris:8181")


def polaris_catalog_properties() -> dict:
    return {
        "uri": f"{_base()}/api/catalog",
        "credential": (
            f"{os.environ['POLARIS_ROOT_CLIENT_ID']}:"
            f"{os.environ['POLARIS_ROOT_CLIENT_SECRET']}"
        ),
        "warehouse": CATALOG,
        "header.Polaris-Realm": REALM,
        # Explicit OAuth endpoint silences pyiceberg's deprecation fallback warning.
        "oauth2-server-uri": f"{_base()}/api/catalog/v1/oauth/tokens",
    }


def get_polaris_catalog() -> RestCatalog:
    return RestCatalog("polaris", **polaris_catalog_properties())


def polaris_token() -> str:
    r = requests.post(
        f"{_base()}/api/catalog/v1/oauth/tokens",
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


def ensure_bronze_namespace(token: Optional[str] = None) -> None:
    tok = token or polaris_token()
    bucket = os.environ.get("S3_BUCKET", "opensky")
    # INC-3: Polaris validates `location`, not `default-base-location` — set both.
    base = f"s3://{bucket}/warehouse/bronze.db/"
    r = requests.post(
        f"{_base()}/api/catalog/v1/{CATALOG}/namespaces",
        json={
            "namespace": [NAMESPACE],
            "properties": {"location": base, "default-base-location": base},
        },
        headers={"Authorization": f"Bearer {tok}", "Polaris-Realm": REALM},
        timeout=10,
    )
    if r.status_code not in (200, 409):
        r.raise_for_status()


def register_bronze_table(
    metadata_location: str,
    token: Optional[str] = None,
) -> dict:
    tok = token or polaris_token()
    r = requests.post(
        f"{_base()}/api/catalog/v1/{CATALOG}/namespaces/{NAMESPACE}/register",
        json={"name": TABLE, "metadata-location": metadata_location},
        headers={"Authorization": f"Bearer {tok}", "Polaris-Realm": REALM},
        timeout=30,
    )
    if r.status_code == 409:
        return {"status": "already_registered", "snapshot_id": load_polaris_snapshot(tok)}
    r.raise_for_status()
    return {
        "status": "registered",
        "snapshot_id": r.json()["metadata"]["current-snapshot-id"],
    }


def load_polaris_snapshot(token: Optional[str] = None) -> int:
    # INC-5: pyiceberg RestCatalog.load_table sends X-Iceberg-Access-Delegation:
    # vended-credentials, which Polaris cannot honor with stsUnavailable=true.
    tok = token or polaris_token()
    r = requests.get(
        f"{_base()}/api/catalog/v1/{CATALOG}/namespaces/{NAMESPACE}/tables/{TABLE}",
        headers={"Authorization": f"Bearer {tok}", "Polaris-Realm": REALM},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["metadata"]["current-snapshot-id"]
