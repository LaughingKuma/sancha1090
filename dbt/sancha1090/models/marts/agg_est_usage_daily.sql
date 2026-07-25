{{ config(materialized='table') }}

-- Ledger item 1: the PR-4 demand gate as a standing query. producer='test' rows are the
-- sanctioned identity-test exhaust (rev 10.1) and never count as usage.
with per_estimate as (
    select
        estimate_id,
        toDate(any(computed_at)) as day,
        any(producer) as producer,
        multiIf(isNull(any(flight_id)), 'live',
                any(input_provisional) = 1, 'provisional',
                'settled') as arm,
        any(subject_key) as subject_key,
        countIf(seg_idx > 0) as segments
    from {{ source('bronze', 'path_estimates') }}
    where producer != 'test'
    group by estimate_id
)

select
    day,
    producer,
    arm,
    count() as requests,
    countIf(segments > 0) as served,
    countIf(segments = 0) as all_skipped,
    sum(segments) as segments,
    uniqExact(subject_key) as subjects
from per_estimate
group by day, producer, arm
-- `producer` and `segments` each name both a source column and a same-named aggregate alias;
-- without this CH binds the WHERE / countIf refs to the alias and raises ILLEGAL_AGGREGATION.
settings prefer_column_name_to_alias = 1
