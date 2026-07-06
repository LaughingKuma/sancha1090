{{ config(materialized='table', tags=['reconcile']) }}

-- Cross-source consensus flight mart: per flight, plurality per endpoint, authority + scheduled-service
-- tiebreak, single-source flagged, curated override on top; full provenance. Additive -- pure lanes untouched.
with flight_shape as (
    -- only airliners get the sched tiebreak below; a real military flight may legitimately land at RJCJ.
    select flight_id, {{ airline_shaped('anchor_callsign') }} as is_airline
    from {{ ref('int_flight_spine') }}
),
origin_ballot as (
    select a.flight_id as flight_id, a.origin_icao as airport, count() as votes,
           min(a.source_rank) as best_rank, max(coalesce(ap.scheduled_service, false)) as sched
    from {{ ref('int_flight_attach') }} a
    left join {{ ref('dim_airports') }} ap on ap.icao = a.origin_icao
    where a.origin_icao is not null
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
        transform(best_rank, [1, 2, 3], ['opensky_flights', 'adsblol', 'opensky_states'], 'opensky_states') as origin_src,
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
    where a.dest_icao is not null
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
        transform(best_rank, [1, 2, 3], ['opensky_flights', 'adsblol', 'opensky_states'], 'opensky_states') as dest_src,
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
    -- the Japan box actually saw this flight (a fact_state_snapshots fix in-window); attach's callsign
    -- guard would miss a states leg the sessionizer relabeled to a sibling flight.
    select distinct sp.flight_id as flight_id
    from {{ ref('int_flight_spine') }} sp
    join {{ ref('fact_state_snapshots') }} s on s.icao24 = sp.icao24
    where s.snapshot_time between sp.flight_start and sp.flight_end
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
)
select
    sp.flight_id as flight_id,
    sp.icao24 as icao24,
    sp.anchor_callsign as callsign,
    sp.flight_start as start_time,
    sp.flight_end as end_time,
    sp.anchor_source as anchor_source,
    coalesce(nc.n_sources, 0) as n_sources,
    -- origin: curated override > consensus
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
