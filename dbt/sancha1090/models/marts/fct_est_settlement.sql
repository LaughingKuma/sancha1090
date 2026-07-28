{{ config(materialized='table') }}
-- join_use_nulls-independent: if()+assumeNotNull merge + explicit NULLs (under jun=0 a
-- left-join miss fills the type default, not NULL); verified byte-identical under 0 and 1.
with gaps as (
    select
        estimate_id                            as estimate_id,
        seg_idx                                as seg_idx,
        producer                               as producer,
        method_version                         as method_version,
        config_hash                            as config_hash,
        input_fingerprint                      as input_fingerprint,
        uncertainty_bin                        as uncertainty_bin,
        uncertainty_p50_km                     as served_p50_km,
        uncertainty_p90_km                     as served_p90_km,
        computed_at                            as computed_at,
        input_provisional                      as input_provisional,
        icao24                                 as est_icao24,
        points                                 as pts,
        toInt64(points[1].1)                   as span_lo_ts,
        toInt64(points[length(points)].1)      as span_hi_ts,
        points[1].2                            as a_lat,
        points[1].3                            as a_lon,
        points[length(points)].2               as z_lat,
        points[length(points)].3               as z_lon
    from {{ source('bronze', 'path_estimates') }}
    where producer != 'test' and kind = 'gap' and flight_id is not null
),

-- branch (a): hex-carrying rows (rev 10.4(1), post-Task-1). fct_flight_path has no icao24;
-- fct_flights_reconciled is the mandatory intermediary. Window padded to the path mart's own clip.
hex_cand as (
    select
        g.estimate_id                    as estimate_id,
        g.seg_idx                        as seg_idx,
        groupUniqArray(r.flight_id)      as cand_fids,
        groupUniqArray(toDate(r.start_time)) as cand_days
    from gaps g
    inner join {{ ref('fct_flights_reconciled') }} r
        on lower(r.icao24) = g.est_icao24
       and r.start_time - interval {{ var('path_window_pad_min') }} minute <= toDateTime(g.span_hi_ts, 'UTC')
       and r.end_time   + interval {{ var('path_window_pad_min') }} minute >= toDateTime(g.span_lo_ts, 'UTC')
    where g.est_icao24 != ''
    group by g.estimate_id, g.seg_idx
),

-- branch (b): legacy rows — icao24='' is a VALUE test, never a date pin. Entry/exit candidate-set
-- INTERSECTION (205 unambiguous vs 176 strict-dual, 0 disagreements measured — Amit-ruled).
anchors as (
    select estimate_id as estimate_id, seg_idx as seg_idx, 'entry' as side,
           span_lo_ts as t, a_lat as lat, a_lon as lon
    from gaps where est_icao24 = ''
    union all
    select estimate_id, seg_idx, 'exit', span_hi_ts, z_lat, z_lon
    from gaps where est_icao24 = ''
),
akeys as (
    select estimate_id as estimate_id, seg_idx as seg_idx, side as side, t as t, lat as lat, lon as lon,
           toStartOfMinute(toDateTime(t, 'UTC')) + interval arrayJoin([-2, -1, 0, 1, 2]) minute as mb
    from anchors
),
anchor_scan as (
    -- day_key IN-set + minute-bucket equi-key, or CH cross-joins the whole mart (156 s+ vs
    -- 2.4 s measured); +/-1 day because fixes can precede the flight-start day_key (win pad).
    select flight_id as flight_id, ts as ts, lat as lat, lon as lon, day_key as day_key
    from {{ ref('fct_flight_path') }}
    where day_key in (
            select arrayJoin([toDate(toDateTime(t, 'UTC')) - 1,
                              toDate(toDateTime(t, 'UTC')),
                              toDate(toDateTime(t, 'UTC')) + 1]) from anchors)
      and toStartOfMinute(ts) in (select mb from akeys)
),
anchor_hits as (
    -- ±90 s / <5 km: one OpenSky-REST ambient cadence step of slack around the anchor fix,
    -- tight enough that co-located traffic must share the runway queue to collide (E6).
    select k.estimate_id as estimate_id, k.seg_idx as seg_idx, k.side as side,
           p.flight_id as cand_fid, any(p.day_key) as cand_day
    from akeys k
    inner join anchor_scan p on toStartOfMinute(p.ts) = k.mb
    where abs(toInt64(toUnixTimestamp(p.ts)) - k.t) <= 90
      and geoDistance(k.lon, k.lat, p.lon, p.lat) < 5000
    group by k.estimate_id, k.seg_idx, k.side, p.flight_id
),
anchor_cand as (
    select estimate_id as estimate_id, seg_idx as seg_idx,
           arrayIntersect(groupUniqArrayIf(cand_fid, side = 'entry'),
                          groupUniqArrayIf(cand_fid, side = 'exit')) as cand_fids,
           groupUniqArray(cand_day) as cand_days
    from anchor_hits
    group by estimate_id, seg_idx
),

