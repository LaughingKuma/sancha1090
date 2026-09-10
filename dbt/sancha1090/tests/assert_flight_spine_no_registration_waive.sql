-- #225 tripwire, the complement of assert_flight_spine_no_variant_dup_anchors: an opinion the flight-number
-- arm waived must agree with its waiver on the letters after the digits -- N123AB is not ABC123's flight 123.
with pop as (
    -- Junk/number derivation is INLINED (see the variant-dup oracle): the naive last digit run finds the
    -- pairs the arm could waive, the tail equality below is the rule under test. Both sides, derived once.
    select side, icao24, win_start, win_end, callsign, source, rank,
           upper(trimBoth(callsign)) as cs,
           (callsign is null or not match(cs, '^[A-Z0-9]*[A-Z][A-Z0-9]*$')
              or cs = repeat(substring(cs, 1, 1), length(cs))) as junk,
           if(junk or not match(cs, '[0-9]'), NULL,
              replaceRegexpOne(arrayElement(extractAll(cs, '[0-9]+'), -1), '^0+', '')) as num,
           replaceRegexpOne(extract(cs, '[0-9]+[A-Z]{2,}$'), '^0+', '') as tail,
           {{ overlap_days('win_start', 'win_end') }} as overlap_day
    from (
        select 'sp' as side, icao24, flight_start as win_start, flight_end as win_end,
               anchor_callsign as callsign, anchor_source as source, anchor_rank as rank
        from {{ ref('int_flight_spine') }}
        where flight_start is not null and flight_end is not null
        union all
        -- the anti-joins' own population: a NULL callsign is junk (waived on any overlap) and D4.2 drops the
        -- over-cap states windows before the compat test, so neither can be number-waived
        select 'op', icao24, win_start, win_end, callsign, source, if(source = 'adsblol', 2, 3)
        from {{ ref('int_flight_opinions') }}
        where source in ('adsblol', 'opensky_states') and callsign is not null
          and win_start is not null and win_end is not null
          and not (source = 'opensky_states' and (toUnixTimestamp(win_end) - toUnixTimestamp(win_start)) / 3600.0
                                                  > {{ var('flight_max_hours') }})
    )
),
sp as (select * from pop where side = 'sp'),
op as (select * from pop where side = 'op'),
unanchored as (
    -- an anchor keeps its opinion's window, callsign and source: no row means the anti-join dropped it
    select o.* from op o
    left anti join {{ ref('int_flight_spine') }} s
      on s.icao24 = o.icao24 and s.flight_start = o.win_start and s.flight_end = o.win_end
     and s.anchor_callsign = o.callsign and s.anchor_source = o.source
)
-- Fires when nothing in the intended rule (exact, junk, 5D micro, equal number AND tail) explains the drop
-- but the naive number arm does. Same-hex day-keyed join as the anti-joins themselves (#191).
select o.icao24 as icao24, o.source as source, o.win_start as win_start, o.win_end as win_end,
       o.callsign as callsign, anyIf(h.callsign, h.num = o.num) as waiver_callsign
from unanchored o
join sp h on h.icao24 = o.icao24 and h.overlap_day = o.overlap_day
where h.rank < o.rank and h.win_start <= o.win_end and h.win_end >= o.win_start
group by o.icao24, o.source, o.win_start, o.win_end, o.callsign
having countIf(h.callsign = o.callsign or h.junk or o.junk
               or (h.num is not null and h.num = o.num and h.tail = o.tail)
               or (o.source = 'opensky_states'
                   and dateDiff('second', o.win_start, o.win_end) < {{ var('micro_anchor_max_s') }})) = 0
   and countIf(h.num is not null and h.num = o.num) > 0
