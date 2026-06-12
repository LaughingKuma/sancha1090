-- Raw bytes, not typed columns: MVs decode jsonb exactly like silver decodes _raw_json,
-- so quirky messages (alt_baro='ground', absent dbFlags, ~hex) can't be dropped at ingest.
CREATE SOURCE IF NOT EXISTS adsb_live (data bytea)
INCLUDE timestamp AS kafka_ts
WITH (
    connector = 'kafka',
    topic = 'adsb.live',
    properties.bootstrap.server = 'redpanda:9092',
    -- replay whatever the ~35-min topic retains on first creation; offsets checkpoint after
    scan.startup.mode = 'earliest'
)
FORMAT PLAIN ENCODE BYTES;
