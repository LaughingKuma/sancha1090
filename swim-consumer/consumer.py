# swim-consumer/consumer.py — health server + the run loop; single worker owns the connection.
import logging, os, sys, threading, http.server
from datetime import datetime, timezone
from solace.messaging.messaging_service import MessagingService
from solace.messaging.resources.queue import Queue
from solace.messaging.config.transport_security_strategy import TLS
from include.swim_consumer import drain, is_healthy, Liveness
from include.s3_helpers import write_parquet
from include import manifest

logger = logging.getLogger(__name__)
HEALTH_MAX_AGE_S = int(os.environ.get("SWIM_HEALTH_MAX_AGE_S", "600"))

def _make_health_handler(liveness, receiver_thread):
    class _Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            # A health decision that can't be computed can't attest health — recycle like the drain watchdog.
            # (http.server would otherwise route the exception to handle_error and keep serving a broken check.)
            try:
                healthy = is_healthy(now=datetime.now(timezone.utc), last_activity=liveness.last_activity,
                                      thread_alive=receiver_thread.is_alive(), max_age_s=HEALTH_MAX_AGE_S)
            except Exception:
                logger.exception("health decision failed, exiting for restart")
                os._exit(1)
            # A write failure just means the client (healthcheck curl) hung up mid-reply — harmless, keep serving
            # (exiting on benign client-socket noise would cause restart storms).
            try:
                self.send_response(200 if healthy else 503)
                self.end_headers()
                self.wfile.write(b"ok" if healthy else b"unhealthy")
            except Exception:
                pass
        def log_message(self, *a): pass
    return _Health

def _serve_health(liveness, receiver_thread):
    # A dead health endpoint must recycle the process like a dead drain thread — sys.exit in a thread only
    # kills the thread, so os._exit to force restart:unless-stopped redelivery.
    try:
        http.server.HTTPServer(("0.0.0.0", 8000), _make_health_handler(liveness, receiver_thread)).serve_forever()
    except Exception:
        logger.exception("health server died, exiting for restart")
        os._exit(1)

def main():
    liveness = Liveness()
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
    liveness.touch()   # successful connect counts as activity, so a slow first message isn't a false wedge
    rx = svc.create_persistent_message_receiver_builder().build(
        Queue.durable_exclusive_queue(os.environ["SWIM_QUEUE"]))
    rx.start()
    # drain runs in its own thread so /healthz can observe it dying independently of the health server itself.
    receiver_thread = threading.Thread(
        target=drain, args=(rx,),
        kwargs=dict(write=write_parquet, record=manifest.record_load, on_activity=liveness.touch),
        daemon=True)
    receiver_thread.start()
    threading.Thread(target=_serve_health, args=(liveness, receiver_thread), daemon=True).start()
    # main thread is now a watchdog: a 503 alone never restarts the container under plain compose, so
    # exiting on drain death re-arms restart:unless-stopped and lets the durable queue redeliver.
    receiver_thread.join()
    logger.error("drain thread died, exiting for restart")
    sys.exit(1)

if __name__ == "__main__":
    main()
