#!/usr/bin/env python3
# Pins the workbench wire schemas: `--write` regenerates tests/fixtures/workbench/schema_snapshot.json but
# refuses a schema change under an unchanged contract — an envelope change must bump livemap/wb_contract.json.
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests" / "fixtures" / "workbench" / "schema_snapshot.json"
sys.path.insert(0, str(ROOT / "tests"))
from _livemap_loader import load_livemap_module  # noqa: E402

BUMP_MSG = "schemas changed but the contract did not go up — bump livemap/wb_contract.json, then --write again"
STALE_MSG = "schema snapshot is stale — bump livemap/wb_contract.json, then scripts/wb_schema_snapshot.py --write"


def generate() -> dict:
    models = load_livemap_module("wb_models.py")
    return {"contract": json.loads((ROOT / "livemap" / "wb_contract.json").read_text())["contract"],
            "schemas": {m.__name__: m.model_json_schema() for m in models.ENVELOPES.values()}}


def refusal(current: dict, committed) -> str | None:
    # a schema change needs a HIGHER contract: equality is the handshake test, so a recycled number would pass it
    if committed and committed["schemas"] != current["schemas"] and current["contract"] <= committed["contract"]:
        return BUMP_MSG
    return None


def main(argv) -> int:
    current = generate()
    committed = json.loads(SNAPSHOT.read_text()) if SNAPSHOT.exists() else None
    if "--write" in argv:
        msg = refusal(current, committed)
        if msg:
            print(msg, file=sys.stderr)
            return 1
        SNAPSHOT.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")
        print(f"wrote {SNAPSHOT}")
        return 0
    if current == committed:
        print("schema snapshot up to date")
        return 0
    print(STALE_MSG, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
