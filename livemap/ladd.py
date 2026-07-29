import json
import os

# LADD suppression: the live surfaces only need "listed right now" — the OPEN intervals. The mart's
# window-aware is_ladd covers history. dim_ladd is RMT(_version) so FINAL for current SCD2 state.
SUPPRESS_QUERY = "SELECT icao24, callsign FROM dim.dim_ladd FINAL WHERE valid_to IS NULL"
# Last-good suppression sets on disk: a restart mid-CH-cold-start reseeds from this instead of failing open
# (None). The container FS survives restarts, so the fail-open window collapses to first-ever boot/recreate.
CACHE_PATH = os.environ.get("LIVEMAP_LADD_CACHE_PATH", "/tmp/ladd_suppress_cache.json")
# LADD open-interval identities (hex + normalized callsign) refreshed from CH every ~15 min; suppressed on the
# live surfaces. EMPTY_SUPPRESS is the sentinel for a real, loaded, currently-empty list.
EMPTY_SUPPRESS: dict = {"hex": frozenset(), "callsign": frozenset()}


def write_cache(suppress, path) -> None:
    # Atomic last-good write (temp + os.replace) so a crash mid-write never leaves a half-JSON the boot seed trusts.
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump({"hex": sorted(suppress["hex"]), "callsign": sorted(suppress["callsign"])}, fh)
    os.replace(tmp, path)


def read_cache(path):
    # Boot seed from last-good so a CH-cold-start restart resumes dim filtering instead of failing open;
    # conservative-stale is the right direction (the list mostly grows). Missing/corrupt -> None (never-loaded).
    try:
        with open(path) as fh:
            d = json.load(fh)
        return {"hex": frozenset(d["hex"]), "callsign": frozenset(d["callsign"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def cache_suppress(suppress, path) -> None:
    # Best-effort: a read-only/full FS must never break the live refresh — the in-memory set stays authoritative.
    try:
        write_cache(suppress, path)
    except Exception as exc:
        print(f"livemap ladd suppress cache write skipped: {exc}", flush=True)


def is_suppressed(hex_, callsign, mv_is_ladd, suppress) -> bool:
    # Pure: True if the row is LADD-listed by the MV's db_flags bit OR by an open-interval hex/callsign identity.
    # suppress None = the dim set never loaded; only the MV belt applies here (callers fail /track closed).
    if mv_is_ladd:
        return True
    if suppress is None:
        return False
    h = (hex_ or "").strip().lower()
    if h and h in suppress["hex"]:
        return True
    c = (callsign or "").strip().upper()
    return bool(c and c in suppress["callsign"])


def should_refresh(state, tick, refresh_ticks) -> bool:
    # None = never loaded: retry every poll tick until the first success closes the fail-open window (a host
    # reboot boots livemap before CH is healthy). Once loaded (even empty) revert to the ~15-min cadence.
    return state is None or tick % refresh_ticks == 0


def track_belt_suppressed(hex_, now, mv_ladd_hexes, ttl_s) -> bool:
    # Pure: /track can't see the MV is_ladd bit (mv_track_positions carries no dbFlags), so honor the live belt
    # _fetch maintains — a hex dropped for mv_is_ladd within the last ttl_s.
    ts = mv_ladd_hexes.get((hex_ or "").strip().lower())
    return ts is not None and (now - ts) <= ttl_s


def filter_flights(rows, suppress) -> list:
    # Per-row callsign belt at serve time (around the CH cache): the hex is already cleared upstream, so drop only
    # rows on a currently-listed callsign — a listing added after the flight escapes the window-scoped mart is_ladd.
    return [r for r in rows
            if not is_suppressed(None, r.get("callsign"), mv_is_ladd=False, suppress=suppress)]


def missing_table(exc, is_unknown_table) -> bool:
    # Pre-deploy, dim.dim_ladd doesn't exist yet — expected cold-start, not an outage. Scope the UNKNOWN_TABLE
    # signal to dim_ladd so any *other* missing relation still surfaces as a real error.
    return "dim_ladd" in str(exc).lower() and is_unknown_table(exc)


def fetch_suppress(ch_client) -> dict:
    client = ch_client()
    try:
        res = client.query(SUPPRESS_QUERY)
    finally:
        client.close()
    hexes = {i.strip().lower() for i, _ in res.result_rows if i}
    calls = {c.strip().upper() for _, c in res.result_rows if c}
    return {"hex": frozenset(hexes), "callsign": frozenset(calls)}


def refresh_suppress(current, fetch, cache_path, is_missing_table):
    # Graduated fail-closed: a real refresh error keeps current state (None stays None -> surfaces fail closed);
    # a MISSING dim_ladd is pre-deploy — a successful empty load: None -> empty, belt-only filtering resumes.
    try:
        fresh = fetch()
    except Exception as exc:
        if is_missing_table(exc):
            print(f"livemap ladd suppress: dim_ladd absent (pre-deploy) -> empty load: {exc}", flush=True)
            return EMPTY_SUPPRESS
        print(f"livemap ladd suppress refresh kept current: {type(exc).__name__}: {exc}", flush=True)
        return current
    cache_suppress(fresh, cache_path)  # persist last-good so a cold-start restart reseeds instead of failing open
    return fresh
