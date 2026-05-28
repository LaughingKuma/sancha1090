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
        # Polaris rejects token requests without an explicit scope.
        "scope": "PRINCIPAL_ROLE:ALL",
        # INC-5: empty pre-empts pyiceberg's setdefault of "vended-credentials",
        # which Polaris cannot honor under stsUnavailable=true. Client-side s3.* below feeds FileIO.
        "header.X-Iceberg-Access-Delegation": "",
        **_s3_properties(),
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
    bucket = os.environ.get("S3_BUCKET", "sancha1090")
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
    r.raise_for_status()
    return r.json()["metadata"]["current-snapshot-id"]


def drop_bronze_table(token: Optional[str] = None) -> None:
    # purgeRequested=false leaves Garage data files untouched (spike acceptance #7).
    tok = token or polaris_token()
    r = requests.delete(
        f"{_base()}/api/catalog/v1/{CATALOG}/namespaces/{NAMESPACE}/tables/{TABLE}",
        params={"purgeRequested": "false"},
        headers={"Authorization": f"Bearer {tok}", "Polaris-Realm": REALM},
        timeout=30,
    )
    if r.status_code not in (204, 404):
        r.raise_for_status()


def load_polaris_table(token: Optional[str] = None) -> Optional[dict]:
    # Raw GET sidesteps pyiceberg entirely — useful for register_bronze_in_polaris recovery
    # paths where we want metadata-location without instantiating a Table object.
    tok = token or polaris_token()
    r = requests.get(
        f"{_base()}/api/catalog/v1/{CATALOG}/namespaces/{NAMESPACE}/tables/{TABLE}",
        headers={"Authorization": f"Bearer {tok}", "Polaris-Realm": REALM},
        timeout=30,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def load_polaris_snapshot(token: Optional[str] = None) -> int:
    table = load_polaris_table(token)
    if table is None:
        raise RuntimeError("bronze.opensky_states not registered in Polaris")
    return table["metadata"]["current-snapshot-id"]


def _s3_properties() -> dict:
    return {
        "s3.endpoint": f"http://{os.environ['S3_ENDPOINT']}",
        "s3.access-key-id": os.environ["S3_ACCESS_KEY"],
        "s3.secret-access-key": os.environ["S3_SECRET_KEY"],
        "s3.region": "garage",
    }
