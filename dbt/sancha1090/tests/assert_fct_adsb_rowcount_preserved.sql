{{ config(tags=['adsb']) }}
-- Fails if the LEFT joins ever drop or fan out rows: fct must mirror bronze 1:1.
select f.n as fct_rows, b.n as bronze_rows
from (select count(*) n from {{ ref('fct_adsb_state') }}) f,
     (select count(*) n from {{ source('bronze', 'adsb_states') }}) b
where f.n <> b.n
