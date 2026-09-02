-- 5C tripwire: two spellings of one flight number on one airframe in overlapping windows must leave
-- only the higher-authority anchor -- the anti-joins' flight-number compat arm owns that suppression.
with a as (
    -- Junk/number derivation is INLINED, never the model's macros: an independent oracle must fire
    -- when a macro regression stops the model suppressing, not inherit the same misclassification.
    select icao24, flight_start, flight_end, anchor_callsign, anchor_rank,
           upper(trimBoth(anchor_callsign)) as cs
    from {{ ref('int_flight_spine') }}
    where anchor_callsign is not null
),
n as (
    select *,
        if(not match(cs, '^[A-Z0-9]*[A-Z][A-Z0-9]*$')
           or cs = repeat(substring(cs, 1, 1), length(cs))
           or not match(cs, '[0-9]'),
           NULL,
           if(replaceRegexpOne(arrayElement(extractAll(cs, '[0-9]+'), -1), '^0+', '') = '', '0',
              replaceRegexpOne(arrayElement(extractAll(cs, '[0-9]+'), -1), '^0+', ''))) as cs_num
    from a
)
-- Junk yields cs_num NULL, dropped by the non-null equality: NULL-compat is a different rule than a
-- variant miss. Rank-1 pairs are the merge rule's business (assert_flight_spine_no_near_dup_anchors).
-- a1 is the suppressible side (strictly weaker authority); the strict > also keeps each pair once.
-- Same-hex self-join with no day key on purpose: pairs are per-airframe and bounded (0.4 s live).
select a1.icao24 as icao24, a1.anchor_rank as a1_rank, a1.flight_start as a1_start,
       a1.anchor_callsign as a1_callsign, a2.anchor_rank as a2_rank, a2.flight_start as a2_start,
       a2.anchor_callsign as a2_callsign
from n a1
join n a2 on a2.icao24 = a1.icao24
where a1.anchor_rank > a2.anchor_rank
  and a1.flight_start <= a2.flight_end and a2.flight_start <= a1.flight_end
  and a1.anchor_callsign != a2.anchor_callsign
  and a1.cs_num is not null and a1.cs_num = a2.cs_num
