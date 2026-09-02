from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

import polars as pl

from include import adsblol_backfill as ab
from include import adsblol_route_ledger as ledger
from include import adsblol_routes as routes
from include import manifest
from include.s3_helpers import write_parquet

log = logging.getLogger(__name__)

USER_AGENT = "sancha1090-backfill"
# Politeness pacing for GitHub HEAD probes and part opens.
HEAD_SPACING_S = 0.25
MIN_TRACES = 10_000
CORRUPT_RATIO_CEILING = 0.01
# A 6 M-point day held as Python dicts is several GB; polars frames are ~10x smaller.
PATH_CHUNK_ROWS = 250_000

KIND_SUFFIXES = ("prod-0", "prod-0tmp", "staging-0")


class ReleaseUnavailable(RuntimeError):
    pass


class ProbeFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseRef:
    repo: str
    tag: str
    parts: tuple[str, ...]


@dataclass
class DayExtract:
    seg_rows: list[dict] = field(default_factory=list)
    path_frames: list[pl.DataFrame] = field(default_factory=list)
    members: int = 0
    extracted: int = 0
    bad: int = 0
    outcomes: dict[str, str] = field(default_factory=dict)

    def frames(self) -> tuple[pl.DataFrame, pl.DataFrame]:
        return (routes.segments_frame(self.seg_rows),
                pl.concat(self.path_frames) if self.path_frames else routes.paths_frame([]))


def head_ok(url: str) -> bool:
    # Only a definitive 404 means "part doesn't exist" — treating a transient
    # 403/429/5xx as missing would silently truncate the tar part chain into a
    # partial (and committable) day.
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30):
                return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            last_exc = exc
        # URLError is itself an OSError; a response-read TimeoutError/RemoteDisconnected escapes
        # urllib un-wrapped on CPython 3.12, and must stay inside the retry loop.
        except (urllib.error.URLError, OSError) as exc:
            last_exc = exc
        time.sleep(2**attempt)
    raise ProbeFailed(f"HEAD {url} kept failing: {last_exc}")


