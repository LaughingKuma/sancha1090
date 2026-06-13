from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_iceberg_module_has_no_sqlcatalog_import():
    src = (REPO_ROOT / "include" / "iceberg.py").read_text()
    assert "SqlCatalog" not in src, (
        "include/iceberg.py still references SqlCatalog; Polaris is the only catalog after v2.4-5."
    )
