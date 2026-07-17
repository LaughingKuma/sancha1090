from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from datetime import date, datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# --- US N-number <-> ICAO 24-bit hex (deterministic mixed-radix; ported from the readsb registrations.js n_reg) --

# 24-letter suffix alphabet: A-Z minus I and O.
_LIMITED = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_N_BASE = 0xA00001
_N_COUNT = 915399               # valid US block is 0xA00001..0xADF7C7
_L1, _L2, _L3, _L4 = 101711, 10111, 951, 35   # per-digit strides
_SUFFIX_MAX = 600               # highest index _n_letters encodes ("", 24 singles, 24*24 pairs)


def _n_letter(rem: int) -> str:
    # 0 -> "", 1..24 -> a single suffix letter
    if rem == 0:
        return ""
    return _LIMITED[rem - 1]


def _n_letters(rem: int) -> str:
    # 0 -> "", 1..600 -> a one- or two-letter suffix (Na, NaA..NaZ, NaAA..NaZZ)
    if rem == 0:
        return ""
    rem -= 1
    return _LIMITED[rem // 25] + _n_letter(rem % 25)


def _n_letter_inv(suffix: str) -> Optional[int]:
    if suffix == "":
        return 0
    if len(suffix) == 1:
        k = _LIMITED.find(suffix)
        return k + 1 if k >= 0 else None
    return None


def _n_letters_inv(suffix: str) -> Optional[int]:
    if suffix == "":
        return 0
    if len(suffix) == 1:
        k = _LIMITED.find(suffix)
        return 1 + 25 * k if k >= 0 else None
    if len(suffix) == 2:
        k, j = _LIMITED.find(suffix[0]), _LIMITED.find(suffix[1])
        return 2 + 25 * k + j if k >= 0 and j >= 0 else None
    return None


def hex_to_n_number(hexid: int) -> Optional[str]:
    # Forward map (icao24 int -> N-number). Kept alongside the inverse so tests can round-trip.
    offset = hexid - _N_BASE
    if offset < 0 or offset >= _N_COUNT:
        return None
    reg = "N" + str(offset // _L1 + 1)
    offset %= _L1
    if offset <= _SUFFIX_MAX:
        return reg + _n_letters(offset)
    offset -= _SUFFIX_MAX + 1
    reg += str(offset // _L2)
    offset %= _L2
    if offset <= _SUFFIX_MAX:
        return reg + _n_letters(offset)
    offset -= _SUFFIX_MAX + 1
    reg += str(offset // _L3)
    offset %= _L3
    if offset <= _SUFFIX_MAX:
        return reg + _n_letters(offset)
    offset -= _SUFFIX_MAX + 1
    reg += str(offset // _L4)
    offset %= _L4
    if offset <= 24:
        return reg + _n_letter(offset)
    return reg + str(offset - 25)


def _finish(offset: int) -> Optional[str]:
    if offset < 0 or offset >= _N_COUNT:
        return None
    return format(_N_BASE + offset, "06x")


def n_number_to_hex(nnumber: str) -> Optional[str]:
    # Inverse map (N-number -> lowercase icao24), or None when the string is not a US N-number in the block.
    s = (nnumber or "").strip().upper()
    if len(s) < 2 or s[0] != "N" or not s[1].isdigit() or s[1] == "0":
        return None
    body = s[1:]
    i = 0
    while i < len(body) and body[i].isdigit():
        i += 1
    digits, tail = body[:i], body[i:]
    if any(c not in _LIMITED for c in tail):
        return None
    nd = len(digits)
    off = (int(digits[0]) - 1) * _L1
    if nd == 1:
        s1 = _n_letters_inv(tail)
        return _finish(off + s1) if s1 is not None else None
    off += _SUFFIX_MAX + 1 + int(digits[1]) * _L2
    if nd == 2:
        s2 = _n_letters_inv(tail)
        return _finish(off + s2) if s2 is not None else None
    off += _SUFFIX_MAX + 1 + int(digits[2]) * _L3
    if nd == 3:
        s3 = _n_letters_inv(tail)
        return _finish(off + s3) if s3 is not None else None
    off += _SUFFIX_MAX + 1 + int(digits[3]) * _L4
    if nd == 4:
        s4 = _n_letter_inv(tail)
        return _finish(off + s4) if s4 is not None else None
    if nd == 5 and tail == "":
        return _finish(off + 25 + int(digits[4]))
    return None


# --- Defensive CSV parse (header-sniff registration + optional callsign; unknown columns ignored) --------------

_REG_ALIASES = {"nnumber", "registration", "registrationnumber", "regnumber", "reg",
                "tail", "tailnumber", "aircraftregistration", "aircraftregistrationnumber"}
_CALLSIGN_ALIASES = {"callsign", "flightid", "flightident", "flightnumber", "ident", "telephony"}


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def _pick_column(headers: list[str], aliases: set[str], contains: tuple[str, ...] = ()) -> Optional[str]:
    normed = [(_norm_header(h), h) for h in headers]
    for n, h in normed:
        if n in aliases:
            return h
    for n, h in normed:
        if any(tok in n for tok in contains):
            return h
    return None


def sniff_columns(headers: list[str]) -> tuple[Optional[str], Optional[str]]:
    reg = _pick_column(headers, _REG_ALIASES, contains=("nnumber", "registration", "tailnumber"))
    callsign = _pick_column(headers, _CALLSIGN_ALIASES, contains=("callsign",))
    return reg, callsign


def _normalize_registration(raw: Optional[str]) -> Optional[str]:
    r = re.sub(r"\s+", "", (raw or "").strip().strip("'\"").upper())
    if not r:
        return None
    # N-prefix a bare US tail (spreadsheet exports drop the N); leave already-lettered values as-is.
    return "N" + r if r[0].isdigit() else r


def _normalize_callsign(raw: Optional[str]) -> Optional[str]:
    c = (raw or "").strip().upper()
    return c or None


_BARE_IDENTITY_RE = re.compile(r"^[A-Z0-9]+$")


def _normalize_bare(raw: Optional[str]) -> Optional[str]:
    # Same trim/de-quote/uppercase as _normalize_registration but no N-prefixing — a bare callsign like "2FAKE"
    # must never be mistaken for a digit-leading US tail. \s also eats stray non-breaking spaces (\xa0).
    v = re.sub(r"\s+", "", (raw or "").strip().strip("'\"").upper())
    return v if v and _BARE_IDENTITY_RE.match(v) else None


def _parse_ladd_industry_filter(rows: list[list[str]]) -> list[dict]:
    # N-shaped and bare-callsign lines get the identical shape: callsign=value keeps hex-OR-callsign
    # suppression alive for both (registration here is only the SCD2 key); which lines are N-shaped only
    # matters later, in resolve_icao24.
    out: dict[str, dict] = {}
    for i, row in enumerate(rows, start=1):
        if len(row) > 1:
            # The FAA native format is one identity per line, no commas at all — a multi-field row (even with
            # only blank extra fields) means the file isn't what its filename claims; fail loud rather than
            # silently discard fields. A blank line (len 0) is still tolerated padding, not a violation.
            raise ValueError(f"LADD file row {i} has {len(row)} fields (expected 1): {row!r}")
        val = _normalize_bare(row[0]) if row else None
        if not val:
            continue
        out.setdefault(val, {"registration": val, "callsign": val})
    return list(out.values())


def parse_ladd_industry_filter(data: bytes) -> list[dict]:
    # Public bytes-level entry point for the FAA-native "Industry filter" format: headerless, single column,
    # CRLF, one N-number or bare callsign per line. Row 0 is ALWAYS data — the loader dispatches to this
    # function purely on the object's filename (the FAA-native convention), never by sniffing file shape.
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError("LADD file is empty")
    out = _parse_ladd_industry_filter(rows)
    if not out:
        raise ValueError("LADD file looks like a headerless identity list but has no valid identities")
    return out


def parse_ladd_csv(data: bytes) -> list[dict]:
    # Strictly the headered parser — the legacy IndustryLADD- filename convention only. No shape inference:
    # a single-column file reaching here (whatever its first line says) is treated as headered, since the
    # loader only ever routes here for objects whose NAME matched the legacy convention.
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError("LADD file is empty")
    headers = rows[0]
    reg_col, cs_col = sniff_columns(headers)
    if reg_col is None:
        raise ValueError(f"LADD file has no recognizable registration column; headers={headers!r}")
    reg_i = headers.index(reg_col)
    cs_i = headers.index(cs_col) if cs_col is not None else None
    out: dict[str, dict] = {}
    for row in rows[1:]:
        if reg_i >= len(row):
            continue
        reg = _normalize_registration(row[reg_i])
        if not reg:
            continue
        cs = _normalize_callsign(row[cs_i]) if (cs_i is not None and cs_i < len(row)) else None
        # First occurrence wins; the identity set is keyed on registration.
        out.setdefault(reg, {"registration": reg, "callsign": cs})
    if not out:
        # A found reg column that yields zero valid registrations means a garbage / mis-encoded file; never
        # return [] — the caller applies the list as truth and an empty list would close every open interval.
        raise ValueError(f"LADD file has a registration column ({reg_col!r}) but no valid registrations parsed")
    return list(out.values())


# --- FAA registry hex resolution (authoritative MASTER.txt first, algorithm fallback) --------------------------

_REGISTRY_URL = "https://registry.faa.gov/database/ReleasableAircraft.zip"


def build_registry_index(master_text: str) -> dict[str, str]:
    rows = csv.reader(io.StringIO(master_text))
    try:
        header = next(rows)
    except StopIteration:
        return {}
    normed = [_norm_header(h) for h in header]
    n_i = normed.index("nnumber") if "nnumber" in normed else None
    hex_i = normed.index("modescodehex") if "modescodehex" in normed else None
    if n_i is None or hex_i is None:
        raise ValueError("FAA MASTER.txt missing N-NUMBER or MODE S CODE HEX column")
    index: dict[str, str] = {}
    for row in rows:
        if n_i >= len(row) or hex_i >= len(row):
            continue
        nn, hx = row[n_i].strip().upper(), row[hex_i].strip().lower()
        if nn and hx:
            index["N" + nn] = hx
    return index


def resolve_icao24(registration: str, registry_index: dict[str, str]) -> tuple[Optional[str], bool]:
    # Returns (icao24, from_registry). from_registry lets the SCD2 diff ignore hex churn from a transient registry
    # outage — the algorithm fallback can differ from a prior registry-resolved hex without being a real change.
    hit = registry_index.get(registration)
    if hit:
        return hit, True
    return n_number_to_hex(registration), False


def _find_master(names: list[str]) -> str:
    for n in names:
        if n.upper().endswith("MASTER.TXT"):
            return n
    raise ValueError(f"MASTER.txt not found in FAA registry zip; entries={names!r}")


def download_registry_index(url: str = _REGISTRY_URL, timeout: float = 300.0) -> dict[str, str]:
    import httpx

    buf = io.BytesIO()
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as r:
        r.raise_for_status()
        for chunk in r.iter_bytes():
            buf.write(chunk)
    buf.seek(0)
    with zipfile.ZipFile(buf) as zf:
        with zf.open(_find_master(zf.namelist())) as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace").read()
    return build_registry_index(text)


# --- SCD2 diff over registration identity (pure) --------------------------------------------------------------

def scd2_plan(list_date: date, new_rows: list[dict], open_rows: list[dict]) -> dict:
    # Rows are 5-tuples (registration, callsign, icao24, valid_from, valid_to) ready for insert.
    # A close re-inserts the existing open row's key (registration, valid_from) with valid_to filled so the
    # ReplacingMergeTree merge keeps the closed version.
    new_by = {r["registration"]: r for r in new_rows}
    open_by = {r["registration"]: r for r in open_rows}
    opens, closes = [], []
    for reg, r in new_by.items():
        o = open_by.get(reg)
        if o is None:
            opens.append((reg, r.get("callsign"), r.get("icao24"), list_date, None))
            continue
        # A differing hex is a real identity change only when the new value is registry-sourced (authoritative) or
        # the stored one is NULL (first resolution) — an algorithm-fallback hex differing from a prior registry hex
        # is transient-outage churn, not a re-version. Callsign changes always re-version.
        icao_changed = r.get("icao24") != o.get("icao24") and (
            r.get("icao24_from_registry") or o.get("icao24") is None
        )
        if r.get("callsign") != o.get("callsign") or icao_changed:
            # A non-authoritative differing hex never overwrites a known one, even on a callsign-triggered re-version.
            open_icao = o.get("icao24") if (
                not r.get("icao24_from_registry") and o.get("icao24") is not None
                and r.get("icao24") != o.get("icao24")
            ) else r.get("icao24")
            # Continuing registration whose identity changed: close the old identity's window (preserving the
            # data-protection it already covers) and open a fresh interval carrying the current identity.
            closes.append((reg, o.get("callsign"), o.get("icao24"), o["valid_from"], list_date))
            opens.append((reg, r.get("callsign"), open_icao, list_date, None))
    closes.extend(
        (o["registration"], o.get("callsign"), o.get("icao24"), o["valid_from"], list_date)
        for reg, o in open_by.items() if reg not in new_by
    )
    return {"open": opens, "close": closes}


def check_mass_close(n_open: int, n_removed: int, min_open: int = 20) -> None:
    # A weekly privacy list never legitimately sheds most of its members at once — a plan that removes >half of
    # the open intervals means partial corruption or a wrongly-sniffed column. The floor avoids tripping during
    # early small-dim adoption. Pure so the load path can gate on it before touching ClickHouse.
    if n_open >= min_open and n_removed * 2 > n_open:
        raise ValueError(
            f"LADD plan would remove {n_removed} of {n_open} open intervals (>half) — refusing as likely corruption"
        )


def check_duplicate_dates(pending: list[dict]) -> None:
    # One authoritative file per list date: the legacy IndustryLADD- name and FAA's native filename could both
    # land for the same date, and applying both in one run would produce order-dependent same-day interval
    # churn. Pure so the load path can gate on it before touching ClickHouse, same as check_mass_close.
    by_date: dict[date, list[str]] = {}
    for o in pending:
        by_date.setdefault(o["list_date"], []).append(o["key"])
    dupes = {d: keys for d, keys in by_date.items() if len(keys) > 1}
    if dupes:
        detail = "; ".join(f"{d}: {keys!r}" for d, keys in sorted(dupes.items()))
        raise ValueError(f"LADD pull has multiple files for the same list_date — refusing ({detail})")


# --- Freshness decision (pure) --------------------------------------------------------------------------------

def freshness_decision(newest_list_date: Optional[date], today: date, max_age_days: int = 21) -> tuple[str, str]:
    # 21d = 3 missed weekly FAA emails; sized to tolerate a vacation/out-of-cycle gap without failing on the
    # very first miss, while still catching a genuinely broken channel well before it goes stale for a month.
    if newest_list_date is None:
        return "skip", "dim.dim_ladd has never been loaded — no LADD pull yet"
    age = (today - newest_list_date).days
    if age > max_age_days:
        return "fail", f"newest LADD list is {age}d old (over {max_age_days}d) — pull overdue"
    return "ok", f"newest LADD list is {age}d old"


# --- ClickHouse / Garage orchestration (thin; lazy imports so the pure core stays dependency-free) ------------

_DIM_LADD = "dim.dim_ladd"
_LADD_PULLS = "dim.ladd_pulls"
_LADD_PREFIX = "dims/ladd_raw"
_LADD_FILE_RE = re.compile(r"IndustryLADD-(\d{4}-\d{2}-\d{2})\.csv$")
# FAA distributes the weekly Industry filter pull under this exact native name; accepting it as-received
# removes a manual rename step per pull.
_LADD_FAA_NATIVE_RE = re.compile(r"LADD_Industry_Filter_CUI_SP_PRVCY_(\d{8})\.txt$")
_LADD_COLS = ["registration", "callsign", "icao24", "valid_from", "valid_to"]


def _classify_ladd_filename(name: str) -> tuple[Optional[date], Optional[str]]:
    # The Garage prefix listing only ever admits these two known conventions — the filename itself IS the
    # format signal, so the loader dispatches the parser off this tag instead of inferring shape from content.
    name = name or ""
    for pattern, fmt, tag in (
        (_LADD_FILE_RE, "%Y-%m-%d", "legacy"),
        (_LADD_FAA_NATIVE_RE, "%Y%m%d", "native"),
    ):
        m = pattern.search(name)
        if not m:
            continue
        try:
            return datetime.strptime(m.group(1), fmt).date(), tag
        except ValueError:
            return None, None
    return None, None


def parse_list_date(name: str) -> Optional[date]:
    return _classify_ladd_filename(name)[0]


def _list_ladd_objects(fs, bucket: str) -> list[dict]:
    try:
        keys = fs.find(f"{bucket}/{_LADD_PREFIX}")
    except FileNotFoundError:
        return []
    out = []
    for k in keys:
        d, fmt = _classify_ladd_filename(k.rsplit("/", 1)[-1])
        if d is not None:
            out.append({"list_date": d, "uri": f"s3://{k}", "key": k, "format": fmt})
    out.sort(key=lambda r: r["list_date"])
    return out


def _seen_list_dates(client) -> set:
    return {r[0] for r in client.query(f"SELECT DISTINCT list_date FROM {_LADD_PULLS}").result_rows}


def _read_open_intervals(client) -> list[dict]:
    rows = client.query(
        f"SELECT registration, callsign, icao24, valid_from FROM {_DIM_LADD} FINAL WHERE valid_to IS NULL"
    ).result_rows
    return [{"registration": r[0], "callsign": r[1], "icao24": r[2], "valid_from": r[3]} for r in rows]


def _apply_list(client, list_date: date, new_rows: list[dict], object_uri: str) -> dict:
    open_intervals = _read_open_intervals(client)
    plan = scd2_plan(list_date, new_rows, open_intervals)
    # Count only registrations that leave the open set entirely (identity re-versions reopen, so they don't count).
    opened_regs = {o[0] for o in plan["open"]}
    removed = sum(1 for c in plan["close"] if c[0] not in opened_regs)
    check_mass_close(len(open_intervals), removed)
    inserts = plan["open"] + plan["close"]
    if inserts:
        client.insert(_DIM_LADD, [list(t) for t in inserts], column_names=_LADD_COLS)
    client.insert(_LADD_PULLS, [[list_date, object_uri]], column_names=["list_date", "object_uri"])
    return {"opened": len(plan["open"]), "closed": len(plan["close"])}


def load_ladd_pulls_to_ch() -> dict:
    from include.clickhouse import ch_client
    from include.s3_helpers import get_bucket, get_s3fs

    fs = get_s3fs()
    objects = _list_ladd_objects(fs, get_bucket())
    if not objects:
        return {"files": 0, "opened": 0, "closed": 0, "ok": True}

    client = ch_client()
    try:
        seen = _seen_list_dates(client)
        pending = [o for o in objects if o["list_date"] not in seen]
        if not pending:
            return {"files": 0, "opened": 0, "closed": 0, "ok": True}
        check_duplicate_dates(pending)   # fail loud before any _apply_list — nothing applied on a collision
        # Registry download is best-effort: the deterministic algorithm still resolves standard US tails.
        try:
            registry = download_registry_index()
        except Exception:
            log.warning("FAA registry download failed — using the N-number algorithm only", exc_info=True)
            registry = {}
        opened = closed = files = 0
        for o in pending:
            # Dispatch by the filename convention that was already recorded at listing time — never by
            # re-inspecting file content/shape.
            parser = parse_ladd_industry_filter if o["format"] == "native" else parse_ladd_csv
            rows = parser(fs.cat_file(o["key"]))   # raises on a no-identity/malformed file → fail loud
            for r in rows:
                r["icao24"], r["icao24_from_registry"] = resolve_icao24(r["registration"], registry)
            res = _apply_list(client, o["list_date"], rows, o["uri"])
            opened += res["opened"]
            closed += res["closed"]
            files += 1
        return {"files": files, "opened": opened, "closed": closed, "ok": True}
    finally:
        client.close()


def ladd_freshness_ch(today: Optional[date] = None, max_age_days: int = 21) -> tuple[str, str]:
    from include.clickhouse import ch_client

    client = ch_client()
    try:
        cnt, newest = client.query(f"SELECT count(), max(list_date) FROM {_LADD_PULLS}").result_rows[0]
    finally:
        client.close()
    today = today or datetime.now(timezone.utc).date()
    return freshness_decision(newest if cnt else None, today, max_age_days)
