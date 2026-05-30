from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Optional


_STREAMS = ("adsb_state", "beast_raw")
_DATA_SUFFIX = {"adsb_state": ".parquet", "beast_raw": ".beast.gz"}
_MANIFEST_SUFFIX = ".manifest.json"


class RowCountMismatch(ValueError):
    """Parquet footer row count disagrees with the producer manifest's row_count."""


@dataclass(frozen=True)
class RemoteManifestBundle:
    stream: str                       # 'adsb_state' | 'beast_raw'
    filename: str                     # data file basename, PK in adsb_ingestion_manifest
    data_s3_uri: str
    manifest_s3_uri: str
    extra_sidecar_s3_uris: list[str]  # [.beastidx.gz uri] for beast, [] for parquet
    manifest: dict


def _uri(key: str) -> str:
    return f"s3://{key}"


def list_remote_bundles(fs, bucket: str, prefix: str = "bronze") -> Iterable[RemoteManifestBundle]:
    """Yield complete bundles only: manifest present, data file present, all sidecars present,
    data file not *.inprogress. Manifest-driven (the manifest's existence is the completeness
    gate). Dedup against Postgres is the caller's job — discovery is stateless."""
    keys = set(fs.find(f"{bucket}/{prefix}"))

    for manifest_key in sorted(k for k in keys if k.endswith(_MANIFEST_SUFFIX)):
        try:
            manifest = json.loads(fs.cat(manifest_key))
        except (ValueError, KeyError):
            continue

        stream = manifest.get("stream")
        data_name = manifest.get("filename")
        if stream not in _STREAMS or not data_name:
            continue
        if data_name.endswith(".inprogress"):
            continue

        directory = manifest_key.rsplit("/", 1)[0]
        data_key = f"{directory}/{data_name}"
        if data_key not in keys:
            continue  # orphan manifest — data not landed (yet)

        # Beast carries a .beastidx.gz sidecar (same stem); Parquet has none.
        sidecar_keys: list[str] = []
        if stream == "beast_raw":
            stem = data_name[: -len(_DATA_SUFFIX["beast_raw"])]
            sidecar_keys = [f"{directory}/{stem}.beastidx.gz"]
        if any(sk not in keys for sk in sidecar_keys):
            continue  # incomplete bundle — a sidecar is missing

        yield RemoteManifestBundle(
            stream=stream,
            filename=data_name,
            data_s3_uri=_uri(data_key),
            manifest_s3_uri=_uri(manifest_key),
            extra_sidecar_s3_uris=[_uri(sk) for sk in sidecar_keys],
            manifest=manifest,
        )


def validate_bundle(bundle: RemoteManifestBundle, num_rows: Optional[int]) -> None:
    """Parquet: footer row count must match manifest['row_count']. Beast: trust the manifest
    (no cheap remote integrity check; full validation is deferred to silver decode)."""
    if bundle.stream != "adsb_state":
        return
    expected = bundle.manifest.get("row_count")
    if num_rows != expected:
        raise RowCountMismatch(
            f"{bundle.filename}: parquet rows={num_rows} manifest row_count={expected}"
        )


def read_parquet_num_rows(pa_fs, path: str) -> int:
    """Footer-only read (~10 KB GET). `path` is filesystem-native (no s3:// scheme)."""
    import pyarrow.parquet as pq

    with pa_fs.open_input_file(path) as f:
        return pq.ParquetFile(f).metadata.num_rows
