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


def test_garage_pyarrow_fs_requires_endpoint(monkeypatch):
    monkeypatch.delenv("S3_ENDPOINT", raising=False)
    monkeypatch.setenv("S3_ACCESS_KEY", "x")
    monkeypatch.setenv("S3_SECRET_KEY", "y")
    with pytest.raises(KeyError):
        s3_helpers.garage_pyarrow_fs()


def test_garage_pyarrow_fs_requires_access_key(monkeypatch):
    monkeypatch.setenv("S3_ENDPOINT", "garage:3900")
    monkeypatch.delenv("S3_ACCESS_KEY", raising=False)
    monkeypatch.setenv("S3_SECRET_KEY", "y")
    with pytest.raises(KeyError):
        s3_helpers.garage_pyarrow_fs()


def test_read_pending_frames_skips_object_missing_in_garage(monkeypatch):
    # A manifest entry whose Garage object is missing (FileNotFoundError) must not wedge the whole batch —
    # it is skipped and left out of `good`, so the present files still load and only they get marked.
    import pyarrow as pa

    present = pa.table({"snapshot_time": [1, 2]})

    def fake_read(path, **_kw):
        if "missing" in path:
            raise FileNotFoundError(path)
        return present

    monkeypatch.setattr(s3_helpers.pq, "read_table", fake_read)
    pending = [
        {"object_uri": "s3://b/bronze/states_raw/missing.parquet"},
        {"object_uri": "s3://b/bronze/states_raw/ok.parquet"},
    ]
    frames, good = s3_helpers.read_pending_frames(object(), pending)
    assert len(frames) == 1
    assert [g["object_uri"] for g in good] == ["s3://b/bronze/states_raw/ok.parquet"]


def test_read_pending_frames_rejects_non_s3_uri():
    with pytest.raises(ValueError, match="non-s3 manifest URI"):
        s3_helpers.read_pending_frames(object(), [{"object_uri": "/local/x.parquet"}])
