from __future__ import annotations


# Field buckets kept in lock-step with the edge producer's capture_v2._build_schema() (test_adsb_schema
# guards drift); adsb_drift reads them as the "typed readsb fields" view to flag new untyped keys.
STRING_FIELDS = ["hex", "type", "r", "t", "desc", "category", "sil_type",
                 "emergency", "ownOp", "year", "flight", "squawk", "alt_baro"]
DOUBLE_FIELDS = ["now", "lat", "lon", "r_dst", "r_dir", "seen", "seen_pos", "rssi",
                 "gs", "mach", "track", "track_rate", "roll", "mag_heading",
                 "true_heading", "nav_qnh", "nav_heading"]
INT_FIELDS = ["messages", "nic", "rc", "version", "nac_p", "nac_v", "sil", "nic_baro",
              "gva", "sda", "alert", "spi", "alt_geom", "ias", "tas", "baro_rate",
              "geom_rate", "nav_altitude_mcp", "nav_altitude_fms", "wd", "ws", "oat", "tat"]
LIST_FIELDS = ["nav_modes", "mlat", "tisb"]
JSON_FIELDS = ["acas_ra"]

# Column order mirrors capture_v2._build_schema(): capture_ts, the typed readsb buckets, then our
# own _raw_json/_schema_version. This is the PRODUCER PARQUET contract (test_adsb_schema guards it).
ADSB_COLUMNS = (
    ["capture_ts"]
    + STRING_FIELDS + DOUBLE_FIELDS + INT_FIELDS + LIST_FIELDS + JSON_FIELDS
    + ["_raw_json", "_schema_version"]
)

# CH_ADSB_COLUMNS: the bronze.adsb_states target order (v6.3+) — DIVERGES from the producer Parquet.
# _raw_json is eliminated from CH (39% of the hot table; the only CH readers decoded dbFlags) and replaced
# by a typed db_flags column baked at load; the producer Parquet keeps _raw_json (the drift scan reads it).
CH_ADSB_COLUMNS = (
    ["capture_ts"]
    + STRING_FIELDS + DOUBLE_FIELDS + INT_FIELDS + LIST_FIELDS + JSON_FIELDS
    + ["db_flags", "_schema_version"]
)
