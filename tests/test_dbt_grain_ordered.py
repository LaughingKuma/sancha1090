from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
MODELS = REPO / "dbt" / "sancha1090" / "models"


def _ordered_grains() -> list[tuple[str, list[str]]]:
    found = []
    for yml in sorted(MODELS.rglob("*.yml")):
        doc = yaml.safe_load(yml.read_text()) or {}
        for model in doc.get("models", []) or []:
            for test in model.get("tests", []) or []:
                if isinstance(test, dict) and "grain" in test:
                    args = test["grain"].get("arguments", test["grain"])
                    if args.get("ordered"):
                        found.append((model["name"], list(args["column_names"])))
    return found


def _order_by(model_name: str) -> list[str]:
    sql = next(MODELS.rglob(f"{model_name}.sql")).read_text()
    match = re.search(r"order_by\s*=\s*\[([^\]]*)\]", sql)
    assert match, f"{model_name}: an ordered grain test needs a list-form order_by in the model config"
    return [c.strip().strip("'\"") for c in match.group(1).split(",") if c.strip()]


def test_ordered_grain_columns_are_a_prefix_of_the_model_sort_key():
    # ordered=true is only cheap while the window ORDER BY is read in sorting-key order (read-in-order ->
    # MergingSortedTransform); any other order silently becomes a full sort of the table on every tick.
    grains = _ordered_grains()
    assert grains, "no ordered grain tests found — fct_flight_path's should be here"
    for model, cols in grains:
        assert _order_by(model)[: len(cols)] == cols, (model, cols)
