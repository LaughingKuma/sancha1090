from __future__ import annotations

import json
from pathlib import Path

import pytest
from pyarrow.fs import LocalFileSystem

from include import adsb_discovery as ad


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "adsb" / "sample_adsb_state.parquet"
BUCKET = "sancha1090"
DT = "bronze/{stream}/dt=2026-05-29"


class FakeFS:
    """Minimal s3fs stand-in: an in-memory {key: bytes} store exposing find + cat."""

    def __init__(self, store: dict[str, bytes]):
        self.store = store

    def find(self, path: str) -> list[str]:
        return sorted(k for k in self.store if k.startswith(path))

    def cat(self, key: str) -> bytes:
        return self.store[key]


def _manifest_bytes(filename: str, stream: str, **extra) -> bytes:
    base = {
        "filename": filename, "stream": stream, "complete": True,
        "hostname": "sangenjaya-edge", "process_uuid": "5f3b0bb5-7da1-48d5-be0c-9cff1808a86f",
        "rotation_start_ts": "2026-05-29T00:00:00Z", "rotation_end_ts": "2026-05-29T01:00:00Z",
        "schema_version": 1,
    }
    base.update(extra)
    return json.dumps(base).encode()


def _key(stream: str, name: str) -> str:
    return f"{BUCKET}/{DT.format(stream=stream)}/{name}"


def _complete_adsb_store() -> dict[str, bytes]:
    stem = "sangenjaya-edge_adsb_state_2026-05-29T00_5f3b"
    return {
        _key("adsb_state", f"{stem}.parquet"): b"PARQUETBYTES",
        _key("adsb_state", f"{stem}.manifest.json"): _manifest_bytes(
            f"{stem}.parquet", "adsb_state", row_count=45800),
    }


def _complete_beast_store() -> dict[str, bytes]:
    stem = "sangenjaya-edge_beast_raw_2026-05-29T00_5f3b"
    return {
        _key("beast_raw", f"{stem}.beast.gz"): b"BEASTBYTES",
        _key("beast_raw", f"{stem}.beastidx.gz"): b"IDXBYTES",
        _key("beast_raw", f"{stem}.manifest.json"): _manifest_bytes(
            f"{stem}.beast.gz", "beast_raw", frame_count=423491, byte_count=4265615,
            beast_uncompressed_size=7818463),
    }


def test_lists_complete_adsb_and_beast_bundles():
    fs = FakeFS({**_complete_adsb_store(), **_complete_beast_store()})
    bundles = list(ad.list_remote_bundles(fs, BUCKET))
    by_stream = {b.stream: b for b in bundles}

    assert set(by_stream) == {"adsb_state", "beast_raw"}
    adsb = by_stream["adsb_state"]
    assert adsb.filename.endswith(".parquet")
    assert adsb.data_s3_uri.startswith("s3://sancha1090/bronze/adsb_state/")
    assert adsb.manifest_s3_uri.endswith(".manifest.json")
    assert adsb.extra_sidecar_s3_uris == []
    assert adsb.manifest["row_count"] == 45800

    beast = by_stream["beast_raw"]
    assert beast.filename.endswith(".beast.gz")
    assert len(beast.extra_sidecar_s3_uris) == 1
    assert beast.extra_sidecar_s3_uris[0].endswith(".beastidx.gz")


def test_skips_orphan_manifest_without_data():
    stem = "sangenjaya-edge_adsb_state_2026-05-29T00_5f3b"
    fs = FakeFS({_key("adsb_state", f"{stem}.manifest.json"): _manifest_bytes(
        f"{stem}.parquet", "adsb_state", row_count=1)})
    assert list(ad.list_remote_bundles(fs, BUCKET)) == []


def test_skips_data_without_manifest():
    stem = "sangenjaya-edge_adsb_state_2026-05-29T00_5f3b"
    fs = FakeFS({_key("adsb_state", f"{stem}.parquet"): b"X"})
    assert list(ad.list_remote_bundles(fs, BUCKET)) == []


def test_skips_manifest_not_marked_complete():
    stem = "sangenjaya-edge_adsb_state_2026-05-29T00_5f3b"
    fs = FakeFS({
        _key("adsb_state", f"{stem}.parquet"): b"X",
        _key("adsb_state", f"{stem}.manifest.json"): _manifest_bytes(
            f"{stem}.parquet", "adsb_state", complete=False, row_count=1),
    })
    assert list(ad.list_remote_bundles(fs, BUCKET)) == []


def test_skips_inprogress_data():
    stem = "sangenjaya-edge_adsb_state_2026-05-29T00_5f3b"
    # An .inprogress file with no manifest must never be yielded.
    fs = FakeFS({_key("adsb_state", f"{stem}.parquet.inprogress"): b"PARTIAL"})
    assert list(ad.list_remote_bundles(fs, BUCKET)) == []


def test_skips_beast_missing_sidecar():
    store = _complete_beast_store()
    # drop the .beastidx.gz sidecar
    del store[next(k for k in store if k.endswith(".beastidx.gz"))]
    fs = FakeFS(store)
    assert list(ad.list_remote_bundles(fs, BUCKET)) == []


def test_validate_bundle_passes_on_matching_rowcount():
    fs = FakeFS(_complete_adsb_store())
    bundle = next(iter(ad.list_remote_bundles(fs, BUCKET)))
    ad.validate_bundle(bundle, num_rows=45800)  # no raise


def test_validate_bundle_raises_on_rowcount_mismatch():
    fs = FakeFS(_complete_adsb_store())
    bundle = next(iter(ad.list_remote_bundles(fs, BUCKET)))
    with pytest.raises(ad.RowCountMismatch):
        ad.validate_bundle(bundle, num_rows=45799)


def test_validate_bundle_trusts_beast_manifest():
    fs = FakeFS(_complete_beast_store())
    bundle = next(iter(ad.list_remote_bundles(fs, BUCKET)))
    ad.validate_bundle(bundle, num_rows=None)  # beast: no rowcount check, no raise


def test_read_parquet_num_rows_reads_footer():
    # Real footer read against the real fixture, via LocalFileSystem (no S3 needed).
    assert ad.read_parquet_num_rows(LocalFileSystem(), str(FIXTURE)) == 5
