-- Reads the ClickHouse bronze table directly; columns are already typed, so no ::casts.
-- Full bronze history, no rolling window (measured trivial at Japan-box scale — docs/notes/2026-07-18);
-- bronze is the only retention boundary. Japan scope (v5.0): filter geographically, NOT by region label — the pre-v5.0
-- 'east_asia' box already covered Japan, so a region='japan' filter would drop
-- recent Japan history. Box is the japan_box_* vars (mirrors include/regions.py).
{{ states_staging_body(source('bronze', 'opensky_states'), in_japan_box('latitude', 'longitude')) }}