def find_release(day: date, kinds: tuple[str, ...] = KIND_SUFFIXES) -> Optional[ReleaseRef]:
    def probe(url: str) -> bool:
        time.sleep(HEAD_SPACING_S)
        return head_ok(url)

    for repo, tag in ab.release_candidates(day):
        if not tag.endswith(kinds):
            continue
        # Sub-2GB days ship as one unsplit .tar; larger days split into aa/ab/...
        if probe(ab.part_url(repo, tag)):
            parts = [""]
        else:
            parts = []
            for i in range(40):
                suffix = chr(ord("a") + i // 26) + chr(ord("a") + i % 26)
                if not probe(ab.part_url(repo, tag, suffix)):
                    break
                parts.append(suffix)
        if not parts:
            continue
        return ReleaseRef(repo=repo, tag=tag, parts=tuple(parts))
    return None


def open_release(ref: ReleaseRef) -> ab.ChainedReader:
    def opener(part: str):
        def _open():
            time.sleep(HEAD_SPACING_S)
            req = urllib.request.Request(
                ab.part_url(ref.repo, ref.tag, part), headers={"User-Agent": USER_AGENT}
            )
            return urllib.request.urlopen(req, timeout=120)
        return _open

    return ab.ChainedReader([opener(p) for p in ref.parts])


def quality_gate(members: int, bad: int, extracted: int, min_traces: int) -> None:
    # Tolerate isolated bad traces, but a desynced tar stream corrupts everything after the bad
    # spot — never commit a quietly-partial day. The absolute floor keeps small target sets (a
    # 4-hex resegment day) from failing on one truncated member. min_traces is a knob: some
    # upstream days are legitimately partial (e.g. 2026-05-05's 236 MB tar) and need a lower floor.
    if members < min_traces or bad > max(2, extracted * CORRUPT_RATIO_CEILING):
        raise RuntimeError(f"day failed quality gate: {members} traces, {bad} corrupt or failed")


def member_progress(day, indent: str = "") -> Callable[[int], None]:
    # Block-buffered docker-exec stdout stays silent for a whole day otherwise; the 20,000-member
    # cadence lives in extract_day.
    def cb(members: int) -> None:
        print(f"{indent}{day}: {members} members scanned", flush=True)
    return cb


def record_parquet(df: pl.DataFrame, key: str, ts_col: str, *, engine=None) -> str:
    uri = write_parquet(df, key)
    col = df.get_column(ts_col)
    manifest.record_load(uri, int(col.min()) if df.height else None,
                         int(col.max()) if df.height else None, df.height, engine=engine)
    return uri


def _result(day_iso: str, ref: Optional[ReleaseRef], ex: DayExtract,
            outcomes: list[tuple[str, str, str]], n_targets: int) -> dict:
    return {
        "day": day_iso,
        "repo": ref.repo if ref is not None else None,
        "tag": ref.tag if ref is not None else None,
        "parts": len(ref.parts) if ref is not None else 0,
        "streamed": bool(outcomes),
        "members": ex.members,
        "targets": n_targets,
        "fetched": len(outcomes),
        "extracted": ex.extracted,
        "landed": sum(1 for _, _, o in outcomes if o == "landed"),
        "missing": sum(1 for _, _, o in outcomes if o == "missing"),
        "errors": sum(1 for _, _, o in outcomes if o == "error"),
        "rows": len(ex.seg_rows),
        "path_rows": sum(f.height for f in ex.path_frames),
        "uri": None,
        # Landed hexes only: the backfill deletes their superseded bronze rows (a re-walk that
        # drops a landing's leading ground cluster gets a new seg_start the RMT won't replace).
        "landed_hexes": sorted(h for h, _, o in outcomes if o == "landed"),
    }


def extract_day(day: date, reader: Any, targets: set[str], *,
                min_traces: int = MIN_TRACES, progress=None) -> DayExtract:
    ex = DayExtract()
    path_rows: list[dict[str, Any]] = []

    def keep(name: str) -> bool:
        ex.members += 1
        if progress is not None and ex.members % 20_000 == 0:
            progress(ex.members)
        return ab.member_icao(name) in targets

    for name, data in ab.iter_trace_members(reader, keep=keep):
        ex.extracted += 1
        hexid = ab.member_icao(name)
        if data is None:
            ex.bad += 1
            ex.outcomes[hexid] = "error"
            continue
        try:
            doc = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            ex.bad += 1
            ex.outcomes[hexid] = "error"
            continue
        try:
            segs = routes.trace_segments(doc, day)
            paths = routes.trace_paths(doc, day, segs)
        except Exception:
            log.warning("trace segmentation failed for (%s, %s)", hexid, day, exc_info=True)
            # A malformed-but-parseable doc must not abort the day's whole stream, but it still
            # counts toward the gate — a systematic segmenter break must red the run.
            ex.bad += 1
            ex.outcomes[hexid] = "error"
            continue
        ex.outcomes[hexid] = "landed"
        ex.seg_rows.extend(segs)
        path_rows.extend(paths)
        if len(path_rows) >= PATH_CHUNK_ROWS:
            ex.path_frames.append(routes.paths_frame(path_rows))
            path_rows = []

    if path_rows:
        ex.path_frames.append(routes.paths_frame(path_rows))
    quality_gate(ex.members, ex.bad, ex.extracted, min_traces)
    return ex


def land_release_day(day: date, targets, *, engine=None, ref: Optional[ReleaseRef] = None,
                     reader: Any = None, min_traces: int = MIN_TRACES,
                     dry_run: bool = False, progress=None) -> dict:
    day_iso = day.isoformat()
    target_set = sorted(set(targets))
    pairs = ledger.filter_unattempted([(h, day_iso) for h in target_set], engine)
    wanted = {h for h, _ in pairs}
    if not wanted:
        return _result(day_iso, None, DayExtract(), [], len(target_set))

    if reader is None:
        ref = ref or find_release(day)
        if ref is None:
            raise ReleaseUnavailable(f"no adsb.lol release for {day}")
        reader = open_release(ref)
    ex = extract_day(day, reader, wanted, min_traces=min_traces, progress=progress)

    df, pdf = ex.frames()
    outcomes = [(h, day_iso, ex.outcomes.get(h, "missing")) for h in sorted(wanted)]
    result = _result(day_iso, ref, ex, outcomes, len(target_set))
    if dry_run:
        return result

    # One stamp per run: a same-day rerun lands additively (record_load keeps ch_loaded_at on a
    # same-key rewrite, so overwriting part-000 never re-drained); the RMT dedups overlaps.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    if df.height:
        key = f"bronze/adsblol_flight_segments/dt={day_iso}/part-{stamp}.parquet"
        result["uri"] = record_parquet(df, key, "seg_start", engine=engine)
    if pdf.height:
        pkey = f"bronze/adsblol_flight_paths/dt={day_iso}/part-{stamp}.parquet"
        record_parquet(pdf, pkey, "ts", engine=engine)
    ledger.record_attempts(outcomes, engine)
    return result


def land_day_if_due(day: date, *, engine=None, force: bool = False,
                    kinds: tuple[str, ...] = KIND_SUFFIXES, min_traces: int = MIN_TRACES) -> dict:
    day_iso = day.isoformat()
    landed = ledger.release_landing(day_iso, engine)
    if landed is not None and not force:
        return {"day": day_iso, "status": "already_landed", **landed}

    targets = routes.release_targets(day)
    if not targets:
        # No marker: a later sweep tick re-checks, which covers a bronze replay landing late.
        log.warning("no adsb.lol extraction targets for %s", day_iso)
        return {"day": day_iso, "status": "no_targets"}

    ref = find_release(day, kinds)
    if ref is None:
        return {"day": day_iso, "status": "unpublished"}

    if force:
        # Only once the release is known (a marker cleared ahead of an unpublished probe would come back
        # as a zero-count landing); filter_unattempted skips landed pairs, so force clears them here.
        ledger.delete_attempts([(h, day_iso) for h in targets], engine)
    res = land_release_day(day, targets, engine=engine, ref=ref, min_traces=min_traces)
    ledger.record_release_landing(
        day_iso, repo=ref.repo, tag=ref.tag, parts=len(ref.parts), members=res["members"],
        targets=res["targets"], landed=res["landed"], missing=res["missing"],
        errors=res["errors"], engine=engine,
    )
    return {"status": "landed", **res}


def sweep_days(newest: date, n: int = 4) -> list[date]:
    return [newest - timedelta(days=k) for k in range(n - 1, -1, -1)]
