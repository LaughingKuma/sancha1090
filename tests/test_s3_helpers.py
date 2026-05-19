from __future__ import annotations

import pytest

from include import s3_helpers


def test_get_bucket_prefers_s3_var(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "new-bucket")
    monkeypatch.setenv("MINIO_BUCKET", "old-bucket")
    assert s3_helpers.get_bucket() == "new-bucket"


def test_get_bucket_falls_back_to_minio_var(monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setenv("MINIO_BUCKET", "old-bucket")
    assert s3_helpers.get_bucket() == "old-bucket"


def test_get_bucket_default_when_neither_set(monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("MINIO_BUCKET", raising=False)
    assert s3_helpers.get_bucket() == "opensky"


def test_get_bucket_treats_empty_s3_as_unset(monkeypatch):
    # Compose passes through empty strings when an .env var is unset, so
    # empty S3_BUCKET must fall back to MINIO_BUCKET rather than win.
    monkeypatch.setenv("S3_BUCKET", "")
    monkeypatch.setenv("MINIO_BUCKET", "old-bucket")
    assert s3_helpers.get_bucket() == "old-bucket"


def test_get_s3_endpoint_prefers_s3_var(monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT", "garage:3900")
    monkeypatch.setenv("MINIO_ENDPOINT", "minio:9000")
    assert s3_helpers._get_endpoint() == "garage:3900"


def test_get_s3_endpoint_falls_back_to_minio(monkeypatch):
    monkeypatch.delenv("S3_ENDPOINT", raising=False)
    monkeypatch.setenv("MINIO_ENDPOINT", "minio:9000")
    assert s3_helpers._get_endpoint() == "minio:9000"


def test_get_s3_endpoint_raises_when_neither_set(monkeypatch):
    monkeypatch.delenv("S3_ENDPOINT", raising=False)
    monkeypatch.delenv("MINIO_ENDPOINT", raising=False)
    with pytest.raises(KeyError, match="S3_ENDPOINT"):
        s3_helpers._get_endpoint()
