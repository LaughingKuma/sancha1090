import collections
import os

BURST = float(os.environ.get("LIVEMAP_RATE_LIMIT_BURST", "10"))
REFILL_PER_SEC = float(os.environ.get("LIVEMAP_RATE_LIMIT_REFILL_PER_SEC", "1"))
# Cap on tracked IP buckets; fully-refilled idle ones are swept lazily on access past this cap.
BUCKETS_MAX = int(os.environ.get("LIVEMAP_RATE_BUCKETS_MAX", "65536"))

# value shape: ip -> (tokens, last_seen_ts)
_buckets: collections.OrderedDict = collections.OrderedDict()


def client_ip(request) -> str:
    # Only Cloudflare can reach the public instance (tunnel-only ingress), so CF-Connecting-IP is the real
    # client; fall back to the socket peer for a direct/local hit.
    cf = request.headers.get("CF-Connecting-IP")
    if cf:
        return cf.strip()
    return request.client.host if request.client else "unknown"


def allow(key, now, buckets, burst=BURST, refill=REFILL_PER_SEC, max_buckets=BUCKETS_MAX) -> bool:
    tokens, last = buckets.pop(key, (burst, now))   # pop+reinsert = move-to-end, so dict order IS recency
    tokens = min(burst, tokens + (now - last) * refill)
    allowed = tokens >= 1.0
    buckets[key] = (tokens - 1.0 if allowed else tokens, now)
    # Hard LRU ceiling, O(1): evicting the least-recently-seen bucket only re-grants that IP a fresh burst —
    # acceptable for a backstop limiter, unlike unbounded growth under the very load it exists to absorb.
    while len(buckets) > max_buckets:
        del buckets[next(iter(buckets))]
    return allowed


def allow_request(key, now) -> bool:
    return allow(key, now, _buckets)
