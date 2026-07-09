import logging
import threading
import xml.etree.ElementTree as ET
import polars as pl
from datetime import datetime, timezone
from include.swim_parser import parse_envelope

logger = logging.getLogger(__name__)

_BRONZE_COLS = ["gufi","flight_ref","acid","computer_id","msg_type","dep_point","dep_point_kind","dep_point_raw",
    "arr_point","arr_point_kind","arr_point_raw","filed_departure_time","filed_departure_time_raw",
    "filed_arrival_time","filed_arrival_time_raw","msg_timestamp","source_received_at","ingested_at","raw_xml"]

# Pin the batch schema so an all-NULL datetime/string flush can't infer a polars Null column and drift the
# Parquet schema across bronze/swim_raw parts. Parser times are naive UTC; the consumer stamps are tz-aware UTC.
_STR_COLS = {"gufi","flight_ref","acid","computer_id","msg_type","dep_point","dep_point_kind","dep_point_raw",
    "arr_point","arr_point_kind","arr_point_raw","filed_departure_time_raw","filed_arrival_time_raw","raw_xml"}
_SWIM_SCHEMA = ({c: pl.String for c in _STR_COLS}
    | {c: pl.Datetime("us") for c in ("filed_departure_time", "filed_arrival_time", "msg_timestamp")}
    | {c: pl.Datetime("us", "UTC") for c in ("source_received_at", "ingested_at")})

def handle_message(payload: bytes, buffer: list, *, now) -> bool:
    # A Solace message is an envelope of many fltdMessages → many rows. True if any row was buffered.
    if not payload:
        # None/empty extraction (unexpected payload type): ACK as non-target — never crash into a redeliver loop.
        logger.warning("handle_message: empty/unextractable payload, dropping")
        return False
    try:
        rows = parse_envelope(payload)
    except ET.ParseError:
        # Malformed payload: treat as non-target so the caller ACKs it instead of redelivering forever.
        logger.warning("handle_message: malformed XML payload, dropping")
        return False
    if not rows:
        return False
    ts = now()
    for row in rows:
        row["source_received_at"] = ts     # volatile; excluded from _dedup_fp + ORDER BY
        buffer.append(row)
    return True

def flush_batch(rows, *, write, record, now):
    if not rows:
        return None
    ts = now()
    for r in rows:
        r["ingested_at"] = ts              # volatile flush stamp
    df = pl.DataFrame(rows, schema_overrides=_SWIM_SCHEMA).select([c for c in _BRONZE_COLS if c in rows[0]])
    key = f"bronze/swim_raw/dt={ts:%Y-%m-%d}/part-{ts:%Y%m%dT%H%M%S%f}.parquet"
    uri = write(df, key)                   # write_parquet returns s3://<bucket>/<key>; raises on failure
    epochs = [int(r["source_received_at"].timestamp()) for r in rows if r.get("source_received_at")]
    record(uri, min(epochs) if epochs else None, max(epochs) if epochs else None, len(rows))  # use the real uri
    return uri

def drain(receiver, *, write, record, flush_every=500, flush_seconds=10,
          now=lambda: datetime.now(timezone.utc), max_messages=None, on_activity=lambda: None):
    buffer, pending, seen = [], [], 0
    last_flush = now()
    while max_messages is None or seen < max_messages:
        msg = receiver.receive_message(timeout=int(flush_seconds * 1000) or 1000)
        if msg is not None:
            on_activity()   # liveness heartbeat only — observational, never touches the ack/flush decisions below
            if handle_message(_payload(msg), buffer, now=now):
                pending.append(msg)        # parsed → ACK only after the durable flush
            else:
                receiver.ack(msg)          # non-target: nothing to persist; ACK now or it redelivers forever
            seen += 1
        # flush on idle, a full batch, OR the staleness deadline — else a steady trickle sits unacked past flush_seconds.
        stale = (now() - last_flush).total_seconds() >= flush_seconds
        if buffer and (msg is None or len(buffer) >= flush_every or stale):
            flush_batch(buffer, write=write, record=record, now=now)   # raises -> no ack below
            for m in pending:
                receiver.ack(m)            # ACK ONLY after durable write+record succeeded
            buffer, pending = [], []
            last_flush = now()

def _payload(msg):
    b = getattr(msg, "payload", None)
    if b is not None:
        return b if isinstance(b, bytes) else str(b).encode()
    # Live SCDS messages arrive string-typed (JMS TextMessage — spike-proven): bytes accessor returns None for
    # them, and an unhandled None would crash the drain into a redeliver loop. String first, bytes as fallback.
    s = msg.get_payload_as_string()
    if s is not None:
        return s.encode("utf-8", "replace")
    b = msg.get_payload_as_bytes()
    return bytes(b) if b is not None else None

class Liveness:
    # Thread-safe last-activity clock shared between the drain thread (touch) and the /healthz handler (read).
    def __init__(self, now=lambda: datetime.now(timezone.utc)):
        self._now = now
        self._lock = threading.Lock()
        self._last_activity = None

    def touch(self):
        with self._lock:
            self._last_activity = self._now()

    @property
    def last_activity(self):
        with self._lock:
            return self._last_activity

def is_healthy(*, now, last_activity, thread_alive, max_age_s):
    # Pure decision, no threads/clocks: a dead receiver thread or a feed gone silent past max_age_s is a wedge.
    if not thread_alive or last_activity is None:
        return False
    return (now - last_activity).total_seconds() <= max_age_s
