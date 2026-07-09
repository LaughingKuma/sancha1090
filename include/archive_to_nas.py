from __future__ import annotations

import hashlib
import io
import logging
import os
from itertools import zip_longest
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# bronze/states (frozen pre-states_raw history) is distinct from states_raw — the escaped LIKE stops them cross-matching.
_OPENSKY_PREFIXES = ("bronze/states", "bronze/states_raw", "bronze/flights_raw", "bronze/adsblol_states_raw",
                     "bronze/swim_raw")


def _read_key(uri: str) -> str:
    # pyarrow fs paths are bucket/key, not URIs — strip the s3:// scheme.
    if not uri.startswith("s3://"):
        raise ValueError(f"archive: unexpected non-s3 uri: {uri}")
    return uri[len("s3://"):]


def _rel_key(uri: str) -> str:
    # Cold dest mirrors the bronze key, bucket-stripped: .../<bucket>/bronze/<lane>/…/f.parquet -> bronze/…/f.parquet
    key = _read_key(uri)
    i = key.find("bronze/")
    if i < 0:
        raise ValueError(f"archive: uri has no bronze/ segment: {uri}")
    rel = key[i:]
    # Reject traversal/empty segments so a malformed manifest URI can never resolve outside the cold root.
    if any(seg in ("", ".", "..") for seg in rel.split("/")):
        raise ValueError(f"archive: unsafe path segment in uri: {uri}")
    return rel


def _probe(data: bytes) -> tuple[int, int, str]:
    # read_metadata raises on a non-parquet/truncated blob, so the rowcount probe doubles as torn-copy detection.
    import pyarrow.parquet as pq

    nrows = pq.read_metadata(io.BytesIO(data)).num_rows
    return len(data), nrows, hashlib.md5(data, usedforsecurity=False).hexdigest()


def _copy_verify_one(fs, cold_root: Path, uri: str, expected_rows: Optional[int] = None) -> int:
    # Idempotent: a dest already matching the source is a no-op (covers a crash between copy and the archived_at
    # flip). Raises on any verify mismatch so the DAG reds rather than marking a bad copy.
    with fs.open_input_file(_read_key(uri)) as fh:
        src_bytes = fh.read()
    src = _probe(src_bytes)
    # The manifest row_count is the persisted oracle for what CH loaded; a drift means the Garage object changed
    # since ingest, so refuse it before any write.
    if expected_rows is not None and src[1] != expected_rows:
        raise RuntimeError(f"archive: source rowcount {src[1]} != manifest row_count {expected_rows} for {uri}")
    dest = cold_root / _rel_key(uri)

    if dest.exists() and _probe(dest.read_bytes()) == src:
        return src[1]  # already copied + verified

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Write to a sidecar then atomic-rename so a torn write never leaves a complete-looking partial dest.
    tmp = dest.with_name(dest.name + ".partial")
    tmp.write_bytes(src_bytes)
    os.replace(tmp, dest)

    if _probe(dest.read_bytes()) != src:
        raise RuntimeError(f"archive verify mismatch for {uri}")
    return src[1]