rekey as (
    select
        g.estimate_id as estimate_id,
        g.seg_idx     as seg_idx,
        if(g.est_icao24 != '', 'icao24', 'anchor')                       as match_via,
        if(g.est_icao24 != '', assumeNotNull(h.cand_fids), assumeNotNull(a.cand_fids)) as cand_fids,
        if(g.est_icao24 != '', assumeNotNull(h.cand_days), assumeNotNull(a.cand_days)) as cand_days,
        length(cand_fids)                                                as n_cand,
        if(length(cand_fids) = 1, cand_fids[1], toUInt64(0))             as matched_flight_id
    from gaps g
    left join hex_cand    h on h.estimate_id = g.estimate_id and h.seg_idx = g.seg_idx
    left join anchor_cand a on a.estimate_id = g.estimate_id and a.seg_idx = g.seg_idx
),

truth_scan as (
    -- tuple-IN = both prunes (PK prefix + day_key partitions; 6.8x at 100x scale). rekey is
    -- evaluated 3x model-wide (CH re-evaluates CTEs) — 2.1-2.8s warm, warm-median>5s stop rule.
    select flight_id as flight_id, ts as ts, lat as lat, lon as lon
    from {{ ref('fct_flight_path') }}
    where (flight_id, day_key) in (
            select matched_flight_id,
                   arrayJoin(arrayConcat(arrayMap(d -> d - 1, cand_days), cand_days,
                                         arrayMap(d -> d + 1, cand_days)))
            from rekey where matched_flight_id != 0)
),

point_scores as (
    select
        g.estimate_id as estimate_id,
        g.seg_idx     as seg_idx,
        arrayFirstIndex(x -> toInt64(x.1) > toInt64(toUnixTimestamp(p.ts)), g.pts) as hi_raw,
        if(hi_raw = 0, length(g.pts), hi_raw)                                      as hi_i,
        greatest(hi_i - 1, 1)                                                      as lo_i,
        toInt64(g.pts[lo_i].1)                                                     as t0,
        toInt64(g.pts[hi_i].1)                                                     as t1,
        if(t1 = t0, 0., (toInt64(toUnixTimestamp(p.ts)) - t0) / (t1 - t0))         as w,
        g.pts[hi_i].3 - g.pts[lo_i].3                                              as dlon_raw,
        abs(dlon_raw) > 180                                                        as is_wrap,
        -- antimeridian: UNWRAP and score, never exclude (14/19 long segments hold a wrap pair;
        -- naive interpolation puts the point ~6,469 km off and would own err_max/p90).
        dlon_raw - 360 * round(dlon_raw / 360)                                     as dlon_unwrapped,
        g.pts[lo_i].2 + w * (g.pts[hi_i].2 - g.pts[lo_i].2)                        as est_lat,
        g.pts[lo_i].3 + w * dlon_unwrapped                                         as lon_open,
        lon_open - 360 * round(lon_open / 360)                                     as est_lon,
        geoDistance(est_lon, est_lat, p.lon, p.lat) / 1000.                         as err_km
    from gaps g
    inner join rekey rk
        on rk.estimate_id = g.estimate_id and rk.seg_idx = g.seg_idx
    inner join truth_scan p
        on p.flight_id = rk.matched_flight_id
    where rk.n_cand = 1
      and toInt64(toUnixTimestamp(p.ts)) > g.span_lo_ts + 60
      and toInt64(toUnixTimestamp(p.ts)) < g.span_hi_ts - 60
),

seg_scores as (
    select
        estimate_id                                             as estimate_id,
        seg_idx                                                  as seg_idx,
        count()                                                  as truth_pts,
        countIf(is_wrap)                                         as wrap_pts,
        quantileExact(0.5)(err_km)                               as sc_p50,
        quantileExact(0.9)(err_km)                               as sc_p90,
        max(err_km)                                              as sc_max,
        arraySort(groupArray(toFloat32(err_km)))                 as errs_km
    from point_scores
    group by estimate_id, seg_idx
)

select
    g.estimate_id                              as estimate_id,
    g.seg_idx                                  as seg_idx,
    g.producer                                 as producer,
    g.method_version                           as method_version,
    g.config_hash                              as config_hash,
    g.input_fingerprint                        as input_fingerprint,
    g.uncertainty_bin                          as uncertainty_bin,
    g.served_p50_km                            as served_p50_km,
    g.served_p90_km                            as served_p90_km,
    g.computed_at                              as computed_at,
    g.input_provisional                        as input_provisional,
    rk.match_via                               as match_via,
    rk.matched_flight_id                       as matched_flight_id,
    rk.n_cand                                  as n_cand,
    coalesce(s.truth_pts, 0)                   as truth_pts,
    coalesce(s.wrap_pts, 0)                    as wrap_pts,
    -- >= 3 interior fixes: the minimum that makes a per-segment p50/p90 meaningful
    toUInt8(truth_pts >= 3)                    as settled,
    toUInt8(rk.n_cand > 1)                     as skip_ambiguous,
    -- unsettled rows carry explicit NULLs: under join_use_nulls=0 a bare left-join miss would
    -- read 0.0 — an unscored segment masquerading as a perfect estimate.
    if(settled = 1, s.sc_p50, NULL)            as err_p50_km,
    if(settled = 1, s.sc_p90, NULL)            as err_p90_km,
    if(settled = 1, s.sc_max, NULL)            as err_max_km,
    coalesce(s.errs_km, [])                    as errs_km
from gaps g
inner join rekey rk on rk.estimate_id = g.estimate_id and rk.seg_idx = g.seg_idx
left join seg_scores s on s.estimate_id = g.estimate_id and s.seg_idx = g.seg_idx
