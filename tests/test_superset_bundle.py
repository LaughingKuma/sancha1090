from __future__ import annotations

from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
DATASETS_DIR = REPO_ROOT / "superset" / "assets" / "datasets" / "analytics"
DATABASES_DIR = REPO_ROOT / "superset" / "assets" / "databases"

# superset/ isn't mounted into the airflow runtime image — skip there, like the
# other tests skip when their backing service/dir is absent.
pytestmark = pytest.mark.skipif(
    not DATASETS_DIR.exists(), reason=f"superset assets not present at {DATASETS_DIR}"
)

CH_UUID = "01234567-0000-0000-0000-000000000004"

# P5 CH cutover: every analytics dataset moves off trino-iceberg onto the clickhouse
# connection. ClickHouse has no multi-level catalog (catalog -> null); the four aggregates/
# marts live in gold_ch, the lone silver fact in silver_ch.
EXPECTED_SCHEMA = {
    "agg_country_traffic_2.yaml": "gold_ch",
    "agg_hourly_traffic_3.yaml": "gold_ch",
    "anomalies_2.yaml": "gold_ch",
    "agg_airport_daily.yaml": "gold_ch",
    "fact_state_snapshots_5.yaml": "silver_ch",
}

# main_dttm_col must survive the re-point untouched — it is the column Superset's dialect
# DATEADDs against. Values are UNCHANGED from the v2.7 Trino re-point (agg_airport_daily added).
EXPECTED_MAIN_DTTM = {
    "agg_country_traffic_2.yaml": "snapshot_ts",
    "agg_hourly_traffic_3.yaml": "snapshot_hour",
    "anomalies_2.yaml": "snapshot_time",
    "agg_airport_daily.yaml": "traffic_day",
    "fact_state_snapshots_5.yaml": "snapshot_time",
}


def _load(name: str) -> dict:
    return yaml.safe_load((DATASETS_DIR / name).read_text())


@pytest.mark.parametrize("name, schema", EXPECTED_SCHEMA.items())
def test_all_v2_datasets_point_at_clickhouse(name, schema):
    doc = _load(name)
    assert doc["database_uuid"] == CH_UUID
    assert doc["catalog"] is None
    assert doc["schema"] == schema


@pytest.mark.parametrize("name, col", EXPECTED_MAIN_DTTM.items())
def test_main_dttm_cols_unchanged_post_repoint(name, col):
    assert _load(name)["main_dttm_col"] == col


def test_clickhouse_connection_asset_present():
    doc = yaml.safe_load((DATABASES_DIR / "clickhouse.yaml").read_text())
    assert doc["uuid"] == CH_UUID
    assert doc["sqlalchemy_uri"].startswith("clickhousedb://")
    # v6.0 retired Trino/Iceberg — the legacy lakehouse connection asset is removed from the bundle.
    assert not (DATABASES_DIR / "trino-iceberg.yaml").exists()
