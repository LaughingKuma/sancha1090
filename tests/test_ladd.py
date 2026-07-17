# tests/test_ladd.py — SYNTHETIC fixtures only (fake N-numbers, no live network/ClickHouse). The N-numbers are
# derived from arbitrary hexids via the algorithm under test, so no real aircraft registration appears here.
import io
import zipfile
from datetime import date

import pytest

from include import ladd


# --- N-number <-> hex algorithm --------------------------------------------------------------------------------

def test_algorithm_known_structural_pairs():
    # Locks the readsb n_reg semantics (block start, digit rollover, block end).
    assert ladd.hex_to_n_number(0xA00001) == "N1"
    assert ladd.hex_to_n_number(0xA0025A) == "N10"
    assert ladd.hex_to_n_number(0xA18D50) == "N2"
    assert ladd.hex_to_n_number(0xADF7C7) == "N99999"
    assert ladd.n_number_to_hex("N1") == "a00001"
    assert ladd.n_number_to_hex("N99999") == "adf7c7"


def test_algorithm_round_trip_full_stride():
    # Strided sweep of the whole 0xA00001..0xADF7C7 block: every forward N-number inverts back to its hexid.
    for i in range(0, ladd._N_COUNT, 97):
        hexid = ladd._N_BASE + i
        reg = ladd.hex_to_n_number(hexid)
        assert reg is not None
        assert ladd.n_number_to_hex(reg) == format(hexid, "06x"), (hexid, reg)


@pytest.mark.parametrize("hexid", [
    0xA00001, 0xA00002, 0xA00019, 0xA0001A, 0xA00259, 0xA0025A, 0xA029D8, 0xA18D4F, 0xADF7C7,
])
def test_algorithm_round_trip_boundaries(hexid):
    assert ladd.n_number_to_hex(ladd.hex_to_n_number(hexid)) == format(hexid, "06x")


def test_algorithm_out_of_range_hex_is_none():
    assert ladd.hex_to_n_number(0xA00000) is None            # one below the block
    assert ladd.hex_to_n_number(0xADF7C8) is None            # one above the block


@pytest.mark.parametrize("junk", ["", "N", "N0", "N0ABC", "NABC", "9M-ABC", "N1234567", "N12I", "N12O", "JA8089"])
def test_algorithm_rejects_non_us_and_junk(junk):
    assert ladd.n_number_to_hex(junk) is None


def test_algorithm_excludes_i_and_o():
    # No forward N-number should ever contain I or O (the limited alphabet drops them).
    for i in range(0, ladd._N_COUNT, 613):
        reg = ladd.hex_to_n_number(ladd._N_BASE + i)
        assert "I" not in reg[1:] and "O" not in reg[1:]


# --- Defensive CSV parse ---------------------------------------------------------------------------------------

def test_parse_sniffs_registration_and_callsign_any_header_style():
    csv_bytes = b"Aircraft Registration,Call Sign,Owner\nN1AA,TEST01,Someone\n"
    rows = ladd.parse_ladd_csv(csv_bytes)
    assert rows == [{"registration": "N1AA", "callsign": "TEST01"}]


def test_parse_nnumber_header_and_bare_tail_gets_n_prefix():
    csv_bytes = b"N-NUMBER,extra\n1AA,ignored\n"
    assert ladd.parse_ladd_csv(csv_bytes) == [{"registration": "N1AA", "callsign": None}]


def test_parse_normalizes_case_and_quotes_and_dedups():
    csv_bytes = b"Registration\n' n1aa '\nN1AA\nN1AB\n"
    rows = ladd.parse_ladd_csv(csv_bytes)
    regs = [r["registration"] for r in rows]
    assert regs == ["N1AA", "N1AB"]          # trimmed + upper + de-quoted, and the duplicate collapsed


