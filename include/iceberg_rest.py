from __future__ import annotations

import os

from pyiceberg.catalog.rest import RestCatalog


CATALOG = "opensky"
REALM = "POLARIS"


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


def _s3_properties() -> dict:
    return {
        "s3.endpoint": f"http://{os.environ['S3_ENDPOINT']}",
        "s3.access-key-id": os.environ["S3_ACCESS_KEY"],
        "s3.secret-access-key": os.environ["S3_SECRET_KEY"],
        "s3.region": "garage",
    }
