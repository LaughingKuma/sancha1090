{{ config(
    materialized='table',
    tags=['reconcile'],
    query_settings={'max_memory_usage': 8000000000},
) }}

-- Materialized out of fct_flights_reconciled because a CTE re-evaluates per reference in ClickHouse,
-- and the coherence gate reads each ballot winner twice (OOM'd at 11+ GiB inline).
with flight_shape as (
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
        votes as origin_votes_n,
        best_rank as origin_best_rank,
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
        votes as dest_votes_n,
        best_rank as dest_best_rank,
        {{ rank_source_label('best_rank') }} as dest_src,
        multiIf(total_votes = 1, 'single', distinct_airports = 1, 'unanimous', n_top > 1, 'tiebreak', 'majority') as dest_agr
    from dest_rank where rn = 1
),
dest_votes_map as (
    select flight_id, CAST((groupArray(airport), groupArray(votes)) AS Map(String, UInt64)) as dest_votes
    from dest_ballot group by flight_id
),
combined as (
    select
        coalesce(ow.flight_id, dw.flight_id) as flight_id,
        ow.origin_icao, ow.origin_votes_n, ow.origin_best_rank, ow.origin_src, ow.origin_agr,
        ovm.origin_votes,
        dw.dest_icao, dw.dest_votes_n, dw.dest_best_rank, dw.dest_src, dw.dest_agr,
        dvm.dest_votes
    from origin_win ow
    full outer join dest_win dw on dw.flight_id = ow.flight_id
    left join origin_votes_map ovm on ovm.flight_id = coalesce(ow.flight_id, dw.flight_id)
    left join dest_votes_map dvm on dvm.flight_id = coalesce(ow.flight_id, dw.flight_id)
),
-- Computed once here (a targeted per-flight equi-join) rather than as a DISTINCT over the full
-- ~2.5M-row attach table referenced twice downstream -- same class of fix as this whole model.
pair_asserted as (
    select distinct c.flight_id
    from combined c
    join {{ ref('int_flight_attach') }} a
      on a.flight_id = c.flight_id and a.origin_icao = c.origin_icao and a.dest_icao = c.dest_icao
    where c.origin_icao is not null and c.dest_icao is not null
)
select c.*, (pa.flight_id is not null) as pair_asserted
from combined c
left join pair_asserted pa on pa.flight_id = c.flight_id