def test_parse_ignores_unknown_columns_and_blank_registration_rows():
    csv_bytes = b"foo,tail number,bar\nx,N1AA,y\nx,,y\n"
    assert ladd.parse_ladd_csv(csv_bytes) == [{"registration": "N1AA", "callsign": None}]


def test_parse_rejects_file_with_no_identity_column():
    with pytest.raises(ValueError, match="no recognizable registration column"):
        ladd.parse_ladd_csv(b"owner,city,state\nSomeone,Reno,NV\n")


def test_parse_rejects_empty_file():
    with pytest.raises(ValueError, match="empty"):
        ladd.parse_ladd_csv(b"")


def test_parse_rejects_header_only_no_valid_registrations():
    # A found reg column with no data rows must fail loud — an empty list would close every open interval.
    with pytest.raises(ValueError, match="no valid registrations"):
        ladd.parse_ladd_csv(b"Registration\n")
    with pytest.raises(ValueError, match="no valid registrations"):
        ladd.parse_ladd_csv(b"Registration\n' '\n,\n")


@pytest.mark.parametrize("first_line", ["identifier", "Call Sign"])
def test_parse_single_column_unrecognized_or_callsign_header_raises(first_line):
    # Format is dispatched by FILENAME now, not inferred from content shape — parse_ladd_csv is strictly the
    # headered parser, so any single-column CSV reaching it treats row 0 as a header. An unrecognized word
    # ("identifier") and a recognizable callsign header ("Call Sign") both hard-fail the same simple way.
    with pytest.raises(ValueError, match="no recognizable registration column"):
        ladd.parse_ladd_csv(f"{first_line}\nZZZ655\n".encode())


def test_parse_multi_column_no_reg_header_still_raises():
    with pytest.raises(ValueError, match="no recognizable registration column"):
        ladd.parse_ladd_csv(b"owner,city,state\nSomeone,Reno,NV\nOther,City,CA\n")


# --- Public headerless "Industry filter" parser (FAA-native format, dispatched by filename) ---------------------
# Invented identities only — N12345/N54321/2FAKE/ZZZ123/etc. are fabricated for these fixtures, never copied from
# a real FAA file (it's a privacy list).

def test_industry_filter_rejects_empty_file():
    with pytest.raises(ValueError, match="empty"):
        ladd.parse_ladd_industry_filter(b"")


def test_industry_filter_parses_mixed_single_column_crlf():
    csv_bytes = b"N12345\r\nZZZ123\r\n2FAKE\r\n"
    rows = ladd.parse_ladd_industry_filter(csv_bytes)
    by_reg = {r["registration"]: r for r in rows}
    assert set(by_reg) == {"N12345", "ZZZ123", "2FAKE"}
    # row 0 is ALWAYS data here — no header sniffing at all — so N12345 must survive
    assert by_reg["N12345"] == {"registration": "N12345", "callsign": "N12345"}


def test_industry_filter_n_shaped_value_keys_registration_with_matching_callsign():
    rows = ladd.parse_ladd_industry_filter(b"N54321\r\nZZZ999\r\n")
    assert {"registration": "N54321", "callsign": "N54321"} in rows


@pytest.mark.parametrize("callsign", ["2FAKE", "ZZZ123", "ZZZ655", "FFL123"])
def test_industry_filter_non_n_values_become_callsign_keyed_not_n_prefixed(callsign):
    rows = ladd.parse_ladd_industry_filter(f"{callsign}\r\nN99999\r\n".encode())
    by_reg = {r["registration"]: r for r in rows}
    assert callsign in by_reg                        # keyed by the bare value, never N-prefixed
    assert by_reg[callsign] == {"registration": callsign, "callsign": callsign}


def test_industry_filter_tolerates_blank_lines_and_dedups_case_and_whitespace():
    csv_bytes = b"\r\n' n12345 '\r\nN12345\r\n\r\nZZZ123\r\n"
    rows = ladd.parse_ladd_industry_filter(csv_bytes)
    regs = sorted(r["registration"] for r in rows)
    assert regs == ["N12345", "ZZZ123"]               # blank/quoted/lowercase dupe collapsed into N12345 once


