import os
import sys

from solace.messaging.messaging_service import MessagingService
from solace.messaging.resources.queue import Queue
from solace.messaging.config.transport_security_strategy import TLS

# Throwaway SCDS spike: connect to the live TFMData queue, capture N raw fltdMessages, dump XML for the parser.
N = int(os.environ.get("SWIM_SPIKE_N", "50"))
OUT = os.environ.get("SWIM_SPIKE_OUT", "tests/fixtures/swim")
os.makedirs(OUT, exist_ok=True)

svc = (
    MessagingService.builder()
    .from_properties({
        "solace.messaging.transport.host": os.environ["SWIM_HOST"],            # tcps://<host>:55443
        "solace.messaging.service.vpn-name": os.environ["SWIM_VPN"],
        "solace.messaging.authentication.scheme.basic.username": os.environ["SWIM_USER"],
        "solace.messaging.authentication.scheme.basic.password": os.environ["SWIM_PASS"],
        # SCDS Data Compression Standard is mandatory — enable it (confirm the accepted level with the helpdesk).
        "solace.messaging.transport.compression-level": os.environ.get("SWIM_COMPRESSION", "9"),
    })
    .with_transport_security_strategy(
        TLS.create().with_certificate_validation(
            False, validate_server_name=True,   # 1st arg is ignore_expiration → False so expired certs are rejected
            # Solace needs an explicit CA trust store (OpenSSL-hashed dir); default to the system bundle.
            trust_store_file_path=os.environ.get("SWIM_TRUSTSTORE", "/etc/ssl/certs")))
    .build()
)
svc.connect()
print("connected; solace-pubsubplus wheel imports + TLS handshake OK", file=sys.stderr)

receiver = svc.create_persistent_message_receiver_builder().build(
    Queue.durable_exclusive_queue(os.environ["SWIM_QUEUE"]))
receiver.start()
print(f"bound {os.environ['SWIM_QUEUE']}; capturing up to {N} messages...", file=sys.stderr)

got = 0
while got < N:
    msg = receiver.receive_message(timeout=30000)
    if msg is None:
        break
    body = msg.get_payload_as_string() or (msg.get_payload_as_bytes() or b"").decode("utf-8", "replace")
    path = f"{OUT}/msg_{got:03d}.xml"
    with open(path, "w") as f:
        f.write(body)
    print(path)
    receiver.ack(msg)   # spike only — the real consumer ACKs after the durable write (plan Task 3)
    got += 1

receiver.terminate()
svc.disconnect()
print(f"captured {got} messages to {OUT}", file=sys.stderr)
