from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional


_STREAMS = ("adsb_state", "beast_raw")
_DATA_SUFFIX = {"adsb_state": ".parquet", "beast_raw": ".beast.gz"}
_MANIFEST_SUFFIX = ".manifest.json"
_STREAM_PREFIX = {"adsb_state": "bronze/adsb_state/", "beast_raw": "bronze/beast_raw/"}


class StrayManifestError(ValueError):
    """A producer manifest sits outside its stream's canonical prefix — cross-lane poisoning risk."""


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


def _parse_process_start_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    # naive would TypeError against aware peers — one malformed manifest must not kill discovery.
    if dt.tzinfo is None:
        return None
    return dt


def list_remote_bundles(fs, bucket: str, prefix: str = "bronze") -> Iterable[RemoteManifestBundle]:
    """Yield complete bundles, plus any partial whose writer process is provably dead — a
    complete bundle exists for the same stream from a different, strictly newer process_uuid.
    Manifest present, data file present, all sidecars present, data file not *.inprogress.
    Manifest-driven (the manifest's existence is the completeness gate). Dedup against Postgres
    is the caller's job — discovery is stateless."""
    keys = set(fs.find(f"{bucket}/{prefix}"))

    deferred: dict[str, list[RemoteManifestBundle]] = {}
    complete_processes: dict[str, list[tuple[object, object]]] = {}

    for manifest_key in sorted(k for k in keys if k.endswith(_MANIFEST_SUFFIX)):
        try:
            manifest = json.loads(fs.cat(manifest_key))
        except (ValueError, KeyError):
            continue

        stream = manifest.get("stream")
        data_name = manifest.get("filename")

        # Boundary guard: an edge-stream manifest outside its prefix (or an alien manifest inside
        # one) must fail loud — the glob loader would otherwise ingest whatever it points at.
        rel_key = manifest_key[len(f"{bucket}/"):]
        owner = next((s for s, p in _STREAM_PREFIX.items() if rel_key.startswith(p)), None)
        if (stream in _STREAMS or owner is not None) and owner != stream:
            raise StrayManifestError(
                f"{manifest_key}: stream={stream!r} does not belong under this prefix")

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

        bundle = RemoteManifestBundle(
            stream=stream,
            filename=data_name,
            data_s3_uri=_uri(data_key),
            manifest_s3_uri=_uri(manifest_key),
            extra_sidecar_s3_uris=[_uri(sk) for sk in sidecar_keys],
            manifest=manifest,
        )

        if manifest.get("complete") is not True:
            deferred.setdefault(stream, []).append(bundle)
            continue

        complete_processes.setdefault(stream, []).append(
            (manifest.get("process_uuid"), manifest.get("process_start_ts")))
        yield bundle

    # A dead process's partial never flips complete; a complete bundle from a provably newer
    # process (the edge runs exactly one at a time) proves it final — identity ordering, not
    # hour arithmetic, since the successor's first complete bundle can land hours later.
    for stream, partials in deferred.items():
        for bundle in partials:
            partial_uuid = bundle.manifest.get("process_uuid")
            partial_start = _parse_process_start_ts(bundle.manifest.get("process_start_ts"))
            if not partial_uuid or partial_start is None:
                continue  # no identity to prove the writer dead — never supersede without it
            for uuid, start_ts in complete_processes.get(stream, []):
                if not uuid or uuid == partial_uuid:
                    continue  # an identity-less successor is not proof a different process exists
                start = _parse_process_start_ts(start_ts)
                if start is not None and start > partial_start:
                    yield bundle
                    break


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
