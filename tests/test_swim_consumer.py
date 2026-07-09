from pathlib import Path
import pytest
from include import swim_consumer as sc

FIX = Path(__file__).parent / "fixtures" / "swim"
_HAVE_FIX = (FIX / "msg_008.xml").exists()
# Real fixtures are gitignored flight data (local-only) — mark only the tests that need them, so the
# synthetic-fixture and no-fixture tests still run in CI.
needs_real_fixtures = pytest.mark.skipif(not _HAVE_FIX, reason="swim fixtures are gitignored / local-only")

FLIGHTPLAN_XML = (FIX / "msg_008.xml").read_bytes() if _HAVE_FIX else b""   # departure + amendment (kept)
NO_KEPT_XML = (FIX / "msg_000.xml").read_bytes() if _HAVE_FIX else b""      # all trackInformation → []
SYNTH_XML = (Path(__file__).parent / "fixtures" / "swim_synthetic" / "amendment.xml").read_bytes()


class FakeMsg:
    def __init__(self, payload): self.payload = payload; self.acked = False


class FakeStringMsg:
    # mimics a real string-typed InboundMessage (JMS TextMessage): NO .payload attr, bytes accessor returns None.
    def __init__(self, text): self._text = text; self.acked = False
    def get_payload_as_string(self): return self._text
    def get_payload_as_bytes(self): return None


class FakeEmptyMsg:
    # pathological payload type: both accessors empty — must be ACKed as non-target, never crash the drain.
    def __init__(self): self.acked = False
    def get_payload_as_string(self): return None
    def get_payload_as_bytes(self): return None


class FakeReceiver:
    def __init__(self, msgs): self.msgs = list(msgs); self.acked = []
    def receive_message(self, timeout): return self.msgs.pop(0) if self.msgs else None  # noqa: ARG002 (real API kw)
    def ack(self, m): m.acked = True; self.acked.append(m)


@needs_real_fixtures
def test_ack_only_after_successful_write_and_record():
    writes, records = [], []
    r = FakeReceiver([FakeMsg(FLIGHTPLAN_XML)])
    sc.drain(r, write=lambda _df, key: writes.append(key) or f"s3://b/{key}",
             record=lambda uri, _smin, _smax, _rows: records.append(uri),
             flush_every=1, flush_seconds=0, max_messages=1)
    assert writes and records                       # durable side effects happened
    assert records[0].startswith("s3://b/")         # the real returned uri was recorded, not a hardcoded one
    assert r.acked and all(m.acked for m in r.acked)  # ack actually happened, and only after the durable write


@needs_real_fixtures
def test_no_ack_when_write_fails():
    r = FakeReceiver([FakeMsg(FLIGHTPLAN_XML)])
    def boom(_df, _key): raise IOError("garage down")
    with pytest.raises(IOError):
        sc.drain(r, write=boom, record=lambda *_a: None,
                 flush_every=1, flush_seconds=0, max_messages=1)
    assert r.acked == []                            # unacked → durable queue redelivers


@needs_real_fixtures
def test_non_target_message_acked_without_write():
    # well-formed envelope, no kept messages (all trackInformation) → parse_envelope returns [].
    writes = []
    r = FakeReceiver([FakeMsg(NO_KEPT_XML)])
    sc.drain(r, write=lambda _df, key: writes.append(key) or "s3://b/k",
             record=lambda *_a: None, flush_every=1, flush_seconds=0, max_messages=1)
    assert writes == []                             # nothing persisted
    assert len(r.acked) == 1                         # but ACKed, so it doesn't redeliver forever


def test_malformed_payload_acked_without_crash():
    # malformed XML → parse_envelope raises ET.ParseError; handle_message must swallow it, not crash the loop.
    writes = []
    r = FakeReceiver([FakeMsg(b"<broken")])
    sc.drain(r, write=lambda _df, key: writes.append(key) or "s3://b/k",
             record=lambda *_a: None, flush_every=1, flush_seconds=0, max_messages=1)
    assert writes == []                             # nothing persisted
    assert len(r.acked) == 1                         # ACKed so it doesn't redeliver forever


def test_string_typed_message_is_extracted_and_flushed():
    # live SCDS messages are string-typed (JMS TextMessage; spike-proven): bytes accessor yields None, so the
    # string path MUST work end-to-end or the consumer crash-loops on first real traffic.
    writes, records = [], []
    r = FakeReceiver([FakeStringMsg(SYNTH_XML.decode())])
    sc.drain(r, write=lambda _df, key: writes.append(key) or f"s3://b/{key}",
             record=lambda uri, *_a: records.append(uri),
             flush_every=1, flush_seconds=0, max_messages=1)
    assert writes and records                       # the kept synthetic amendment was buffered + flushed
    assert r.acked and all(m.acked for m in r.acked)


def test_unextractable_payload_acked_without_crash():
    # both accessors empty → _payload None → non-target ACK, never ET.fromstring(None) TypeError crash-loop.
    writes = []
    r = FakeReceiver([FakeEmptyMsg()])
    sc.drain(r, write=lambda _df, key: writes.append(key) or "s3://b/k",
             record=lambda *_a: None, flush_every=1, flush_seconds=0, max_messages=1)
    assert writes == []
    assert len(r.acked) == 1
