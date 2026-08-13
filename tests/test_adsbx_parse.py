from __future__ import annotations

import gzip
import json

import pytest

import include.adsbx_parse as adsbx_parse
from include.adsbx_parse import parse_basic_ac_db


def _gz(lines: list[bytes]) -> bytes:
    return gzip.compress(b"\n".join(lines) + b"\n")


def _obj(i: int) -> bytes:
    return json.dumps({"icao": f"{i:06x}", "reg": f"REG{i}"}).encode()


def test_clean_ndjson_all_kept():
    lines = [_obj(i) for i in range(5)]
    df, total, rejected = parse_basic_ac_db(_gz(lines), max_drop_rate=0.5)
    assert (total, rejected, df.height) == (5, 0, 5)


def test_double_escaped_quote_syntax_error_rejected():
    # 2026-08-09..12 incident line (icao 7c3c56): \\" is two literal backslashes then an
    # unescaped quote, closing the string early -> invalid JSON, not a schema mismatch.
    bad = (
        b'{"icao":"7c3c56","reg":"VH-L7C","ownop":"UAB \\\\"AVIAAM B02\\\\", '
        b'SKYTRANS AUSTRALIA PTY LTD","faa_pia":false}'
    )
    lines = [_obj(i) for i in range(5)] + [bad]
    df, total, rejected = parse_basic_ac_db(_gz(lines), max_drop_rate=0.5)
    assert (total, rejected) == (6, 1)
    assert "7c3c56" not in df["icao"].to_list()


def test_null_line_bypass_class_exceeds_ceiling():
    # Reviewer's exact synthetic: 1,000 valid objects + 2 bare `null` lines. `null` parses as
    # valid JSON (not a dict) so it must be counted rejected, not silently absorbed by polars.
    lines = [_obj(i) for i in range(1000)] + [b"null", b"null"]
    with pytest.raises(ValueError, match="drop rate too high") as exc:
        parse_basic_ac_db(_gz(lines), max_drop_rate=0.001)
    rate = 2 / 1002
    assert rate > 0.001  # ~0.1996%, above the 0.1% ceiling
    assert f"{rate:.4%}" in str(exc.value)


def test_json_array_line_rejected_without_aborting_polars():
    # A top-level array is valid JSON but not a record; must be rejected pre-polars, never
    # handed to read_ndjson (which would abort the whole parse on a non-object line).
    lines = [_obj(i) for i in range(500)] + [b"[1, 2, 3]"]
    df, total, rejected = parse_basic_ac_db(_gz(lines), max_drop_rate=0.01)
    assert (total, rejected, df.height) == (501, 1, 500)


def test_boundary_rate_exactly_at_ceiling_passes():
    # Guard is strictly-greater: rejected/total == max_drop_rate must NOT raise.
    lines = [_obj(i) for i in range(999)] + [b"null"]
    df, total, rejected = parse_basic_ac_db(_gz(lines), max_drop_rate=0.001)
    assert (total, rejected, df.height) == (1000, 1, 999)


def test_boundary_one_reject_above_ceiling_raises():
    lines = [_obj(i) for i in range(998)] + [b"null", b"null"]
    with pytest.raises(ValueError, match="drop rate too high"):
        parse_basic_ac_db(_gz(lines), max_drop_rate=0.001)


def test_all_lines_bad_raises_before_polars():
    # None of these are valid dicts; garbage syntax alone must not reach pl.read_ndjson.
    lines = [b"null", b"[1]", b"not json at all"]
    with pytest.raises(ValueError, match="drop rate too high"):
        parse_basic_ac_db(_gz(lines), max_drop_rate=0.001)


def test_nan_constant_rejected_not_aborted():
    # Reviewer's exact repro: json.loads accepts non-standard NaN by default, so this line must
    # be counted rejected pre-polars, not reach read_ndjson and abort the whole batch.
    lines = [_obj(0), b'{"x": NaN}']
    df, total, rejected = parse_basic_ac_db(_gz(lines), max_drop_rate=0.5)
    assert (total, rejected, df.height) == (2, 1, 1)


def test_infinity_constants_rejected():
    lines = [_obj(0), b'{"x": Infinity}', b'{"x": -Infinity}']
    df, total, rejected = parse_basic_ac_db(_gz(lines), max_drop_rate=0.9)
    assert (total, rejected, df.height) == (3, 2, 1)


def _huge_int_line(icao: str) -> bytes:
    # Round 4: year's measured contract is str-only, so a raw JSON int here is now a type-contract
    # reject at scan time -- it never reaches polars, so this no longer exercises the net (below).
    return json.dumps({"icao": icao, "year": 10**400}).encode()


def _bad_surrogate_line(icao: str) -> bytes:
    # Round 4's net fixture: an unpaired low surrogate passes the type contract (still a str) but
    # reliably raises ComputeError from polars even under an explicit schema -- measured, not guessed.
    return json.dumps({"icao": icao, "reg": "X\udc00Y"}).encode()


def test_huge_int_year_rejected_by_type_contract_not_the_net(monkeypatch):
    # Confirms the net fixture swap above: a raw int under year is now caught before pl.read_ndjson
    # ever runs, same (total, rejected, height) shape as before but via the scan-time contract.
    def _fail_if_net_engaged(*_args, **_kwargs):
        pytest.fail("bisection net engaged — contract should have caught this")

    monkeypatch.setattr(adsbx_parse, "_bisect_bad_lines", _fail_if_net_engaged)
    lines = [_obj(i) for i in range(9)] + [_huge_int_line("dead00")]
    df, total, rejected = parse_basic_ac_db(_gz(lines), max_drop_rate=0.5)
    assert (total, rejected, df.height) == (10, 1, 9)
    assert "dead00" not in df["icao"].to_list()


