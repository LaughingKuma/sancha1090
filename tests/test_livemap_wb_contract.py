import copy
import json
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import scripts.wb_schema_snapshot as snap
from test_livemap_workbench import _FakeUnknownTableError, _NEVER_500_CASES, _SHAPE_CASES

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests" / "e2e"))
import serve_fixture  # noqa: E402  — the e2e oracle's fixture-mode store, driven here in-process

CONTRACT_FILE = REPO_ROOT / "livemap" / "wb_contract.json"
WIRE_DTS = REPO_ROOT / "livemap" / "src" / "features" / "workbench" / "wire.d.ts"


@pytest.fixture(scope="module")
def wb_app(livemap):
    # install() rebinds the store's fetchers; the conftest app is already per-module, so that is its lifetime
    serve_fixture.install(livemap)
    return livemap


@pytest.fixture(scope="module")
def generated():
    return snap.generate()


def _validate(app, path, payload):
    return app.wb_models.ENVELOPES[path].model_validate(payload)


# ---- schema snapshot + contract ----

def test_wb_schema_snapshot(wb_app, generated):
    committed = json.loads(snap.SNAPSHOT.read_text())
    assert generated == committed, snap.STALE_MSG
    assert wb_app.WB_CONTRACT == json.loads(CONTRACT_FILE.read_text())["contract"] == committed["contract"]


def test_snapshot_write_refuses_schema_change_without_bump(generated, tmp_path, monkeypatch):
    committed = generated
    mutated = copy.deepcopy(committed)
    mutated["schemas"]["Instances"]["$defs"]["InstanceRow"]["properties"]["renamed"] = {"type": "string"}
    assert snap.refusal(mutated, committed) == snap.BUMP_MSG
    assert snap.refusal(mutated | {"contract": committed["contract"] + 1}, committed) is None
    assert snap.refusal(mutated | {"contract": committed["contract"] - 1}, committed) == snap.BUMP_MSG
    assert snap.refusal(committed, committed) is None
    # the CLI path: --write must leave the committed file untouched and exit non-zero
    target = tmp_path / "schema_snapshot.json"
    target.write_text(json.dumps(committed))
    monkeypatch.setattr(snap, "SNAPSHOT", target)
    monkeypatch.setattr(snap, "generate", lambda: mutated)
    assert snap.main(["--write"]) == 1
    assert json.loads(target.read_text()) == committed
    monkeypatch.setattr(snap, "generate", lambda: mutated | {"contract": committed["contract"] + 1})
    assert snap.main(["--write"]) == 0
    assert json.loads(target.read_text())["contract"] == committed["contract"] + 1


def test_wire_dts_declares_every_model(generated):
    # wire.d.ts is hand-mirrored (fields drift is PR6a's job); a missing or renamed envelope is caught now
    names = {n for s in generated["schemas"].values() for n in s.get("$defs", {})} | set(generated["schemas"])
    declared = set(re.findall(r"export (?:interface|type) (\w+)", WIRE_DTS.read_text()))
    assert names <= declared, names - declared


# ---- builder outputs (the fixture-mode store the e2e serves) + every degradation payload ----

_FETCH_CASES = {
    "airlines": [("", 50, 0), ("an", 5, 0)],
    "services": [("", "", 100, 0), ("All Nippon Airways", "", 100, 0)],
    "instances": [("", "", "", "", "", "", "", False, None, None, "day_desc", 50, 0),
                  ("", "", serve_fixture.INSTANCES[0].icao24, "", "", "HND-ITM", "", True, None, None,
                   "day_asc", 10, 0)],
    "summary": [(None, None)],
    "trends": [(dim, None, None, 20, 0) for dim in ("route", "airline", "airport")],
    "flags": [("", None, None, 50, 0), ("diversion", None, None, 50, 0)],
    "estimates": [(None, None)],
    "coverage": [(None, None)],
    "search": [("an", 20), ("ja", 20), ("HND", 20)],
}
_LIST_KEY = {"summary": "movers", "trends": "rank", "estimates": "headline", "coverage": "gap_bins",
             "search": "airports"}


@pytest.mark.parametrize("name", list(_FETCH_CASES))
def test_fixture_store_outputs_validate(wb_app, name):
    fetch = getattr(wb_app.wb_store, f"fetch_{name}")
    for args in _FETCH_CASES[name]:
        _validate(wb_app, f"/workbench/{name}", fetch(*args))
    # the base call must exercise rows, or the model check would pass vacuously on empties
    assert fetch(*_FETCH_CASES[name][0])[_LIST_KEY.get(name, name)]


@pytest.mark.parametrize("name", list(_SHAPE_CASES))
def test_shape_cases_validate(wb_app, name):
    path, payload = _SHAPE_CASES[name]
    _validate(wb_app, path.split("?")[0], payload)