def archive_pending(
    *,
    fs=None,
    cold_path: Optional[os.PathLike | str] = None,
    older_than_days: Optional[int] = None,
    limit: Optional[int] = None,
    engine=None,
    adsb_engine=None,
) -> dict:
    # Copies only — never deletes the Garage source (it stays as CH's s3() source); flips archived_at per-file
    # only after a verified copy.
    from include import adsb_manifest as am
    from include import manifest

    older_than_days = older_than_days if older_than_days is not None else int(
        os.environ.get("ARCHIVE_OLDER_THAN_DAYS", "14"))
    limit = limit if limit is not None else int(os.environ.get("ARCHIVE_MAX_FILES", "5000"))
    cold_root = Path(cold_path or os.environ.get("ARCHIVE_COLD_PATH", "/cold"))
    adsb_engine = adsb_engine if adsb_engine is not None else engine

    # Fail fast on a misconfigured cap (e.g. ARCHIVE_MAX_FILES=0): LIMIT 0 would silently archive nothing and a
    # negative LIMIT is dialect-dependent (sqlite treats -1 as unlimited, postgres rejects it).
    if limit <= 0:
        raise ValueError(f"archive: ARCHIVE_MAX_FILES/limit must be positive, got {limit}")

    # Production guard: with ARCHIVE_REQUIRE_MOUNT set (on the scheduler, via docker-compose.local.yml), a missing
    # or regressed NFS mount must red loudly — never silently archive nothing behind a green DAG.
    require_mount = os.environ.get("ARCHIVE_REQUIRE_MOUNT", "").strip().lower() in ("1", "true", "yes")
    if require_mount and not (cold_root.is_dir() and os.path.ismount(cold_root)):
        raise RuntimeError(
            f"archive: cold mount {cold_root} is absent or not a mountpoint and ARCHIVE_REQUIRE_MOUNT is set")
    # Otherwise (non-prod hosts have no NFS mount) an absent path is a green skip, so the unpaused DAG doesn't red.
    if not cold_root.is_dir():
        log.warning("archive: cold path %s absent — skipping (no NFS mount on this host)", cold_root)
        return {"ok": True, "skipped": "cold path absent", "files": 0, "rows": 0, "more_remaining": False}

    if fs is None:
        from include.s3_helpers import garage_pyarrow_fs

        fs = garage_pyarrow_fs()

    # Fetch cap+1 per lane to flag "more remains" without loading the whole backlog (the +1 is a probe, not processed).
    # Tuple is (lane_tag, read_uri, mark_key, expected_rows) — opensky marks by object_uri, adsb by filename.
    lanes: list[list[tuple]] = []
    truncated = False
    for prefix in _OPENSKY_PREFIXES:
        rows_ = manifest.pending_archive_uris(prefix, older_than_days, engine, limit=limit + 1)
        truncated = truncated or len(rows_) > limit
        lanes.append([("opensky", r["object_uri"], r["object_uri"], r["row_count"]) for r in rows_[:limit]])
    arows = am.pending_archive_adsb_uris(older_than_days, adsb_engine, limit=limit + 1)
    truncated = truncated or len(arows) > limit
    lanes.append([("adsb", p["s3_uri"], p["filename"], p["row_count"]) for p in arows[:limit]])

    # Round-robin across ALL lanes (every opensky prefix + adsb) so the per-run cap makes progress on each lane,
    # not just the first — no lane starves behind a larger one during the initial backlog drain.
    work = [x for tup in zip_longest(*lanes) for x in tup if x is not None]
    more_remaining = truncated or len(work) > limit
    if more_remaining:
        # No silent cap: the backlog drains over successive daily runs (idempotent); say a tail remains.
        log.warning("archive: per-run cap %d reached — more candidates remain, draining over subsequent runs", limit)
    work = work[:limit]

    files = rows = 0
    all_ok = True
    for lane, uri, mark_key, expected_rows in work:
        try:
            rows += _copy_verify_one(fs, cold_root, uri, expected_rows)
        except Exception:
            log.exception("archive: copy/verify failed for %s (skipped)", uri)
            all_ok = False
            continue
        # Flip per-file right after verify so a crash mid-run keeps verified files marked (mirrors mark_ch_loaded).
        if lane == "opensky":
            manifest.mark_archived([mark_key], engine)
        else:
            am.mark_archived([mark_key], adsb_engine)
        files += 1
    return {"ok": all_ok, "files": files, "rows": rows, "more_remaining": more_remaining}


def gc_garage_copies(uris: list[str], *, fs=None, confirm: bool = False) -> dict:
    # OFF BY DEFAULT, never wired into the DAG. DANGER: the raw Parquet must stay in Garage as ClickHouse's s3()
    # source (gate + MV reseed glob bronze/**), so GC requires an explicit URI list AND confirm=True.
    if not confirm or not uris:
        return {"ok": True, "deleted": 0, "skipped": True}
    if fs is None:
        from include.s3_helpers import garage_pyarrow_fs

        fs = garage_pyarrow_fs()
    deleted = 0
    for uri in uris:
        fs.delete_file(_read_key(uri))
        deleted += 1
    return {"ok": True, "deleted": deleted}