def test_bisection_net_identifies_single_bad_line():
    lines = [_obj(i) for i in range(9)] + [_bad_surrogate_line("dead00")]
    df, total, rejected = parse_basic_ac_db(_gz(lines), max_drop_rate=0.5)
    assert (total, rejected, df.height) == (10, 1, 9)
    assert "dead00" not in df["icao"].to_list()


def test_bisection_net_identifies_bad_lines_in_opposite_halves():
    objs = [_obj(i) for i in range(10)]
    lines = objs[:2] + [_bad_surrogate_line("bad001")] + objs[2:7] + [_bad_surrogate_line("bad002")] + objs[7:]
    df, total, rejected = parse_basic_ac_db(_gz(lines), max_drop_rate=0.5)
    assert (total, rejected, df.height) == (12, 2, 10)
    kept_icaos = df["icao"].to_list()
    assert "bad001" not in kept_icaos
    assert "bad002" not in kept_icaos


def test_bisection_net_bisected_rejects_pushed_over_ceiling_raises():
    lines = [_obj(i) for i in range(4)] + [_bad_surrogate_line("bad003")]
    with pytest.raises(ValueError, match="drop rate too high"):
        parse_basic_ac_db(_gz(lines), max_drop_rate=0.1)


# (g) the post-parse height-disagreement guard only fires if polars silently drops/coalesces a
# row that survived our own isinstance(dict) filter — needs mocking pl.read_ndjson, skipped.


def test_empty_file_parses_cleanly_zero_total_no_exception():
    # Round 4: explicit schema makes pl.read_ndjson return an empty frame for empty input instead
    # of raising -- measured in-container; the old recursion guard stays defensive but unreached here.
    df, total, rejected = parse_basic_ac_db(gzip.compress(b""), 0.001)
    assert (total, rejected, df.height) == (0, 0, 0)


def test_reviewer_repro_consumed_field_combination_conflict_rejects_violator():
    # Reviewer's exact repro: faa_pia true vs [] across two records -- each parses alone (old code's
    # whole-file inference raised SchemaError only on the combination); scan-time contract catches it.
    lines = [_obj(i) for i in range(5)] + [
        json.dumps({"icao": "aaaaaa", "faa_pia": True}).encode(),
        json.dumps({"icao": "bbbbbb", "faa_pia": []}).encode(),
    ]
    df, total, rejected = parse_basic_ac_db(_gz(lines), max_drop_rate=0.5)
    assert (total, rejected, df.height) == (7, 1, 6)
    kept = df["icao"].to_list()
    assert "aaaaaa" in kept
    assert "bbbbbb" not in kept


def test_reviewer_repro_unconsumed_field_combination_conflict_both_survive():
    # Same conflict shape, but on a key outside the consumed set: projection drops it before polars
    # ever sees it, so both records survive -- the value projection adds over scan-only validation.
    lines = [_obj(i) for i in range(5)] + [
        json.dumps({"icao": "cccccc", "junkfield": "somestring"}).encode(),
        json.dumps({"icao": "dddddd", "junkfield": {"nested": 1}}).encode(),
    ]
    df, total, rejected = parse_basic_ac_db(_gz(lines), max_drop_rate=0.5)
    assert (total, rejected, df.height) == (7, 0, 7)
    kept = df["icao"].to_list()
    assert "cccccc" in kept
    assert "dddddd" in kept


def test_type_contract_string_flag_rejected():
    # faa_pia's measured contract is bool-only; a string "true" is a different JSON type entirely.
    lines = [_obj(i) for i in range(5)] + [json.dumps({"icao": "eeeeee", "faa_pia": "true"}).encode()]
    df, total, rejected = parse_basic_ac_db(_gz(lines), max_drop_rate=0.5)
    assert (total, rejected, df.height) == (6, 1, 5)
    assert "eeeeee" not in df["icao"].to_list()


def test_type_contract_dict_in_text_field_rejected():
    # reg's measured contract is str-only; a nested object is not coercible to Utf8.
    lines = [_obj(i) for i in range(5)] + [json.dumps({"icao": "ffffff", "reg": {"foo": "bar"}}).encode()]
    df, total, rejected = parse_basic_ac_db(_gz(lines), max_drop_rate=0.5)
    assert (total, rejected, df.height) == (6, 1, 5)
    assert "ffffff" not in df["icao"].to_list()


def test_unconsumed_junk_conflicting_shapes_harmless():
    # A third shape (list vs int, not str vs dict) on an unconsumed field, to distinguish this from
    # the reviewer-repro variant above: projection drops the field regardless of what it conflicts as.
    lines = [_obj(i) for i in range(5)] + [
        json.dumps({"icao": "111111", "otherjunk": [1, 2, 3]}).encode(),
        json.dumps({"icao": "222222", "otherjunk": 42}).encode(),
    ]
    df, total, rejected = parse_basic_ac_db(_gz(lines), max_drop_rate=0.5)
    assert (total, rejected, df.height) == (7, 0, 7)
    kept = df["icao"].to_list()
    assert "111111" in kept
    assert "222222" in kept
