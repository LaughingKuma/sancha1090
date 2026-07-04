from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor

import pyarrow.parquet as pq
import sqlalchemy as sa

from include.db import analytics_engine
from include.s3_helpers import garage_pyarrow_fs


def _key(uri: str) -> str:
    # pyarrow fs paths are bucket/key, not URIs.
    return uri[len("s3://"):] if uri.startswith("s3://") else uri


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Reconcile ingestion_manifest.row_count against actual Garage parquet footers. "
        "Same-key rewrites (task retries, flights re-fetch) update the object but ON CONFLICT DO "
        "NOTHING froze the first count — the NAS archiver's rowcount guard then fails loud on them."
    )
    ap.add_argument("--apply", action="store_true", help="write UPDATEs (default: dry-run report)")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    eng = analytics_engine()
    with eng.begin() as conn:
        rows = conn.execute(sa.text(
            "SELECT object_uri, row_count FROM public.ingestion_manifest "
            "WHERE archived_at IS NULL ORDER BY object_uri"
        )).fetchall()
    print(f"scan: {len(rows)} unarchived manifest rows")

    fs = garage_pyarrow_fs()

    def check(row):
        uri, recorded = row
        try:
            with fs.open_input_file(_key(uri)) as f:
                actual = pq.ParquetFile(f).metadata.num_rows
        except Exception as exc:  # missing/torn object: the loader's per-file-skip lane, not ours
            return ("unreadable", uri, recorded, None, repr(exc))
        if recorded is None:
            return ("fill", uri, recorded, actual, None)
        return ("drift" if actual != recorded else "ok", uri, recorded, actual, None)

    ok, drift, fill, unreadable = 0, [], [], []
    with ThreadPoolExecutor(args.workers) as ex:
        for i, (state, uri, recorded, actual, err) in enumerate(ex.map(check, rows), 1):
            if state == "ok":
                ok += 1
            elif state == "drift":
                drift.append((uri, recorded, actual))
            elif state == "fill":
                fill.append((uri, actual))
            else:
                unreadable.append((uri, err))
            if i % 2000 == 0:
                print(f"  ...{i}/{len(rows)}")

    print(f"ok={ok} drifted={len(drift)} null-count={len(fill)} unreadable={len(unreadable)}")
    for uri, rec, act in drift:
        print(f"  DRIFT {uri}: manifest={rec} actual={act}")
    for uri, err in unreadable[:10]:
        print(f"  UNREADABLE {uri}: {err}")

    updates = [(uri, rec, act) for uri, rec, act in drift] + [(uri, None, act) for uri, act in fill]
    if not updates:
        print("nothing to update")
        return
    if not args.apply:
        print(f"dry-run: would UPDATE {len(updates)} rows ({len(drift)} drifted + {len(fill)} null-fill) — rerun with --apply")
        return
    # Footer counts are the correct oracle: the parity gate (CH vs actual objects) is green, so CH
    # content matches today's objects; snapshot bounds stay untouched (nothing verifies them).
    # Optimistic guard: a row re-recorded (record_load upsert) or archived since the scan is stale
    # here — skip it rather than clobber the newer state.
    updated = 0
    with eng.begin() as conn:
        for uri, old, act in updates:
            updated += conn.execute(
                sa.text(
                    "UPDATE public.ingestion_manifest SET row_count = :n "
                    "WHERE object_uri = :u AND archived_at IS NULL "
                    "AND row_count IS NOT DISTINCT FROM :old"
                ),
                {"n": act, "u": uri, "old": old},
            ).rowcount
    skipped = len(updates) - updated
    print(f"updated {updated} rows ({len(drift)} drifted + {len(fill)} null-fill scanned; {skipped} skipped as changed-since-scan)")


if __name__ == "__main__":
    main()
