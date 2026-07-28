import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("est_route", REPO_ROOT / "livemap" / "est_route.py")
er = importlib.util.module_from_spec(spec)
spec.loader.exec_module(er)

# measured live SWIM route strings (bronze.swim_flightdata, 2026-07-28)
DAL69 = ("KSEA.BANGR9.ARRIE..TOU..SEFIX..PRETY..4800N/14000W..4600N/15000W..4400N/16000W.."
         "4300N/17000W..4200N/18000W..4100N/17000E..4000N/16000E..EMRON.OTR7.ADNAP.Y807.LALID."
         "Y804.INUBO.Y50.ELNIS.Y564.IBENO.Y56.TOHME.Y54.TURFY.Y24.KOSHI.Y50.IGMON.A1.DRAKE.."
         "RCTP/1204")
JAL41 = ("RJTT./.PASRO.A590.POWAL..OPAKE..NATES.R338.MORLY.R338.MARCC..JUJXI..OMEKA.."
         "8100N/12000W..8200N/10000W..8200N/08000W..8200N/06000W..8000N/04000W..7400N/02000W.."
         "6600N/01000W..NALAN..ELBUS..AKOMU..VAMEB..NUGRA.NUGRA2H.EGLL")
VHHH_KSFO = ("VHHH..DALOL.V631.ENVAR.M750...4800N/17000W..4700N/15000W..VESPA..AMAKR.BDEGA4."
             "KSFO/1220")


def test_parse_dal69_crosses_antimeridian():
    assert er.parse_route_coords(DAL69) == [
        (48.0, -140.0), (46.0, -150.0), (44.0, -160.0), (43.0, -170.0),
        (42.0, -180.0), (41.0, 170.0), (40.0, 160.0),
    ]


def test_parse_jal41_polar():
    assert er.parse_route_coords(JAL41) == [
        (81.0, -120.0), (82.0, -100.0), (82.0, -80.0), (82.0, -60.0),
        (80.0, -40.0), (74.0, -20.0), (66.0, -10.0),
    ]


def test_parse_vhhh_ksfo_two_tokens():
    assert er.parse_route_coords(VHHH_KSFO) == [(48.0, -170.0), (47.0, -150.0)]


def test_parse_minutes_and_signs():
    assert er.parse_route_coords("AAA..3030S/06045W..BBB") == [(-30.5, -60.75)]
    assert er.parse_route_coords("AAA..3030S/06045E..BBB") == [(-30.5, 60.75)]


def test_parse_invalid_minutes_token_skipped():
    # 60+ minutes is not a coordinate: drop the token, keep the rest of the filed order
    assert er.parse_route_coords("4860N/14000W..4800N/14099W") == []
    assert er.parse_route_coords("4860N/14000W..4700N/15000W") == [(47.0, -150.0)]


def test_parse_no_tokens_and_empty():
    assert er.parse_route_coords("") == []
    assert er.parse_route_coords(None) == []
    assert er.parse_route_coords("RJTT.OTR7.ADNAP.Y807.LALID.RCTP") == []
    assert er.parse_route_coords("N4800/W14000") == []


def test_parse_preserves_filed_order():
    route = "AAA..4000N/16000E..4100N/17000E..4200N/18000W"
    assert er.parse_route_coords(route) == [(40.0, 160.0), (41.0, 170.0), (42.0, -180.0)]


def test_degree_bounds_are_composed_values():
    # 18000W (=180 deg, filed on real transpacific plans) survives; 90.98/180.98 do not (r1)
    s = "A..9000N/14000W..9059N/14000W..9100N/00000E..4200N/18000W..4200N/18059W..0000N/18100E..B"
    assert er.parse_route_coords(s) == [(90.0, -140.0), (42.0, -180.0)]