def test_industry_filter_all_blank_raises():
    with pytest.raises(ValueError, match="no valid identities"):
        ladd.parse_ladd_industry_filter(b"\r\n\r\n' '\r\n")


def test_industry_filter_junk_only_raises():
    # An N/A-style placeholder or stray punctuation must never become a suppression identity.
    with pytest.raises(ValueError, match="no valid identities"):
        ladd.parse_ladd_industry_filter(b"N/A\r\n---\r\nUNKNOWN!\r\n")


def test_industry_filter_mixed_file_skips_junk_line_keeps_valid():
    csv_bytes = b"N12345\r\nN/A\r\nZZZ123\r\n"
    rows = ladd.parse_ladd_industry_filter(csv_bytes)
    regs = sorted(r["registration"] for r in rows)
    assert regs == ["N12345", "ZZZ123"]               # N/A dropped, valid identities kept


def test_industry_filter_multi_field_row_raises():
    # The native format is one identity per line, no commas at all — a multi-field row means the file
    # isn't what its filename claims. Must fail loud, not silently discard the extra field.
    with pytest.raises(ValueError, match="row 1 has 2 fields"):
        ladd.parse_ladd_industry_filter(b"N12345,EXTRA\r\nZZZ123\r\n")


def test_industry_filter_multi_field_row_raises_even_if_every_other_row_valid():
    with pytest.raises(ValueError, match="row 2 has 2 fields"):
        ladd.parse_ladd_industry_filter(b"N12345\r\nZZZ123,EXTRA\r\nN99999\r\n")


def test_industry_filter_lone_comma_row_raises():
    with pytest.raises(ValueError, match="row 1 has 2 fields"):
        ladd.parse_ladd_industry_filter(b",\r\nN12345\r\n")


def test_industry_filter_truly_blank_rows_still_tolerated():
    # A bare CRLF line is zero fields ([]), not a violation — distinct from a multi-field row.
    rows = ladd.parse_ladd_industry_filter(b"\r\nN12345\r\n\r\nZZZ123\r\n")
    assert sorted(r["registration"] for r in rows) == ["N12345", "ZZZ123"]


# --- FAA registry hex resolution -------------------------------------------------------------------------------

_MASTER = (
    "N-NUMBER,SERIAL NUMBER,MFR MDL CODE,MODE S CODE,MODE S CODE HEX\n"
    "1AA,0001,X,50000001,ABCDEF\n"       # deliberately NOT the algorithm's hex, to prove registry precedence
    "1AB,0002,X,50000002,\n"             # blank hex — skipped from the index
)


def test_build_registry_index_sniffs_columns():
    idx = ladd.build_registry_index(_MASTER)
    assert idx == {"N1AA": "abcdef"}     # blank-hex row dropped, hex lowercased, N-prefixed key


def test_resolve_prefers_registry_over_algorithm():
    idx = ladd.build_registry_index(_MASTER)
    assert ladd.resolve_icao24("N1AA", idx) == ("abcdef", True)     # registry wins, flagged registry-sourced
    assert ladd.resolve_icao24("N1", idx) == ("a00001", False)      # algorithm fallback, not registry-sourced
    assert ladd.resolve_icao24("9M-ABC", idx) == (None, False)      # neither resolves -> NULL, not registry


def test_build_registry_index_missing_columns_raises():
    with pytest.raises(ValueError, match="MASTER.txt missing"):
        ladd.build_registry_index("FOO,BAR\n1,2\n")


