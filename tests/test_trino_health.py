from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest


def test_trino_coordinator_is_ready():
    # Skips on the host because `trino-coordinator` only resolves on the
    # compose network; runs for real inside the airflow/worker containers
    # and in CI where the network is shared.
    url = os.environ.get("TRINO_URL", "http://trino-coordinator:8080") + "/v1/info"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, OSError) as exc:
        pytest.skip(f"trino coordinator not reachable: {exc}")

    assert payload["starting"] is False, payload
    assert payload["environment"] == "opensky", payload
