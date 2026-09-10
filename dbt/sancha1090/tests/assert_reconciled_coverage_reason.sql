{{ config(tags=['reconcile']) }}
-- The label is derived from the chain that voted: re-derive it from int_flight_attached_votes for every anchor
-- source and fail on a disagreement in either direction (a label without a cruise end, or a cruise end unlabelled).
with expected as (
    select r.flight_id as flight_id,
           coalesce(r.foreign_unresolved_reason, '') = 'coverage' as labelled,
           (r.origin_icao is null) != (r.dest_icao is null)
             and coalesce((r.origin_icao is null and not c.first_on_ground and c.first_alt_m >= {{ var('legs_cruise_alt_m') }})
                          or (r.dest_icao is null and not c.last_on_ground and c.last_alt_m >= {{ var('legs_cruise_alt_m') }}),
                          false) as cruise_end
    from {{ ref('fct_flights_reconciled') }} r
    left join (select flight_id, icao24, win_start from {{ ref('int_flight_attached_votes') }} where source = 'adsblol') v
           on v.flight_id = r.flight_id
    left join {{ ref('int_flight_chains_adsblol') }} c on c.icao24 = v.icao24 and c.chain_start = v.win_start
)
select flight_id, if(labelled, 'labelled_without_cruise_end', 'cruise_end_unlabelled') as defect
from expected
where labelled != cruise_end
