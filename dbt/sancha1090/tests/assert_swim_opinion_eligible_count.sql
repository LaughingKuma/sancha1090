{{ config(tags=['swim']) }}
-- int_swim_opinion must equal exactly the vote-eligible int_swim_flight rows.
select o.n as opinion_rows, e.n as eligible_rows
from (select count(*) n from {{ ref('int_swim_opinion') }}) o,
     (select count(*) n from {{ ref('int_swim_flight') }}
       where icao24 is not null and hex_ambiguous = 0
         and (origin_icao is not null or dest_icao is not null)) e
where o.n <> e.n
