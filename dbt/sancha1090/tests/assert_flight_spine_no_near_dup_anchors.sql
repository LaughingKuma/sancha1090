-- D4.1: after clustering, no two opensky_flights anchors for one icao24 sit within anchor_merge_gap_min
-- with a compatible callsign. Compatible = equal or either blank (mirrors the merge rule).
with a as (
    select icao24, flight_start, flight_end, anchor_callsign
    from {{ ref('int_flight_spine') }} where anchor_source = 'opensky_flights'
),
pairs as (
    select a1.icao24 as icao24, a1.flight_start as a1_start, a1.flight_end as a1_end,
           a2.flight_start as a2_start, a2.flight_end as a2_end
    from a a1
    join a a2 on a2.icao24 = a1.icao24
    where a1.flight_start < a2.flight_start
      and a2.flight_start <= a1.flight_end + interval {{ var('anchor_merge_gap_min') }} minute
      and (a1.anchor_callsign = a2.anchor_callsign or a1.anchor_callsign is null or a2.anchor_callsign is null)
),
-- #150 exemption: break evidence re-derived independently (same gap islands, strict immediate
-- predecessor) so only an anchor BORN at a turnaround seam is excused — any-pair containment would launder.
turn as (
    select distinct icao24, win_start, prev_win_start, prev_win_end
    from (
        select icao24, win_start, origin_icao,
            lagInFrame(dest_icao, 1, NULL) over (partition by icao24, gap_group order by win_start, win_end
                                    rows between 1 preceding and current row) as prev_dest,
            lagInFrame(toNullable(win_start), 1, NULL) over (partition by icao24, gap_group order by win_start, win_end
                                    rows between 1 preceding and current row) as prev_win_start,
            lagInFrame(toNullable(win_end), 1, NULL) over (partition by icao24, gap_group order by win_start, win_end
                                    rows between 1 preceding and current row) as prev_win_end
        from (
            select *,
                sum(gap_break) over (partition by icao24 order by win_start, win_end
                                     rows between unbounded preceding and current row) as gap_group
            from (
                select *,
                    case when max_prev_end is null
                              or win_start > max_prev_end + interval {{ var('anchor_merge_gap_min') }} minute
                         then 1 else 0 end as gap_break
                from (
                    select icao24, win_start, win_end, origin_icao, dest_icao,
                        max(win_end) over (partition by icao24 order by win_start, win_end
                                           rows between unbounded preceding and 1 preceding) as max_prev_end
                    from {{ ref('int_flight_opinions') }}
                    where source = 'opensky_flights'
                )
            )
        )
    )
    where prev_dest is not null and origin_icao is not null and prev_dest = origin_icao
      and prev_win_end is not null and win_start >= prev_win_end
)
-- pair-specific: a2 starts exactly at its break row AND that break's immediate predecessor sits inside
-- a1 — else an A/B/A layout lets a2's genuine birth from B excuse the unrelated (A, a2) pair.
select p.icao24
from pairs p
left join turn t on t.icao24 = p.icao24 and t.win_start = p.a2_start
group by p.icao24, p.a1_start, p.a1_end, p.a2_start, p.a2_end
having sum(if(t.icao24 is not null
              and t.prev_win_start >= p.a1_start and t.prev_win_end <= p.a1_end, 1, 0)) = 0