@pytest.mark.parametrize("name", list(_NEVER_500_CASES))
def test_degradation_payloads_validate(wb_app, name):
    # the byte-exact never-500 payloads test_generic_exception_serves_empty_200 pins
    payload = _validate(wb_app, f"/workbench/{name}", _NEVER_500_CASES[name][1])
    if hasattr(payload, "complete"):
        assert payload.complete is False


def test_empty_builders_validate(wb_app):
    wb = wb_app.wb
    _validate(wb_app, "/workbench/summary", wb.empty_summary())
    _validate(wb_app, "/workbench/trends", wb.empty_trends("airport", 20, 0))
    _validate(wb_app, "/workbench/estimates", wb.empty_estimates())
    _validate(wb_app, "/workbench/coverage", wb.empty_coverage())


def test_store_mart_absent_payloads_validate(wb_app, monkeypatch):
    # class methods, because serve_fixture.install() shadows the instance's fetchers
    class _Client:
        def query(self, *_a, **_kw):
            raise _FakeUnknownTableError("Code: 60. DB::Exception: (UNKNOWN_TABLE)")

        def close(self):
            pass

    store = wb_app.wb_store
    monkeypatch.setattr(store, "client_factory", _Client)
    real = type(store)
    flags = _validate(wb_app, "/workbench/flags", real.fetch_flags(store, "", None, None, 50, 0))
    est = _validate(wb_app, "/workbench/estimates", real.fetch_estimates(store, None, None))
    cov = _validate(wb_app, "/workbench/coverage", real.fetch_coverage(store, None, None))
    inst = _validate(wb_app, "/workbench/instances",
                     real.fetch_instances(store, "", "", "", "", "", "", "", True, None, None, "day_desc", 50, 0))
    assert (flags.available, est.available, cov.available, inst.military_filter_available) == (False,) * 4


def test_no_tier_fallback_rows_validate(wb_app, monkeypatch):
    # the tier mart absent but recon present: populated rows with tier "unknown" and null gap/points
    from test_livemap_workbench import _LadderClient
    store = wb_app.wb_store
    monkeypatch.setattr(store, "client_factory", lambda: _LadderClient(missing=("fct_flight_recon_tier",)))
    real = type(store)
    inst = _validate(wb_app, "/workbench/instances",
                     real.fetch_instances(store, "", "", "", "", "", "", "", False, None, None, "day_desc", 50, 0))
    flags = _validate(wb_app, "/workbench/flags", real.fetch_flags(store, "", None, None, 50, 0))
    assert inst.instances and inst.instances[0].tier == "unknown"
    assert flags.flags and flags.flags[0].tier == "unknown"


def test_models_forbid_extra_and_reject_renames(wb_app):
    _path, payload = _SHAPE_CASES["instances"]
    row = payload["instances"][0] | {"registration_no": "JA123"}
    with pytest.raises(ValidationError):
        _validate(wb_app, "/workbench/instances", payload | {"instances": [row]})


# ---- OpenAPI + /healthz ----

def test_openapi_envelopes_both_ways(livemap_public, wb_app):
    public_paths = TestClient(livemap_public.app).get("/openapi.json").json()["paths"]
    assert not [p for p in public_paths if p == "/features" or p.startswith("/workbench")]
    private = TestClient(wb_app.app).get("/openapi.json").json()
    served = {p for p in private["paths"] if p == "/features" or p.startswith("/workbench")}
    assert served == set(wb_app.wb_models.ENVELOPES)  # a route outside the table would drift unmodelled
    for path, model in wb_app.wb_models.ENVELOPES.items():
        schema = private["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{model.__name__}"}
        assert private["components"]["schemas"][model.__name__]["additionalProperties"] is False


@pytest.mark.parametrize("mode", ["private", "public"])
def test_healthz_static_build(mode, wb_app, livemap_public, tmp_path, monkeypatch):
    mod = wb_app if mode == "private" else livemap_public
    monkeypatch.setattr(mod, "BUILD_JSON_PATH", str(tmp_path / "missing.json"))
    assert TestClient(mod.app).get("/healthz").json()["static_build"] is None
    built = {"sha": "abc123", "contract": mod.WB_CONTRACT, "built_at": "2026-08-17T00:00:00Z"}
    path = tmp_path / "build.json"
    path.write_text(json.dumps(built))
    monkeypatch.setattr(mod, "BUILD_JSON_PATH", str(path))
    assert TestClient(mod.app).get("/healthz").json()["static_build"] == built | {"matches": True}
    path.write_text(json.dumps(built | {"contract": mod.WB_CONTRACT + 1}))
    body = TestClient(mod.app).get("/healthz").json()["static_build"]
    assert body["matches"] is False and body["contract"] == mod.WB_CONTRACT + 1
    for wrong in ("[]", "null", "3", "not json"):
        path.write_text(wrong)
        r = TestClient(mod.app).get("/healthz")  # 503 = stale snapshot, fine; a wrong-shaped stamp must never 500
        assert r.status_code in (200, 503) and r.json()["static_build"] is None
