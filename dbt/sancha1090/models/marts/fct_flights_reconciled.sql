{{ config(materialized='table', tags=['reconcile']) }}

-- Deploy-order guard: dim.dim_ladd is created by clickhouse-init at the operator's deploy, but transform_marts
-- rebuilds this model from committed code every ~4 min — so gate the LADD join on the table actually existing.
-- schema/identifier are pinned to the dim_ladd entry in sources.yml (the ladd_src CTE's source() owns the ref
-- edge); get_relation is execute-only so parse never touches the warehouse (ladd_rel defaults to none there).
{%- if execute %}
{%- set ladd_rel = adapter.get_relation(database=none, schema='dim', identifier='dim_ladd') %}
{%- else %}
{%- set ladd_rel = none %}
{%- endif %}

-- Cross-source consensus flight mart: per flight, plurality per endpoint, authority + scheduled-service
-- tiebreak, single-source flagged, curated override on top; full provenance. Additive -- pure lanes untouched.
with flight_shape as (
    -- only airliners get the sched tiebreak below; a real military flight may legitimately land at RJCJ.
    -- SP4: is_jet feeds the ballot gate -- an opinion's own callsign can be NULL (single-ping legs), so
    -- infeasibility is also enforced per-flight on the anchor identity, post-attach.
    select sp.flight_id as flight_id,
           {{ airline_shaped('sp.anchor_callsign') }} as is_airline,
           (j.icao24 is not null) as is_jet
    from {{ ref('int_flight_spine') }} sp
    left join {{ ref('int_jet_airframes') }} j on j.icao24 = lower(sp.icao24)
),
origin_ballot as (
    select a.flight_id as flight_id, a.origin_icao as airport, count() as votes,
           min(a.source_rank) as best_rank, max(coalesce(ap.scheduled_service, false)) as sched
    from {{ ref('int_flight_attach') }} a
    left join {{ ref('dim_airports') }} ap on ap.icao = a.origin_icao
    left join flight_shape fs on fs.flight_id = a.flight_id
    where a.origin_icao is not null
      -- SP4 ballot gate: anchor-identity backstop -- a NULL-callsign opinion must not launder an
      -- infeasible field onto an airline-jet flight (opinion-level gate can't see the anchor).
      and not {{ jet_infeasible_endpoint('coalesce(fs.is_airline, false)', 'coalesce(fs.is_jet, false)', 'ap.runway_length_ft', 'ap.airport_type') }}
    group by a.flight_id, a.origin_icao
),
origin_annot as (
    select *,
        sum(votes) over (partition by flight_id) as total_votes,
        max(votes) over (partition by flight_id) as top_votes,
        count() over (partition by flight_id) as distinct_airports
    from origin_ballot
),
origin_rank as (
    select oa.*, fs.is_airline as is_airline,
        sum(if(oa.votes = oa.top_votes, 1, 0)) over (partition by oa.flight_id) as n_top,
        row_number() over (partition by oa.flight_id
            order by oa.votes desc, (fs.is_airline and oa.sched) desc, oa.best_rank asc, oa.airport asc) as rn
    from origin_annot oa
    left join flight_shape fs on fs.flight_id = oa.flight_id
),
origin_win as (
    select flight_id,
        airport as origin_icao,
        {{ rank_source_label('best_rank') }} as origin_src,
        multiIf(total_votes = 1, 'single', distinct_airports = 1, 'unanimous', n_top > 1, 'tiebreak', 'majority') as origin_agr
    from origin_rank where rn = 1
),
origin_votes_map as (
    select flight_id, CAST((groupArray(airport), groupArray(votes)) AS Map(String, UInt64)) as origin_votes
    from origin_ballot group by flight_id
),
dest_ballot as (
    select a.flight_id as flight_id, a.dest_icao as airport, count() as votes,
           min(a.source_rank) as best_rank, max(coalesce(ap.scheduled_service, false)) as sched
    from {{ ref('int_flight_attach') }} a
    left join {{ ref('dim_airports') }} ap on ap.icao = a.dest_icao
    left join flight_shape fs on fs.flight_id = a.flight_id
    where a.dest_icao is not null
      -- SP4 ballot gate: anchor-identity backstop -- a NULL-callsign opinion must not launder an
      -- infeasible field onto an airline-jet flight (opinion-level gate can't see the anchor).
      and not {{ jet_infeasible_endpoint('coalesce(fs.is_airline, false)', 'coalesce(fs.is_jet, false)', 'ap.runway_length_ft', 'ap.airport_type') }}
    group by a.flight_id, a.dest_icao
),
dest_annot as (
    select *,
        sum(votes) over (partition by flight_id) as total_votes,
        max(votes) over (partition by flight_id) as top_votes,
        count() over (partition by flight_id) as distinct_airports
    from dest_ballot
),
dest_rank as (
    select da.*, fs.is_airline as is_airline,
        sum(if(da.votes = da.top_votes, 1, 0)) over (partition by da.flight_id) as n_top,
        row_number() over (partition by da.flight_id
            order by da.votes desc, (fs.is_airline and da.sched) desc, da.best_rank asc, da.airport asc) as rn
    from dest_annot da
    left join flight_shape fs on fs.flight_id = da.flight_id
),
dest_win as (
    select flight_id,
        airport as dest_icao,
        {{ rank_source_label('best_rank') }} as dest_src,
        multiIf(total_votes = 1, 'single', distinct_airports = 1, 'unanimous', n_top > 1, 'tiebreak', 'majority') as dest_agr
    from dest_rank where rn = 1
),
dest_votes_map as (
    select flight_id, CAST((groupArray(airport), groupArray(votes)) AS Map(String, UInt64)) as dest_votes
    from dest_ballot group by flight_id
),
n_src as (
    select flight_id, uniqExact(source) as n_sources from {{ ref('int_flight_attach') }} group by flight_id
),
box_observed as (
    -- the Japan box actually saw this flight (an in-box bronze fix in-window); reads bronze, not
    -- fact_state_snapshots, whose 30-day window would age adsblol/states-anchored flights out of the
    -- mart. Box is the japan_box_* vars (same as stg_states). EXISTS-semantics: dups fine.
    select distinct sp.flight_id as flight_id
    from {{ ref('int_flight_spine') }} sp
    join {{ source('bronze', 'opensky_states') }} s on s.icao24 = sp.icao24
    where s.snapshot_time between sp.flight_start and sp.flight_end
      and {{ in_japan_box('s.latitude', 's.longitude') }}
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
resolved as (
    select
        sp.flight_id as flight_id,
        sp.icao24 as icao24,
        trimBoth(sp.anchor_callsign) as callsign,
        sp.flight_start as start_time,
        sp.flight_end as end_time,
        sp.anchor_source as anchor_source,
        coalesce(nc.n_sources, 0) as n_sources,
        -- per endpoint: curated override > consensus winner
        coalesce(cur.origin_icao, ow.origin_icao) as origin_icao,
        multiIf(cur.origin_icao is not null, 'curated', ow.origin_icao is not null, ow.origin_src, null) as origin_source,
        multiIf(cur.origin_icao is not null, 'curated', ow.origin_icao is not null, ow.origin_agr, null) as origin_agreement,
        ovm.origin_votes as origin_votes,
        coalesce(cur.dest_icao, dw.dest_icao) as dest_icao,
        multiIf(cur.dest_icao is not null, 'curated', dw.dest_icao is not null, dw.dest_src, null) as dest_source,
        multiIf(cur.dest_icao is not null, 'curated', dw.dest_icao is not null, dw.dest_agr, null) as dest_agreement,
        dvm.dest_votes as dest_votes,
        ac.registration, ac.typecode,
        al.name as airline_name, al.country as airline_country,
        {{ ch_hex_country('sp.icao24') }} as reg_country
    from {{ ref('int_flight_spine') }} sp
    left join origin_win ow on ow.flight_id = sp.flight_id
    left join origin_votes_map ovm on ovm.flight_id = sp.flight_id
    left join dest_win dw on dw.flight_id = sp.flight_id
    left join dest_votes_map dvm on dvm.flight_id = sp.flight_id
    left join n_src nc on nc.flight_id = sp.flight_id
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