def test_download_registry_index_extracts_master_from_zip(monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("MASTER.txt", _MASTER)
    payload = buf.getvalue()

    class _Resp:
        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield payload

    class _Stream:
        def __enter__(self):
            return _Resp()

        def __exit__(self, *a):
            return False

    import httpx
    monkeypatch.setattr(httpx, "stream", lambda *_a, **_k: _Stream())
    assert ladd.download_registry_index() == {"N1AA": "abcdef"}


# --- SCD2 diff -------------------------------------------------------------------------------------------------

D1, D2 = date(2026, 6, 1), date(2026, 7, 1)


def test_scd2_opens_new_identities():
    new = [{"registration": "N1AA", "callsign": "C1", "icao24": "a00abc"}]
    plan = ladd.scd2_plan(D1, new, open_rows=[])
    assert plan["open"] == [("N1AA", "C1", "a00abc", D1, None)]
    assert plan["close"] == []


def test_scd2_closes_absent_and_keeps_present():
    new = [{"registration": "N1AA", "callsign": "C1", "icao24": "a00abc"}]
    open_rows = [
        {"registration": "N1AA", "callsign": "C1", "icao24": "a00abc", "valid_from": D1},
        {"registration": "N1AB", "callsign": None, "icao24": None, "valid_from": D1},
    ]
    plan = ladd.scd2_plan(D2, new, open_rows)
    assert plan["open"] == []                                        # N1AA already open -> untouched
    assert plan["close"] == [("N1AB", None, None, D1, D2)]           # N1AB absent -> closed at the new list date


def test_scd2_reapplying_same_list_is_a_noop():
    new = [{"registration": "N1AA", "callsign": "C1", "icao24": "a00abc"}]
    open_rows = [{"registration": "N1AA", "callsign": "C1", "icao24": "a00abc", "valid_from": D1}]
    plan = ladd.scd2_plan(D1, new, open_rows)
    assert plan["open"] == [] and plan["close"] == []


def test_scd2_identity_change_closes_old_and_opens_new():
    # Callsign changed on a continuing registration -> close the old identity window, open a new one.
    new = [{"registration": "N1AA", "callsign": "C2", "icao24": "a00abc"}]
    open_rows = [{"registration": "N1AA", "callsign": "C1", "icao24": "a00abc", "valid_from": D1}]
    plan = ladd.scd2_plan(D2, new, open_rows)
    assert plan["close"] == [("N1AA", "C1", "a00abc", D1, D2)]
    assert plan["open"] == [("N1AA", "C2", "a00abc", D2, None)]


def test_scd2_icao_change_reversions_identity():
    # A registry-sourced hex change is authoritative -> real re-version.
    new = [{"registration": "N1AA", "callsign": "C1", "icao24": "bbbbbb", "icao24_from_registry": True}]
    open_rows = [{"registration": "N1AA", "callsign": "C1", "icao24": "aaaaaa", "valid_from": D1}]
    plan = ladd.scd2_plan(D2, new, open_rows)
    assert plan["close"] == [("N1AA", "C1", "aaaaaa", D1, D2)]
    assert plan["open"] == [("N1AA", "C1", "bbbbbb", D2, None)]


def test_scd2_fallback_hex_differs_does_not_reversion():
    # Registry outage: the algorithm fallback resolves a DIFFERENT hex than the stored registry one -> no churn.
    new = [{"registration": "N1AA", "callsign": "C1", "icao24": "cccccc", "icao24_from_registry": False}]
    open_rows = [{"registration": "N1AA", "callsign": "C1", "icao24": "aaaaaa", "valid_from": D1}]
    plan = ladd.scd2_plan(D2, new, open_rows)
    assert plan["open"] == [] and plan["close"] == []


def test_scd2_null_icao_filled_by_fallback_reversions():
    # Stored hex is NULL (never resolved); filling it — even from the fallback — is a real change -> re-version.
    new = [{"registration": "N1AA", "callsign": "C1", "icao24": "dddddd", "icao24_from_registry": False}]
    open_rows = [{"registration": "N1AA", "callsign": "C1", "icao24": None, "valid_from": D1}]
    plan = ladd.scd2_plan(D2, new, open_rows)
    assert plan["close"] == [("N1AA", "C1", None, D1, D2)]
    assert plan["open"] == [("N1AA", "C1", "dddddd", D2, None)]


def test_scd2_callsign_change_reversions_regardless_of_hex_source():
    # A callsign change always re-versions, even when the hex is only fallback-sourced and unchanged.
    new = [{"registration": "N1AA", "callsign": "C2", "icao24": "aaaaaa", "icao24_from_registry": False}]
    open_rows = [{"registration": "N1AA", "callsign": "C1", "icao24": "aaaaaa", "valid_from": D1}]
    plan = ladd.scd2_plan(D2, new, open_rows)
    assert plan["close"] == [("N1AA", "C1", "aaaaaa", D1, D2)]
    assert plan["open"] == [("N1AA", "C2", "aaaaaa", D2, None)]


def test_scd2_callsign_reversion_during_outage_keeps_old_hex():
    # Callsign re-version while the registry is out: the differing fallback hex must NOT overwrite the stored one.
    new = [{"registration": "N1AA", "callsign": "C2", "icao24": "cccccc", "icao24_from_registry": False}]
    open_rows = [{"registration": "N1AA", "callsign": "C1", "icao24": "aaaaaa", "valid_from": D1}]
    plan = ladd.scd2_plan(D2, new, open_rows)
    assert plan["close"] == [("N1AA", "C1", "aaaaaa", D1, D2)]
    assert plan["open"] == [("N1AA", "C2", "aaaaaa", D2, None)]   # new callsign, OLD hex preserved


def test_scd2_identity_change_rerun_is_idempotent():
    # After the re-version, the open row carries the new identity; re-running the same list is a pure no-op.
    new = [{"registration": "N1AA", "callsign": "C2", "icao24": "a00abc"}]
    open_after = [{"registration": "N1AA", "callsign": "C2", "icao24": "a00abc", "valid_from": D2}]
    plan = ladd.scd2_plan(D2, new, open_after)
    assert plan["open"] == [] and plan["close"] == []


def test_check_mass_close_guard():
    # Trips once >half of the open intervals leave, but only above the small-dim floor.
    with pytest.raises(ValueError, match="likely corruption"):
        ladd.check_mass_close(n_open=20, n_removed=11)
    ladd.check_mass_close(n_open=20, n_removed=10)     # exactly half is allowed (not >half)
    ladd.check_mass_close(n_open=18, n_removed=18)     # under the 20-open floor -> never trips
    with pytest.raises(ValueError, match="likely corruption"):
        ladd.check_mass_close(n_open=100, n_removed=51)


def test_check_duplicate_dates_guard():
    ladd.check_duplicate_dates([{"list_date": D1, "key": "a"}, {"list_date": D2, "key": "b"}])   # distinct dates
    ladd.check_duplicate_dates([{"list_date": D1, "key": "a"}])                                    # single file
    key1 = "dims/ladd_raw/IndustryLADD-2026-06-01.csv"
    key2 = "dims/ladd_raw/LADD_Industry_Filter_CUI_SP_PRVCY_20260601.txt"
    with pytest.raises(ValueError) as excinfo:
        ladd.check_duplicate_dates([{"list_date": D1, "key": key1}, {"list_date": D1, "key": key2}])
    msg = str(excinfo.value)
    assert str(D1) in msg and key1 in msg and key2 in msg     # names the date and both colliding keys


# --- Freshness decision ----------------------------------------------------------------------------------------

def test_freshness_never_loaded_skips():
    assert ladd.freshness_decision(None, date(2026, 7, 9))[0] == "skip"


def test_freshness_boundary_ok_then_fail():
    today = date(2026, 7, 9)
    assert ladd.freshness_decision(date(2026, 6, 29), today, max_age_days=21)[0] == "ok"      # 10d old
    assert ladd.freshness_decision(today, today, max_age_days=0)[0] == "ok"                    # exactly at limit
    assert ladd.freshness_decision(date(2026, 6, 28), today, max_age_days=10)[0] == "fail"     # 11d > 10d


def test_freshness_default_sla_is_21_days():
    # Amit's SLA decision: 21d = 3 missed weekly FAA emails.
    today = date(2026, 7, 22)
    assert ladd.freshness_decision(date(2026, 7, 1), today) == ("ok", "newest LADD list is 21d old")
    assert ladd.freshness_decision(date(2026, 6, 30), today)[0] == "fail"                      # 22d > 21d


def test_parse_list_date_filename_convention():
    assert ladd.parse_list_date("IndustryLADD-2026-07-03.csv") == date(2026, 7, 3)
    assert ladd.parse_list_date("something-else.csv") is None
    assert ladd.parse_list_date("IndustryLADD-2026-13-40.csv") is None    # not a real calendar date


def test_parse_list_date_accepts_faa_native_filename():
    # FAA's own weekly distribution name, accepted as-received — no manual rename step needed.
    assert ladd.parse_list_date("LADD_Industry_Filter_CUI_SP_PRVCY_20260703.txt") == date(2026, 7, 3)
    assert ladd.parse_list_date("LADD_Industry_Filter_CUI_SP_PRVCY_20261399.txt") is None  # not a real date
    assert ladd.parse_list_date("some_other_file_20260703.txt") is None


def test_classify_ladd_filename_returns_format_tag():
    # The filename IS the format signal the loader dispatches on — pin the (date, tag) contract directly.
    assert ladd._classify_ladd_filename("IndustryLADD-2026-07-03.csv") == (date(2026, 7, 3), "legacy")
    assert ladd._classify_ladd_filename("LADD_Industry_Filter_CUI_SP_PRVCY_20260703.txt") == (date(2026, 7, 3), "native")
    assert ladd._classify_ladd_filename("something-else.csv") == (None, None)


# --- End-to-end orchestration over fakes (RMT-FINAL semantics emulated) ----------------------------------------

class _FakeResult:
    def __init__(self, rows):
        self.result_rows = rows


class _FakeClient:
    # In-memory stand-in for a clickhouse_connect client: emulates dim_ladd RMT FINAL collapse by
    # (registration, valid_from) keeping the newest insert, plus the ladd_pulls bookkeeping queries.
    def __init__(self):
        self._ladd = []      # (registration, callsign, icao24, valid_from, valid_to, version)
        self._pulls = []     # (list_date, object_uri)
        self._seq = 0
        self.closed = False

    def insert(self, table, data, **_kwargs):
        if table == ladd._DIM_LADD:
            for row in data:
                self._seq += 1
                self._ladd.append((*row, self._seq))
        elif table == ladd._LADD_PULLS:
            self._pulls.extend((row[0], row[1]) for row in data)
        else:
            raise AssertionError(f"unexpected insert target {table}")

    def _open_intervals(self):
        latest = {}
        for reg, cs, hx, vf, vt, ver in self._ladd:
            key = (reg, vf)
            if key not in latest or ver > latest[key][-1]:
                latest[key] = (reg, cs, hx, vf, vt, ver)
        return [(reg, cs, hx, vf) for (reg, cs, hx, vf, vt, _ver) in latest.values() if vt is None]

    def query(self, sql):
        if "DISTINCT list_date" in sql:
            return _FakeResult([(d,) for d in {p[0] for p in self._pulls}])
        if "FROM dim.dim_ladd FINAL" in sql:
            return _FakeResult(self._open_intervals())
        if "count(), max(list_date)" in sql:
            dates = [p[0] for p in self._pulls]
            return _FakeResult([(len(dates), max(dates) if dates else None)])
        raise AssertionError(f"unexpected query {sql}")

    def close(self):
        self.closed = True


class _FakeFs:
    def __init__(self, files):
        self._files = files      # {key: bytes}

    def find(self, base):
        return [k for k in self._files if k.startswith(base)]

    def cat_file(self, key):
        return self._files[key]


def _wire(monkeypatch, client, files, registry=None):
    monkeypatch.setattr("include.clickhouse.ch_client", lambda: client)
    monkeypatch.setattr("include.s3_helpers.get_s3fs", lambda: _FakeFs(files))
    monkeypatch.setattr("include.s3_helpers.get_bucket", lambda: "sancha1090")
    monkeypatch.setattr(ladd, "download_registry_index", lambda: registry or {})


def test_list_ladd_objects_tags_format():
    fs = _FakeFs({
        "sancha1090/dims/ladd_raw/IndustryLADD-2026-06-01.csv": b"x",
        "sancha1090/dims/ladd_raw/LADD_Industry_Filter_CUI_SP_PRVCY_20260701.txt": b"x",
        "sancha1090/dims/ladd_raw/unrelated.txt": b"x",       # no date match -> excluded entirely
    })
    objs = ladd._list_ladd_objects(fs, "sancha1090")
    by_date = {o["list_date"]: o["format"] for o in objs}
    assert by_date == {date(2026, 6, 1): "legacy", date(2026, 7, 1): "native"}


def test_load_noop_when_prefix_empty(monkeypatch):
    client = _FakeClient()
    _wire(monkeypatch, client, files={})
    assert ladd.load_ladd_pulls_to_ch() == {"files": 0, "opened": 0, "closed": 0, "ok": True}


def test_load_applies_scd2_across_two_pulls_and_is_idempotent(monkeypatch):
    key1 = "sancha1090/dims/ladd_raw/IndustryLADD-2026-06-01.csv"
    key2 = "sancha1090/dims/ladd_raw/IndustryLADD-2026-07-01.csv"
    files = {
        key1: b"Registration\nN1AA\nN1AB\n",
        key2: b"Registration\nN1AA\nN1AC\n",     # AB leaves, AC joins, AA stays
    }
    client = _FakeClient()
    _wire(monkeypatch, client, files, registry={"N1AA": "aaaaaa"})

    first = ladd.load_ladd_pulls_to_ch()
    assert first == {"files": 2, "opened": 3, "closed": 1, "ok": True}

    # Current open set: AA (registry hex) and AC; AB is closed.
    opens = {r[0]: r for r in client._open_intervals()}
    assert set(opens) == {"N1AA", "N1AC"}
    assert opens["N1AA"][2] == "aaaaaa"                 # registry-resolved icao24
    assert opens["N1AC"][2] == ladd.n_number_to_hex("N1AC")   # algorithm-resolved icao24

    # Re-run: both list dates already seen -> pure no-op, nothing re-inserted.
    ladd_rows_before = len(client._ladd)
    second = ladd.load_ladd_pulls_to_ch()
    assert second == {"files": 0, "opened": 0, "closed": 0, "ok": True}
    assert len(client._ladd) == ladd_rows_before
    assert client.closed is True


def test_load_registry_outage_does_not_churn(monkeypatch):
    # First pull resolves N1AA via the registry to a non-algorithm hex; a later pull during a registry outage
    # (download returns {}) must keep that hex, not re-version to the algorithm fallback.
    key1 = "sancha1090/dims/ladd_raw/IndustryLADD-2026-06-01.csv"
    key2 = "sancha1090/dims/ladd_raw/IndustryLADD-2026-07-01.csv"
    files = {key1: b"Registration\nN1AA\n"}
    client = _FakeClient()
    _wire(monkeypatch, client, files, registry={"N1AA": "aaaaaa"})
    ladd.load_ladd_pulls_to_ch()
    assert {r[0]: r[2] for r in client._open_intervals()}["N1AA"] == "aaaaaa"
    assert ladd.n_number_to_hex("N1AA") != "aaaaaa"                  # the fallback really would differ

    files[key2] = b"Registration\nN1AA\n"
    _wire(monkeypatch, client, files, registry={})                  # registry download failed → {} fallback-only
    res = ladd.load_ladd_pulls_to_ch()
    assert res == {"files": 1, "opened": 0, "closed": 0, "ok": True}   # no spurious open/close
    assert {r[0]: r[2] for r in client._open_intervals()}["N1AA"] == "aaaaaa"   # hex unchanged, no churn


def test_load_fails_loud_on_malformed_file(monkeypatch):
    key = "sancha1090/dims/ladd_raw/IndustryLADD-2026-06-01.csv"
    client = _FakeClient()
    _wire(monkeypatch, client, files={key: b"owner,city\nSomeone,Reno\n"})
    with pytest.raises(ValueError, match="no recognizable registration column"):
        ladd.load_ladd_pulls_to_ch()


def test_load_dispatches_native_filename_to_headerless_parser(monkeypatch):
    key = "sancha1090/dims/ladd_raw/LADD_Industry_Filter_CUI_SP_PRVCY_20260601.txt"
    client = _FakeClient()
    _wire(monkeypatch, client, files={key: b"N12345\r\nZZZ123\r\n"})
    res = ladd.load_ladd_pulls_to_ch()
    assert res == {"files": 1, "opened": 2, "closed": 0, "ok": True}
    assert {r[0] for r in client._open_intervals()} == {"N12345", "ZZZ123"}


def test_load_legacy_filename_with_headerless_content_fails_loud(monkeypatch):
    # A legacy-named object always goes through the strict header parser — headerless content (no
    # recognizable registration column) must fail loud, not silently succeed via shape inference.
    key = "sancha1090/dims/ladd_raw/IndustryLADD-2026-06-01.csv"
    client = _FakeClient()
    _wire(monkeypatch, client, files={key: b"N12345\r\nZZZ123\r\n"})
    with pytest.raises(ValueError, match="no recognizable registration column"):
        ladd.load_ladd_pulls_to_ch()


def test_load_native_filename_with_multi_field_row_fails_loud_nothing_applied(monkeypatch):
    key = "sancha1090/dims/ladd_raw/LADD_Industry_Filter_CUI_SP_PRVCY_20260601.txt"
    client = _FakeClient()
    _wire(monkeypatch, client, files={key: b"N12345,EXTRA\r\nZZZ123\r\n"})
    with pytest.raises(ValueError, match="row 1 has 2 fields"):
        ladd.load_ladd_pulls_to_ch()
    assert client._ladd == [] and client._pulls == []   # nothing applied


def test_load_fails_loud_on_duplicate_list_date(monkeypatch):
    # A renamed legacy file and the FAA-native file landing for the SAME date must never both apply.
    key1 = "sancha1090/dims/ladd_raw/IndustryLADD-2026-06-01.csv"
    key2 = "sancha1090/dims/ladd_raw/LADD_Industry_Filter_CUI_SP_PRVCY_20260601.txt"
    client = _FakeClient()
    _wire(monkeypatch, client, files={key1: b"Registration\nN1AA\n", key2: b"Registration\nN1AB\n"})
    with pytest.raises(ValueError, match="multiple files for the same list_date"):
        ladd.load_ladd_pulls_to_ch()
    assert client._ladd == [] and client._pulls == []   # nothing applied — fails loud before any _apply_list


def test_freshness_ch_reads_newest(monkeypatch):
    client = _FakeClient()
    client._pulls = [(date(2026, 6, 1), "u1"), (date(2026, 7, 1), "u2")]
    monkeypatch.setattr("include.clickhouse.ch_client", lambda: client)
    status, _msg = ladd.ladd_freshness_ch(today=date(2026, 7, 9))
    assert status == "ok"
    status, _msg = ladd.ladd_freshness_ch(today=date(2026, 9, 1))
    assert status == "fail"
