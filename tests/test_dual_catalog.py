from __future__ import annotations

import pytest


def _load_both_or_skip():
    try:
        from include import iceberg as ib
        from include import iceberg_rest as rest

        sql_table = ib.get_catalog().load_table(ib.QUALIFIED)
        sql_snap = sql_table.current_snapshot().snapshot_id
        sql_meta = sql_table.metadata_location
        pol_snap = rest.load_polaris_snapshot()
    except Exception as exc:
        pytest.skip(f"polaris/sqlcatalog not reachable from this host: {exc}")
    return ib, rest, sql_table, sql_snap, sql_meta, pol_snap


def test_polaris_snapshot_id_matches_sqlcatalog_after_tableize():
    # v2.3 acceptance: tableize_states' sync_polaris task closes the drift
    # logged in v2.x-v2.2-shipped.md § "Deferred to v2.3".
    _, _, _, sql_snap, _, pol_snap = _load_both_or_skip()
    assert pol_snap == sql_snap, (
        f"Polaris snapshot {pol_snap} differs from SqlCatalog {sql_snap}; "
        f"sync_polaris likely hasn't run after the latest tableize commit."
    )


def test_polaris_pointer_idempotent():
    _, rest, _, _, sql_meta, _ = _load_both_or_skip()
    first = rest.sync_polaris_pointer(sql_meta)
    second = rest.sync_polaris_pointer(sql_meta)
    assert second["action"] == "noop", (
        f"second sync should be no-op when pointer matches; got {second['action']}"
    )
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["metadata_location"] == sql_meta
