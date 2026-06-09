-- Receiver coverage polygon for livemap, computed in batch from Trino history by refresh_range_outline.
-- `gen` = load generation: the loader writes a new gen then drops older ones, so readers (max gen) only
-- ever see a complete polygon (atomic refresh without a txn). Created empty so a fresh install can serve it.
CREATE TABLE IF NOT EXISTS range_outline (
    bin integer,
    lat double precision,
    lon double precision,
    dist_nmi double precision,
    gen bigint
);
