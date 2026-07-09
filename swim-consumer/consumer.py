# swim-consumer/consumer.py — health server + the run loop; single worker owns the connection.
import os, threading, http.server
from solace.messaging.messaging_service import MessagingService
from solace.messaging.resources.queue import Queue
from solace.messaging.config.transport_security_strategy import TLS
from include.swim_consumer import drain
from include.s3_helpers import write_parquet
from include import manifest

class _Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass

def _serve_health():
    http.server.HTTPServer(("0.0.0.0", 8000), _Health).serve_forever()

def main():
    threading.Thread(target=_serve_health, daemon=True).start()
    svc = (MessagingService.builder().from_properties({
        "solace.messaging.transport.host": os.environ["SWIM_HOST"],
        "solace.messaging.service.vpn-name": os.environ["SWIM_VPN"],
        "solace.messaging.authentication.scheme.basic.username": os.environ["SWIM_USER"],
        "solace.messaging.authentication.scheme.basic.password": os.environ["SWIM_PASS"],
        "solace.messaging.transport.compression-level": os.environ.get("SWIM_COMPRESSION", "9"),
    }).with_transport_security_strategy(
        TLS.create().with_certificate_validation(
            False, validate_server_name=True,   # 1st arg is ignore_expiration → False so expired certs are rejected
            # SCDS TLS requires an explicit CA trust store (spike: FAILED_LOADING_TRUSTSTORE without it).
            trust_store_file_path=os.environ.get("SWIM_TRUSTSTORE", "/etc/ssl/certs"))).build())
    svc.connect()
    rx = svc.create_persistent_message_receiver_builder().build(
        Queue.durable_exclusive_queue(os.environ["SWIM_QUEUE"]))
    rx.start()
    drain(rx, write=write_parquet, record=manifest.record_load)   # runs forever

if __name__ == "__main__":
    main()
