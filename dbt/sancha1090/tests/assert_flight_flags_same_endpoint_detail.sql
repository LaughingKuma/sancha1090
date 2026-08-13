-- Membership is pinned by the projection contract; the detail STRING itself (format, modal gates,
-- existence, type) is not. Four arms, violation-rows style: any row returned is a failure.
with same_endpoint as (
    select f.flight_id, f.detail, r.origin_icao
    from {{ ref('fct_flight_flags') }} f
    join {{ ref('fct_flights_reconciled') }} r on r.flight_id = f.flight_id
    where f.flag_class = 'same_endpoint'
),
-- Output-side only: proves rendered⇒gates (soundness). It cannot prove eligible⇒rendered
-- (completeness) — that re-derivation lives in assert_flight_flags_same_endpoint_modal_completeness.sql.
modal_bearing as (
    select
        flight_id,
        origin_icao,
        extractGroups(detail, '^at [A-Z0-9]{4} vs modal ([A-Z0-9]{4}) ([0-9]+)/([0-9]+)$') as g
    from same_endpoint
    where match(detail, '^at [A-Z0-9]{4} vs modal [A-Z0-9]{4} [0-9]+/[0-9]+$')
),

-- 1) SHAPE: anchored on truth — detail must read 'at <this flight's own reconciled airport>' with an
-- optional, anchored modal suffix. ICAO codes are alphanumeric, safe to interpolate into the regex.
shape as (
    select flight_id, 'shape' as violation
    from same_endpoint
    where not match(detail, concat('^at ', origin_icao, '( vs modal [A-Z0-9]{4} [0-9]+/[0-9]+)?$'))
),

-- 2) RENDERED-MODAL GATES: a self-referential or sub-threshold modal that still made it into the
-- rendered string fails here, tested from the output side.
rendered_modal_gate as (
    select flight_id, 'rendered_modal_gate' as violation
    from modal_bearing
    where g[1] = origin_icao
       or toUInt64(g[2]) < {{ var('flag_diversion_min_support') }}
       or toUInt64(g[2]) / greatest(toUInt64(g[3]), 1) < {{ var('flag_diversion_min_share') }}
),

-- 3) EXISTENCE: a regression dropping every modal clause (or every bare form) must fail; malformed
-- details count toward neither form (they fail arm 1). Warehouse-size guards as class_nonempty.
existence as (
    select cast(0 as Nullable(UInt64)) as flight_id, 'no_modal_bearing_detail' as violation
    where (select count() from modal_bearing) = 0
      and (select count() from {{ ref('fct_flights_reconciled') }}) >= 1000
      and (select dateDiff('day', min(start_time), max(start_time)) from {{ ref('fct_flights_reconciled') }}) >= 30

    union all

    select cast(0 as Nullable(UInt64)) as flight_id, 'no_bare_form_detail' as violation
    where (select countIf(match(detail, '^at [A-Z0-9]{4}$')) from same_endpoint) = 0
      and (select count() from {{ ref('fct_flights_reconciled') }}) >= 1000
      and (select dateDiff('day', min(start_time), max(start_time)) from {{ ref('fct_flights_reconciled') }}) >= 30
),

-- 4) TYPE: any() over a Nullable column reports Nullable(String), so this pins the plain-String
-- guarantee the model's coalesce discipline exists for.
type_check as (
    select cast(0 as Nullable(UInt64)) as flight_id, 'detail_not_plain_string' as violation
    where (select toTypeName(any(detail)) from {{ ref('fct_flight_flags') }}) != 'String'
)

select flight_id, violation from shape
union all
select flight_id, violation from rendered_modal_gate
union all
select flight_id, violation from existence
union all
select flight_id, violation from type_check
