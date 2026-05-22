from __future__ import annotations

import pytest


def test_polaris_catalog_properties_construct_from_env(monkeypatch):
    monkeypatch.setenv("POLARIS_ROOT_CLIENT_ID", "test-id")
    monkeypatch.setenv("POLARIS_ROOT_CLIENT_SECRET", "test-secret")
    monkeypatch.delenv("POLARIS_URL", raising=False)

    from include import iceberg_rest

    props = iceberg_rest.polaris_catalog_properties()
    assert props["uri"] == "http://polaris:8181/api/catalog"
    assert props["warehouse"] == "opensky"
    assert props["credential"] == "test-id:test-secret"
    assert props["header.Polaris-Realm"] == "POLARIS"
    assert props["oauth2-server-uri"] == "http://polaris:8181/api/catalog/v1/oauth/tokens"


def test_polaris_snapshot_is_known_to_sqlcatalog():
    # v2.1 has no continuous sync; between manual register_bronze_in_polaris runs,
    # SqlCatalog advances past Polaris. The real correctness gate is that Polaris's
    # pointer still resolves to a snapshot in SqlCatalog's history. v2.3 closes the
    # drift with sync_polaris_pointer.
    try:
        from include import iceberg as ib
        from include import iceberg_rest as rest

        table = ib.get_catalog().load_table(ib.QUALIFIED)
        known = {s.snapshot_id for s in table.snapshots()}
        pol_snap = rest.load_polaris_snapshot()
    except Exception as exc:
        pytest.skip(f"polaris/sqlcatalog not reachable from this host: {exc}")

    assert pol_snap in known, (
        f"Polaris snapshot {pol_snap} is not in SqlCatalog's history "
        f"({sorted(known)}); re-run register_bronze_in_polaris."
    )
