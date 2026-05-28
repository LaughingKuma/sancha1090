"""Tests for s3_helpers env-var handling."""

from __future__ import annotations

import pytest

from include import s3_helpers


def test_get_bucket_reads_s3_bucket(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "my-bucket")
    assert s3_helpers.get_bucket() == "my-bucket"


def test_get_bucket_default(monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    assert s3_helpers.get_bucket() == "sancha1090"


def test_get_s3fs_requires_endpoint(monkeypatch):
    monkeypatch.delenv("S3_ENDPOINT", raising=False)
    monkeypatch.setenv("S3_ACCESS_KEY", "x")
    monkeypatch.setenv("S3_SECRET_KEY", "y")
    with pytest.raises(KeyError):
        s3_helpers.get_s3fs()


def test_get_s3fs_requires_access_key(monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT", "garage:3900")
    monkeypatch.delenv("S3_ACCESS_KEY", raising=False)
    monkeypatch.setenv("S3_SECRET_KEY", "y")
    with pytest.raises(KeyError):
        s3_helpers.get_s3fs()
