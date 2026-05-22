from __future__ import annotations

import os
import random
from typing import Optional
from urllib.parse import urlparse

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
    # INC-5: pyiceberg RestCatalog.load_table sends X-Iceberg-Access-Delegation:
    # vended-credentials, which Polaris cannot honor with stsUnavailable=true.
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


def _verify_data_files_intact(metadata_location: str, sample_size: int = 3) -> None:
    # Spike acceptance #7 step 4: HEAD a few data files from the new metadata
    # chain to prove the prior DELETE didn't touch Garage bytes. INC-side: must
    # compare info.type against FileType.File directly, not via str().
    from pyarrow.fs import FileType, S3FileSystem
    from pyiceberg.table import StaticTable

    table = StaticTable.from_metadata(metadata_location, properties=_s3_properties())
    data_files = [task.file.file_path for task in table.scan().plan_files()]
    if not data_files:
        raise RuntimeError(f"no data files referenced by {metadata_location}")

    fs = S3FileSystem(
        endpoint_override=f"http://{os.environ['S3_ENDPOINT']}",
        access_key=os.environ["S3_ACCESS_KEY"],
        secret_key=os.environ["S3_SECRET_KEY"],
        region="garage",
        scheme="http",
    )
    sampled = random.sample(data_files, min(sample_size, len(data_files)))
    bad: list[str] = []
    for uri in sampled:
        parsed = urlparse(uri)
        info = fs.get_file_info(f"{parsed.netloc}{parsed.path}")
        size = info.size or 0
        if info.type != FileType.File or size <= 0:
            bad.append(f"{uri} type={info.type} size={size}")
    if bad:
        raise RuntimeError(f"data files missing or zero-size: {bad}")


def sync_polaris_pointer(metadata_location: str) -> dict:
    # Spike acceptance #7's verified API path (promotion doc § "Verified API
    # path"). registerTable+overwrite and commitTable+set-snapshot-ref both
    # fail empirically; the working sequence is drop(purgeRequested=false) +
    # re-register. Snapshot parity GET is raw requests, not RestCatalog —
    # pyiceberg sends X-Iceberg-Access-Delegation: vended-credentials, which
    # Polaris can't honor under stsUnavailable=true (INC-5).
    token = polaris_token()
    ensure_bronze_namespace(token)

    current = load_polaris_table(token)
    if current is not None and current["metadata-location"] == metadata_location:
        return {
            "action": "noop",
            "metadata_location": metadata_location,
            "snapshot_id": current["metadata"]["current-snapshot-id"],
        }

    _verify_data_files_intact(metadata_location)
    drop_bronze_table(token)
    registered_snap = register_bronze_table(metadata_location, token)
    parity_snap = load_polaris_snapshot(token)
    if parity_snap != registered_snap:
        raise RuntimeError(
            f"parity check failed: register returned {registered_snap} "
            f"but GET returned {parity_snap}"
        )
    return {
        "action": "repointed",
        "metadata_location": metadata_location,
        "snapshot_id": registered_snap,
    }
