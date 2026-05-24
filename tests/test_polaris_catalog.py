from __future__ import annotations


def test_polaris_catalog_properties_construct_from_env(monkeypatch):
    monkeypatch.setenv("POLARIS_ROOT_CLIENT_ID", "test-id")
    monkeypatch.setenv("POLARIS_ROOT_CLIENT_SECRET", "test-secret")
    monkeypatch.delenv("POLARIS_URL", raising=False)
    monkeypatch.setenv("S3_ENDPOINT", "garage:3900")
    monkeypatch.setenv("S3_ACCESS_KEY", "test-access")
    monkeypatch.setenv("S3_SECRET_KEY", "test-secret-key")

    from include import iceberg_rest

    props = iceberg_rest.polaris_catalog_properties()
    assert props["uri"] == "http://polaris:8181/api/catalog"
    assert props["warehouse"] == "opensky"
    assert props["credential"] == "test-id:test-secret"
    assert props["header.Polaris-Realm"] == "POLARIS"
    assert props["oauth2-server-uri"] == "http://polaris:8181/api/catalog/v1/oauth/tokens"
    # INC-5: empty access-delegation header suppresses pyiceberg's vended-credentials request.
    assert props["scope"] == "PRINCIPAL_ROLE:ALL"
    assert props["header.X-Iceberg-Access-Delegation"] == ""
    assert props["s3.endpoint"] == "http://garage:3900"
    assert props["s3.access-key-id"] == "test-access"
    assert props["s3.secret-access-key"] == "test-secret-key"
    assert props["s3.region"] == "garage"
