from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone

# Runs inside an airflow container (docker exec sancha1090-airflow-scheduler-1 ...)
# where ClickHouse resolves; scripts/ is bind-mounted there.
sys.path.insert(0, "/opt/airflow")

from include import adsblol_backfill as ab
from include.adsblol_release import KIND_SUFFIXES, find_release, open_release
from include.clickhouse import ch_client

# Empty hours in the global sample of EVERY published variant = the gap is upstream and no re-fetch can
# fill it; hours present in any variant but absent from bronze = our miss, land them from that variant.
KINDS = KIND_SUFFIXES
# A variant whose sample holds under this share of its own busiest hour counts as empty for that hour.
EMPTY_HOUR_RATIO = 0.01


def _open(day: date, kind: str) -> ab.ChainedReader | None:
    ref = find_release(day, kinds=(kind,))
    if ref is None:
        return None
    print(f"{day} {ref.repo}/{ref.tag}: {len(ref.parts)} part(s)", flush=True)
    return open_release(ref)


def _bronze_hexes(day: date) -> set[str]:
    client = ch_client()
    try:
        rows = client.query(
            "SELECT DISTINCT lower(icao24) FROM bronze.adsblol_flight_paths WHERE trace_day = %(d)s",
            parameters={"d": day.isoformat()},
        ).result_rows
    finally:
        client.close()
    return {r[0] for r in rows if r[0]}


def _histogram(day: date, reader: ab.ChainedReader, targets: set[str], sample_every: int) -> Counter:
    day_start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    day_end = day_start + 86400
    t0 = time.time()
    members = corrupt = out_of_day = 0
    tgt_hours: Counter = Counter()
    smp_hours: Counter = Counter()
    tgt_first: Counter = Counter()
    tgt_seen: set[str] = set()
    for name, data in ab.iter_trace_members(reader):
        members += 1
        if data is None:
            corrupt += 1
            continue
        hexid = ab.member_icao(name)
        is_tgt = hexid in targets
        is_smp = members % sample_every == 0
        if not (is_tgt or is_smp):
            continue
        try:
            doc = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            corrupt += 1
            continue
        base = doc.get("timestamp")
        if base is None:
            continue
        # Same [day_start, day_end) clamp as dense_rows: a trace can spill a few points past midnight.
        hours = []
        for p in doc.get("trace") or []:
            t = base + float(p[0])
            if day_start <= t < day_end:
                hours.append(int((t - day_start) // 3600))
            else:
                out_of_day += 1
        if is_tgt and hexid is not None:
            tgt_seen.add(hexid)
            tgt_hours.update(hours)
            if hours:
                tgt_first[min(hours)] += 1
        if is_smp:
            smp_hours.update(hours)
        if members % 20000 == 0:
            print(f"  {members} members, {time.time() - t0:.0f}s", flush=True)
    print(f"done: {members} members ({corrupt} corrupt, {out_of_day} points outside the day dropped), "
          f"{len(tgt_seen)}/{len(targets)} bronze hexes present, {time.time() - t0:.0f}s", flush=True)
    print(f"hour  bronze_hex_pts  sample_pts(1/{sample_every})  bronze_hex_traces_starting")
    for h in range(24):
        print(f"{h:>4}  {tgt_hours[h]:>14}  {smp_hours[h]:>16}  {tgt_first[h]:>10}")
    return smp_hours


def _empty_hours(sample: Counter) -> list[int]:
    peak = max(sample.values(), default=0)
    return [h for h in range(24) if sample[h] < peak * EMPTY_HOUR_RATIO]


def run(day: date, kinds: tuple[str, ...], sample_every: int) -> int:
    targets = _bronze_hexes(day)
    print(f"{len(targets)} bronze-landed hexes for {day}", flush=True)
    empties: dict[str, list[int]] = {}
    for kind in kinds:
        reader = _open(day, kind)
        if reader is None:
            print(f"{day} {kind}: no release published", flush=True)
            continue
        empties[kind] = _empty_hours(_histogram(day, reader, targets, sample_every))
        print(f"{kind}: empty hours {empties[kind]}", flush=True)
    if not empties:
        print(f"{day}: no release found for any of {kinds}")
        return 1
    agree = len({tuple(v) for v in empties.values()}) == 1
    print(f"variants probed: {sorted(empties)}; empty hours agree: {'yes' if agree else 'NO'}"
          + ("" if agree else " — some variant has hours the others lack; land from it before closing"))
    return 0


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1 (got {n})")
    return n


def main() -> int:
    p = argparse.ArgumentParser(description="Histogram an adsb.lol release's trace points per UTC hour")
    p.add_argument("day", help="trace day, YYYY-MM-DD")
    p.add_argument("--kind", default="all", choices=("all", *KINDS),
                   help="one release variant, or all published variants in sequence (default)")
    p.add_argument("--sample-every", type=_positive_int, default=20, help="global sample stride (default 20)")
    args = p.parse_args()
    kinds = KINDS if args.kind == "all" else (args.kind,)
    return run(date.fromisoformat(args.day), kinds, args.sample_every)


if __name__ == "__main__":
    raise SystemExit(main())
