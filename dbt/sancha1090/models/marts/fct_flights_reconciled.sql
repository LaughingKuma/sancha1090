{{ config(
    materialized='table',
    tags=['reconcile'],
    query_settings={'max_memory_usage': 12000000000},
) }}

-- Deploy-order guard: dim.dim_ladd arrives via clickhouse-init but this model rebuilds on the 10-min cron, so
-- gate the LADD join on the table existing (sources.yml dim_ladd owns the ref edge; get_relation is execute-only).
{%- set ladd_rel = optional_relation('dim', 'dim_ladd') %}

-- Cross-source consensus flight mart: per flight, plurality per endpoint, authority + scheduled-service
-- tiebreak, single-source flagged, curated override on top; full provenance. Additive -- pure lanes untouched.
-- The ballot computation itself lives in int_flight_ballot (see its header for why): a materialized
-- table can be joined more than once for free, where a CTE re-runs its whole query per reference.
with ballot as (
    select * from {{ ref('int_flight_ballot') }}
),
-- Finding 3: origin/dest vote independently, so their pair can be unasserted by any source -- pruned to
-- the ~0.2% of flights this applies to, before the (otherwise whole-mart-scanning) admissibility joins.
unresolved as (
    select flight_id from ballot
    where origin_icao is not null and dest_icao is not null and not pair_asserted
),
complete_pairs as (
    select a.flight_id, a.origin_icao, a.dest_icao, a.source_rank
    from {{ ref('int_flight_attach') }} a
    where a.origin_icao is not null and a.dest_icao is not null
      and a.flight_id in (select flight_id from unresolved)
),
admissible_pairs as (
    -- a rescue candidate must be a top-vote airport on BOTH sides of its own independent ballot,
    -- exactly like the ballot's own winners are -- rank then picks among these equally-plausible pairs.
    select cp.flight_id as flight_id, cp.origin_icao as origin_icao, cp.dest_icao as dest_icao,
        cp.source_rank as source_rank,
        row_number() over (partition by cp.flight_id order by cp.source_rank asc, cp.origin_icao asc, cp.dest_icao asc) as rn
    from complete_pairs cp
    join ballot b on b.flight_id = cp.flight_id
    where b.origin_votes[cp.origin_icao] = arrayMax(mapValues(b.origin_votes))
      and b.dest_votes[cp.dest_icao] = arrayMax(mapValues(b.dest_votes))
),
rescue_pair as (
    select flight_id, origin_icao as rescue_origin, dest_icao as rescue_dest, source_rank as rescue_rank
    from admissible_pairs where rn = 1
),
pair_coherence as (
    -- Coherence applies only when both sides have a vote; fallback keeps the stronger vote count (rank
    -- ties only). 'coherence' marks only a value actually changed -- an unchanged rescue/fallback side keeps its label.
    select
        b.flight_id as flight_id,
        multiIf(b.dest_icao is null, b.origin_icao,
                b.origin_icao is null, null,
                b.pair_asserted, b.origin_icao,
                rp.flight_id is not null, rp.rescue_origin,
                b.origin_votes_n > b.dest_votes_n or (b.origin_votes_n = b.dest_votes_n and b.origin_best_rank <= b.dest_best_rank), b.origin_icao,
                null) as origin_icao,
        multiIf(b.dest_icao is null, b.origin_src,
                b.origin_icao is null, null,
                b.pair_asserted, b.origin_src,
                rp.flight_id is not null and rp.rescue_origin = b.origin_icao, b.origin_src,
                rp.flight_id is not null, {{ rank_source_label('rp.rescue_rank') }},
                b.origin_votes_n > b.dest_votes_n or (b.origin_votes_n = b.dest_votes_n and b.origin_best_rank <= b.dest_best_rank), b.origin_src,
                null) as origin_src,
        multiIf(b.dest_icao is null, b.origin_agr,
                b.origin_icao is null, null,
                b.pair_asserted, b.origin_agr,
                rp.flight_id is not null and rp.rescue_origin = b.origin_icao, b.origin_agr,
                rp.flight_id is not null, 'coherence',
                b.origin_votes_n > b.dest_votes_n or (b.origin_votes_n = b.dest_votes_n and b.origin_best_rank <= b.dest_best_rank), b.origin_agr,
                'coherence') as origin_agr,
        multiIf(b.origin_icao is null, b.dest_icao,
                b.dest_icao is null, null,
                b.pair_asserted, b.dest_icao,
                rp.flight_id is not null, rp.rescue_dest,
                b.dest_votes_n > b.origin_votes_n or (b.dest_votes_n = b.origin_votes_n and b.dest_best_rank < b.origin_best_rank), b.dest_icao,
                null) as dest_icao,
        multiIf(b.origin_icao is null, b.dest_src,
                b.dest_icao is null, null,
                b.pair_asserted, b.dest_src,
                rp.flight_id is not null and rp.rescue_dest = b.dest_icao, b.dest_src,
                rp.flight_id is not null, {{ rank_source_label('rp.rescue_rank') }},
                b.dest_votes_n > b.origin_votes_n or (b.dest_votes_n = b.origin_votes_n and b.dest_best_rank < b.origin_best_rank), b.dest_src,
                null) as dest_src,
        multiIf(b.origin_icao is null, b.dest_agr,
                b.dest_icao is null, null,
                b.pair_asserted, b.dest_agr,
                rp.flight_id is not null and rp.rescue_dest = b.dest_icao, b.dest_agr,
                rp.flight_id is not null, 'coherence',
                b.dest_votes_n > b.origin_votes_n or (b.dest_votes_n = b.origin_votes_n and b.dest_best_rank < b.origin_best_rank), b.dest_agr,
                'coherence') as dest_agr
    from ballot b
    left join rescue_pair rp on rp.flight_id = b.flight_id
),
n_src as (
    select flight_id, uniqExact(source) as n_sources from {{ ref('int_flight_attach') }} group by flight_id
),
gate as (
    select flight_id, max(origin_gated) as origin_gated, max(dest_gated) as dest_gated
    from {{ ref('int_flight_attach') }} group by flight_id
),
box_spine as (
    -- Day-keyed like int_flight_attached_votes: icao24 alone paired every spine row with every same-hex fix ever (#191).
    -- opensky_flights anchors bypass this gate in `resolved`, so hashing them here only widens the probe side.
    select flight_id, icao24, anchor_source, flight_start, flight_end,
           {{ overlap_days('flight_start', 'flight_end') }} as overlap_day
    from {{ ref('int_flight_spine') }}
    where flight_start is not null and flight_end is not null and anchor_source != 'opensky_flights'
),
{#- OpenSky's poll misses flights adsb.lol or the rooftop saw in the box (#213 D); only adsblol anchors can
    fall to this gate (opensky_states anchors are in-box by construction), so the extra lanes probe those alone. #}
{%- set box_lanes = [
    {'src': 'opensky_states', 'hex_expr': 'icao24', 'ts_expr': 'snapshot_time',
     'lat': 's.latitude', 'lon': 's.longitude', 'anchor_filter': none},
    {'src': 'adsblol_states', 'hex_expr': 'icao24', 'ts_expr': 'snapshot_time',
     'lat': 's.latitude', 'lon': 's.longitude', 'anchor_filter': "anchor_source = 'adsblol'"},
    {'src': 'adsb_states', 'hex_expr': 'lower(hex)', 'ts_expr': "toDateTime64(capture_ts, 6, 'UTC')",
     'lat': 's.lat', 'lon': 's.lon', 'anchor_filter': "anchor_source = 'adsblol'"},
] %}
box_observed as (
    -- The Japan box saw this flight (in-box bronze fix in-window; japan_box_* vars, EXISTS-semantics so dups fine).
    -- The spine is the hash side: ~1M rows against tens of millions per bronze lane, which stream past it.
{%- for lane in box_lanes %}
    {%- if not loop.first %}
    union distinct
    {%- endif %}
    select distinct sp.flight_id as flight_id
    from (
        select {{ lane.hex_expr }} as icao24, {{ lane.ts_expr }} as snapshot_time,
               toUInt32(toRelativeDayNum({{ lane.ts_expr }})) as overlap_day
        from {{ source('bronze', lane.src) }} s
        where {{ in_japan_box(lane.lat, lane.lon) }}
    ) s
    join (select * from box_spine{% if lane.anchor_filter %} where {{ lane.anchor_filter }}{% endif %}) sp
      on sp.icao24 = s.icao24 and sp.overlap_day = s.overlap_day
    where s.snapshot_time between sp.flight_start and sp.flight_end
{%- endfor %}
),
curated as (
    -- Windowless human override; latest valid_from wins if windows overlap.
    -- An endpoint only counts as resolved once dim_airports confirms the ICAO exists.
    select flight_id, origin_icao, dest_icao from (
        select sp.flight_id as flight_id,
               oa.icao as origin_icao, da.icao as dest_icao,
               row_number() over (partition by sp.flight_id order by ov.valid_from desc) as rn
        from {{ ref('int_flight_spine') }} sp
        join {{ ref('dim_route_overrides') }} ov
          on ov.callsign = trimBoth(sp.anchor_callsign)
         and toDate(sp.flight_start) between ov.valid_from and ov.valid_to
        left join {{ ref('dim_airports') }} oa on oa.icao = nullIf(ov.origin_icao, '')
        left join {{ ref('dim_airports') }} da on da.icao = nullIf(ov.dest_icao, '')
    ) where rn = 1
),
trace_end as (
    -- The label follows the vote: the chain int_flight_attached_votes attached here, not any overlapping chain (over-cap
    -- chains cannot vote, a chain spanning two flights attaches to one). te_flight_id: a flight_id here makes `r.*` emit `r.flight_id`.
    select av.flight_id as te_flight_id,
           c.first_alt_m as first_alt_m, c.first_on_ground as first_on_ground,
           c.last_alt_m as last_alt_m, c.last_on_ground as last_on_ground
    from {{ ref('int_flight_attached_votes') }} av
    join {{ ref('int_flight_chains_adsblol') }} c on c.icao24 = av.icao24 and c.chain_start = av.win_start
    where av.source = 'adsblol'
),
resolved as (
    select
        sp.flight_id as flight_id,
        sp.icao24 as icao24,
        trimBoth(sp.anchor_callsign) as callsign,
        sp.flight_start as start_time,
        sp.flight_end as end_time,
        sp.anchor_source as anchor_source,
        coalesce(nc.n_sources, 0) as n_sources,
        -- SP4 left a mark here: at least one source's endpoint was discarded as infeasible, so the endpoint
        -- that survived is a consensus over a reduced ballot. Opinion seam only (see int_flight_attach).
        toUInt8(coalesce(g.origin_gated, 0) = 1 or coalesce(g.dest_gated, 0) = 1) as feasibility_gated,
        -- per endpoint: curated override > coherence-checked consensus winner
        coalesce(cur.origin_icao, pc.origin_icao) as origin_icao,
        -- gated on origin_agr, not origin_icao: a gate-nulled endpoint has icao=NULL but agr='coherence',
        -- and that provenance must survive even though the served value itself is null.
        multiIf(cur.origin_icao is not null, 'curated', pc.origin_agr is not null, pc.origin_src, null) as origin_source,
        multiIf(cur.origin_icao is not null, 'curated', pc.origin_agr is not null, pc.origin_agr, null) as origin_agreement,
        b.origin_votes as origin_votes,
        coalesce(cur.dest_icao, pc.dest_icao) as dest_icao,
        multiIf(cur.dest_icao is not null, 'curated', pc.dest_agr is not null, pc.dest_src, null) as dest_source,
        multiIf(cur.dest_icao is not null, 'curated', pc.dest_agr is not null, pc.dest_agr, null) as dest_agreement,
        b.dest_votes as dest_votes,
        ac.registration, ac.typecode,
        al.name as airline_name, al.country as airline_country,
        {{ ch_hex_country('sp.icao24') }} as reg_country
    from {{ ref('int_flight_spine') }} sp
    left join ballot b on b.flight_id = sp.flight_id
    left join pair_coherence pc on pc.flight_id = sp.flight_id
    left join n_src nc on nc.flight_id = sp.flight_id
    left join gate g on g.flight_id = sp.flight_id
    left join curated cur on cur.flight_id = sp.flight_id
    left join {{ ref('dim_aircraft') }} ac on ac.icao24 = lower(sp.icao24)
    left join {{ ref('dim_airlines') }} al
           on al.icao = substring(trimBoth(sp.anchor_callsign), 1, 3)
          and match(trimBoth(sp.anchor_callsign), '^[A-Z]{3}[0-9]')
    -- else adsb.lol's worldwide chains inflate this Japan mart ~3x.
    where sp.anchor_source = 'opensky_flights' or sp.flight_id in (select flight_id from box_observed)
)
{%- if ladd_rel is not none %},
ladd_src as (
    -- dim_ladd is RMT(_version) → FINAL for current SCD2 state. icao24 is stored lowercase and callsign trim+UPPER;
    -- re-normalized here so the mart↔dim comparison can't drift from the ingest normalizer.
    select lower(trimBoth(icao24)) as icao24, upper(trimBoth(callsign)) as callsign, valid_from, valid_to
    from {{ source('dim', 'dim_ladd') }} final
),
ladd_match as (
    -- D5: identity (hex OR normalized callsign) AND (open interval → all its history, OR a closed interval that
    -- overlaps [start_time, end_time]). Hex and callsign are two equi-joins so CH keeps a hash join (no OR key).
    select r.flight_id
    from resolved r
    join ladd_src l on l.icao24 = lower(r.icao24)
    where l.valid_to is null or (l.valid_from <= toDate(r.end_time) and l.valid_to >= toDate(r.start_time))
    union distinct
    select r.flight_id
    from resolved r
    join ladd_src l on l.callsign = upper(trimBoth(r.callsign))
    where l.valid_to is null or (l.valid_from <= toDate(r.end_time) and l.valid_to >= toDate(r.start_time))
)
{%- endif %}
select
    r.*,
    oap.name as origin_name, nullIf(oap.iata, '') as origin_iata, nullIf(oap.city, '') as origin_city,
    oap.lat as origin_lat, oap.lon as origin_lon,
    dap.name as dest_name, nullIf(dap.iata, '') as dest_iata, nullIf(dap.city, '') as dest_city,
    dap.lat as dest_lat, dap.lon as dest_lon,
    -- 'coverage': the adsb.lol chain left this flight airborne at cruise on its unresolved side -- an unknown foreign
    -- end, not a domestic flight. Another lane's vote there may have been gate-nulled (agreement = 'coherence').
    multiIf((r.origin_icao is null) = (r.dest_icao is null), null,
            r.origin_icao is null and not te.first_on_ground and te.first_alt_m >= {{ var('legs_cruise_alt_m') }}, 'coverage',
            r.dest_icao is null and not te.last_on_ground and te.last_alt_m >= {{ var('legs_cruise_alt_m') }}, 'coverage',
            null) as foreign_unresolved_reason,
{%- if ladd_rel is not none %}
    -- window-aware suppression flag; warehouse keeps the row (flag only), livemap drops it at serve time.
    toUInt8(r.flight_id in (select flight_id from ladd_match)) as is_ladd
{%- else %}
    -- guarded literal until dim.dim_ladd exists (see deploy-order guard above); self-heals on first run after.
    toUInt8(0) as is_ladd
{%- endif %}
from resolved r
left join {{ ref('dim_airports') }} oap on oap.icao = r.origin_icao
left join {{ ref('dim_airports') }} dap on dap.icao = r.dest_icao
left join trace_end te on te.te_flight_id = r.flight_id
