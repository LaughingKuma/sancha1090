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


def _manifest_bytes(
    filename: str, stream: str,
    process_uuid: str = "5f3b0bb5-7da1-48d5-be0c-9cff1808a86f",
    process_start_ts: str = "2026-05-29T00:00:00Z",
    **extra,
) -> bytes:
    base = {
        "filename": filename, "stream": stream, "complete": True,
        "hostname": "sangenjaya-edge", "process_uuid": process_uuid,
        "process_start_ts": process_start_ts,
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


def test_superseded_partial_from_dead_process_is_yielded():
    # A newer process completing a rotation proves the old writer died — its partial is final.
    partial_stem = "sangenjaya-edge_adsb_state_2026-05-29T09_partial"
    complete_stem = "sangenjaya-edge_adsb_state_2026-05-29T10_complete"
    store = {
        _key("adsb_state", f"{partial_stem}.parquet"): b"X",
        _key("adsb_state", f"{partial_stem}.manifest.json"): _manifest_bytes(
            f"{partial_stem}.parquet", "adsb_state", process_uuid="uuid-A",
            process_start_ts="2026-05-29T09:00:00Z", complete=False, row_count=100),
        _key("adsb_state", f"{complete_stem}.parquet"): b"Y",
        _key("adsb_state", f"{complete_stem}.manifest.json"): _manifest_bytes(
            f"{complete_stem}.parquet", "adsb_state", process_uuid="uuid-B",
            process_start_ts="2026-05-29T10:00:00Z", row_count=200),
    }
    fs = FakeFS(store)
    filenames = {b.filename for b in ad.list_remote_bundles(fs, BUCKET)}
    assert filenames == {f"{partial_stem}.parquet", f"{complete_stem}.parquet"}


def test_partial_from_newest_process_not_yielded():
    # Live in-progress case: no complete bundle is newer, so the partial can't be proven dead.
    complete_stem = "sangenjaya-edge_adsb_state_2026-05-29T09_complete"
    partial_stem = "sangenjaya-edge_adsb_state_2026-05-29T10_partial"
    store = {
        _key("adsb_state", f"{complete_stem}.parquet"): b"X",
        _key("adsb_state", f"{complete_stem}.manifest.json"): _manifest_bytes(
            f"{complete_stem}.parquet", "adsb_state", process_uuid="uuid-B",
            process_start_ts="2026-05-29T09:00:00Z", row_count=100),
        _key("adsb_state", f"{partial_stem}.parquet"): b"Y",
        _key("adsb_state", f"{partial_stem}.manifest.json"): _manifest_bytes(
            f"{partial_stem}.parquet", "adsb_state", process_uuid="uuid-A",
            process_start_ts="2026-05-29T10:00:00Z", complete=False, row_count=200),
    }
    fs = FakeFS(store)
    filenames = {b.filename for b in ad.list_remote_bundles(fs, BUCKET)}
    assert filenames == {f"{complete_stem}.parquet"}


def test_partial_same_uuid_as_complete_not_yielded():
    # An unflipped-manifest producer bug (same process, still marked incomplete) must stay
    # visible to the gate, not silently ingested.
    partial_stem = "sangenjaya-edge_adsb_state_2026-05-29T09_partial"
    complete_stem = "sangenjaya-edge_adsb_state_2026-05-29T10_complete"
    store = {
        _key("adsb_state", f"{partial_stem}.parquet"): b"X",
        _key("adsb_state", f"{partial_stem}.manifest.json"): _manifest_bytes(
            f"{partial_stem}.parquet", "adsb_state", process_uuid="uuid-A",
            process_start_ts="2026-05-29T09:00:00Z", complete=False, row_count=100),
        _key("adsb_state", f"{complete_stem}.parquet"): b"Y",
        _key("adsb_state", f"{complete_stem}.manifest.json"): _manifest_bytes(
            f"{complete_stem}.parquet", "adsb_state", process_uuid="uuid-A",
            process_start_ts="2026-05-29T10:00:00Z", row_count=200),
    }
    fs = FakeFS(store)
    filenames = {b.filename for b in ad.list_remote_bundles(fs, BUCKET)}
    assert filenames == {f"{complete_stem}.parquet"}


def test_beast_complete_does_not_supersede_adsb_partial():
    # Cross-stream isolation: a beast_raw completion says nothing about the adsb_state writer.
    partial_stem = "sangenjaya-edge_adsb_state_2026-05-29T09_partial"
    store = {
        _key("adsb_state", f"{partial_stem}.parquet"): b"X",
        _key("adsb_state", f"{partial_stem}.manifest.json"): _manifest_bytes(
            f"{partial_stem}.parquet", "adsb_state", process_uuid="uuid-A",
            process_start_ts="2026-05-29T09:00:00Z", complete=False, row_count=100),
        **_complete_beast_store(),
    }
    fs = FakeFS(store)
    filenames = {b.filename for b in ad.list_remote_bundles(fs, BUCKET)}
    assert f"{partial_stem}.parquet" not in filenames
    assert any(f.endswith(".beast.gz") for f in filenames)


def test_superseded_partial_missing_data_file_not_yielded():
    # Superseded-by-identity still must clear the ordinary integrity guards.
    partial_stem = "sangenjaya-edge_adsb_state_2026-05-29T09_partial"
    complete_stem = "sangenjaya-edge_adsb_state_2026-05-29T10_complete"
    store = {
        # no parquet for the partial — data never landed.
        _key("adsb_state", f"{partial_stem}.manifest.json"): _manifest_bytes(
            f"{partial_stem}.parquet", "adsb_state", process_uuid="uuid-A",
            process_start_ts="2026-05-29T09:00:00Z", complete=False, row_count=100),
        _key("adsb_state", f"{complete_stem}.parquet"): b"Y",
        _key("adsb_state", f"{complete_stem}.manifest.json"): _manifest_bytes(
            f"{complete_stem}.parquet", "adsb_state", process_uuid="uuid-B",
            process_start_ts="2026-05-29T10:00:00Z", row_count=200),
    }
    fs = FakeFS(store)
    filenames = {b.filename for b in ad.list_remote_bundles(fs, BUCKET)}
    assert filenames == {f"{complete_stem}.parquet"}


def test_partial_without_start_ts_not_yielded():
    partial_stem = "sangenjaya-edge_adsb_state_2026-05-29T09_partial"
    complete_stem = "sangenjaya-edge_adsb_state_2026-05-29T10_complete"
    store = {
        _key("adsb_state", f"{partial_stem}.parquet"): b"X",
        _key("adsb_state", f"{partial_stem}.manifest.json"): _manifest_bytes(
            f"{partial_stem}.parquet", "adsb_state", process_uuid="uuid-A",
            process_start_ts=None, complete=False, row_count=100),
        _key("adsb_state", f"{complete_stem}.parquet"): b"Y",
        _key("adsb_state", f"{complete_stem}.manifest.json"): _manifest_bytes(
            f"{complete_stem}.parquet", "adsb_state", process_uuid="uuid-B",
            process_start_ts="2026-05-29T10:00:00Z", row_count=200),
    }
    fs = FakeFS(store)
    filenames = {b.filename for b in ad.list_remote_bundles(fs, BUCKET)}
    assert filenames == {f"{complete_stem}.parquet"}


def test_all_complete_without_start_ts_partial_not_yielded():
    partial_stem = "sangenjaya-edge_adsb_state_2026-05-29T09_partial"
    complete_stem = "sangenjaya-edge_adsb_state_2026-05-29T10_complete"
    store = {
        _key("adsb_state", f"{partial_stem}.parquet"): b"X",
        _key("adsb_state", f"{partial_stem}.manifest.json"): _manifest_bytes(
            f"{partial_stem}.parquet", "adsb_state", process_uuid="uuid-A",
            process_start_ts="2026-05-29T09:00:00Z", complete=False, row_count=100),
        _key("adsb_state", f"{complete_stem}.parquet"): b"Y",
        _key("adsb_state", f"{complete_stem}.manifest.json"): _manifest_bytes(
            f"{complete_stem}.parquet", "adsb_state", process_uuid="uuid-B",
            process_start_ts=None, row_count=200),
    }
    fs = FakeFS(store)
    filenames = {b.filename for b in ad.list_remote_bundles(fs, BUCKET)}
    assert filenames == {f"{complete_stem}.parquet"}


def test_partial_with_naive_start_ts_not_yielded_no_exception():
    # A naive timestamp (producer bug, missing Z/offset) must not TypeError against an aware
    # peer — one malformed manifest can't be allowed to kill the whole discovery generator.
    partial_stem = "sangenjaya-edge_adsb_state_2026-07-12T06_partial"
    complete_stem = "sangenjaya-edge_adsb_state_2026-07-12T10_complete"
    store = {
        _key("adsb_state", f"{partial_stem}.parquet"): b"X",
        _key("adsb_state", f"{partial_stem}.manifest.json"): _manifest_bytes(
            f"{partial_stem}.parquet", "adsb_state", process_uuid="uuid-A",
            process_start_ts="2026-07-12T06:00:00", complete=False, row_count=100),
        _key("adsb_state", f"{complete_stem}.parquet"): b"Y",
        _key("adsb_state", f"{complete_stem}.manifest.json"): _manifest_bytes(
            f"{complete_stem}.parquet", "adsb_state", process_uuid="uuid-B",
            process_start_ts="2026-07-12T10:00:00Z", row_count=200),
    }
    fs = FakeFS(store)
    filenames = {b.filename for b in ad.list_remote_bundles(fs, BUCKET)}
    assert filenames == {f"{complete_stem}.parquet"}


def test_partial_with_missing_process_uuid_not_yielded():
    # No identity on the partial means no proof-of-death, regardless of timestamps.
    partial_stem = "sangenjaya-edge_adsb_state_2026-05-29T09_partial"
    complete_stem = "sangenjaya-edge_adsb_state_2026-05-29T10_complete"
    store = {
        _key("adsb_state", f"{partial_stem}.parquet"): b"X",
        _key("adsb_state", f"{partial_stem}.manifest.json"): _manifest_bytes(
            f"{partial_stem}.parquet", "adsb_state", process_uuid=None,
            process_start_ts="2026-05-29T09:00:00Z", complete=False, row_count=100),
        _key("adsb_state", f"{complete_stem}.parquet"): b"Y",
        _key("adsb_state", f"{complete_stem}.manifest.json"): _manifest_bytes(
            f"{complete_stem}.parquet", "adsb_state", process_uuid="uuid-B",
            process_start_ts="2026-05-29T10:00:00Z", row_count=200),
    }
    fs = FakeFS(store)
    filenames = {b.filename for b in ad.list_remote_bundles(fs, BUCKET)}
    assert filenames == {f"{complete_stem}.parquet"}


def test_partial_with_non_string_start_ts_not_yielded_no_exception():
    # fromisoformat raises TypeError (not ValueError) on a non-string; a JSON number/array/object
    # is truthy so it clears the `if not value` guard and must still be treated as unparseable.
    partial_stem = "sangenjaya-edge_adsb_state_2026-05-29T09_partial"
    complete_stem = "sangenjaya-edge_adsb_state_2026-05-29T10_complete"
    store = {
        _key("adsb_state", f"{partial_stem}.parquet"): b"X",
        _key("adsb_state", f"{partial_stem}.manifest.json"): _manifest_bytes(
            f"{partial_stem}.parquet", "adsb_state", process_uuid="uuid-A",
            process_start_ts=123, complete=False, row_count=100),
        _key("adsb_state", f"{complete_stem}.parquet"): b"Y",
        _key("adsb_state", f"{complete_stem}.manifest.json"): _manifest_bytes(
            f"{complete_stem}.parquet", "adsb_state", process_uuid="uuid-B",
            process_start_ts="2026-05-29T10:00:00Z", row_count=200),
    }
    fs = FakeFS(store)
    filenames = {b.filename for b in ad.list_remote_bundles(fs, BUCKET)}
    assert filenames == {f"{complete_stem}.parquet"}


def test_complete_with_non_string_start_ts_partial_not_yielded_no_exception():
    partial_stem = "sangenjaya-edge_adsb_state_2026-05-29T09_partial"
    complete_stem = "sangenjaya-edge_adsb_state_2026-05-29T10_complete"
    store = {
        _key("adsb_state", f"{partial_stem}.parquet"): b"X",
        _key("adsb_state", f"{partial_stem}.manifest.json"): _manifest_bytes(
            f"{partial_stem}.parquet", "adsb_state", process_uuid="uuid-A",
            process_start_ts="2026-05-29T09:00:00Z", complete=False, row_count=100),
        _key("adsb_state", f"{complete_stem}.parquet"): b"Y",
        _key("adsb_state", f"{complete_stem}.manifest.json"): _manifest_bytes(
            f"{complete_stem}.parquet", "adsb_state", process_uuid="uuid-B",
            process_start_ts=123, row_count=200),
    }
    fs = FakeFS(store)
    filenames = {b.filename for b in ad.list_remote_bundles(fs, BUCKET)}
    assert filenames == {f"{complete_stem}.parquet"}


def test_complete_with_missing_process_uuid_does_not_supersede():
    # An identity-less successor is not proof a DIFFERENT process exists.
    partial_stem = "sangenjaya-edge_adsb_state_2026-05-29T09_partial"
    complete_stem = "sangenjaya-edge_adsb_state_2026-05-29T10_complete"
    store = {
        _key("adsb_state", f"{partial_stem}.parquet"): b"X",
        _key("adsb_state", f"{partial_stem}.manifest.json"): _manifest_bytes(
            f"{partial_stem}.parquet", "adsb_state", process_uuid="uuid-A",
            process_start_ts="2026-05-29T09:00:00Z", complete=False, row_count=100),
        _key("adsb_state", f"{complete_stem}.parquet"): b"Y",
        _key("adsb_state", f"{complete_stem}.manifest.json"): _manifest_bytes(
            f"{complete_stem}.parquet", "adsb_state", process_uuid=None,
            process_start_ts="2026-05-29T10:00:00Z", row_count=200),
    }
    fs = FakeFS(store)
    filenames = {b.filename for b in ad.list_remote_bundles(fs, BUCKET)}
    assert filenames == {f"{complete_stem}.parquet"}


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


def test_raises_on_adsb_manifest_outside_edge_prefixes():
    # A sidecar claiming stream=adsb_state anywhere outside bronze/adsb_state/ is a poisoning vector.
    stem = "sangenjaya-edge_adsb_state_2026-05-29T00_5f3b"
    key = f"{BUCKET}/bronze/adsblol_states_raw/dt=2026-05-29/{stem}.manifest.json"
    fs = FakeFS({key: _manifest_bytes(f"{stem}.parquet", "adsb_state", row_count=1)})
    with pytest.raises(ad.StrayManifestError):
        list(ad.list_remote_bundles(fs, BUCKET))


def test_raises_on_stream_prefix_disagreement():
    stem = "sangenjaya-edge_adsb_state_2026-05-29T00_5f3b"
    fs = FakeFS({_key("beast_raw", f"{stem}.manifest.json"): _manifest_bytes(
        f"{stem}.parquet", "adsb_state", row_count=1)})
    with pytest.raises(ad.StrayManifestError):
        list(ad.list_remote_bundles(fs, BUCKET))


def test_skips_alien_stream_sidecar_outside_edge_prefixes():
    fs = FakeFS({f"{BUCKET}/bronze/other_lane/foo.manifest.json":
                 _manifest_bytes("foo.parquet", "other_lane")})
    assert list(ad.list_remote_bundles(fs, BUCKET)) == []
