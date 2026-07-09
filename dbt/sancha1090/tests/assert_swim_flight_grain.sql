{{ config(tags=['swim']) }}
-- Exactly one latest-amendment row per flight identity.
select flight_key, count(*) as n
from {{ ref('int_swim_flight') }}
group by 1
having count(*) > 1
