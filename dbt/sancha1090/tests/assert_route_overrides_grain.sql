-- One override row per (callsign, valid_from); overlapping windows resolve by latest valid_from.
select callsign, valid_from
from {{ ref('dim_route_overrides') }}
group by callsign, valid_from
having count(*) > 1
