from __future__ import annotations

RETENTION = "7d"


# One statement per list entry: the Trino DBAPI runs a single statement per
# execute, so SQLExecuteQueryOperator iterates the list rather than splitting.
def maintenance_statements(ns: str, tables: list[str], op: str, retention: str = RETENTION) -> list[str]:
    if op == "optimize":
        return [f"ALTER TABLE iceberg.{ns}.{t} EXECUTE optimize" for t in tables]
    if op == "expire":
        return [
            f"ALTER TABLE iceberg.{ns}.{t} EXECUTE expire_snapshots(retention_threshold => '{retention}')"
            for t in tables
        ]
    if op == "orphans":
        return [
            f"ALTER TABLE iceberg.{ns}.{t} EXECUTE remove_orphan_files(retention_threshold => '{retention}')"
            for t in tables
        ]
    raise ValueError(f"unsupported op {op!r} for ns {ns!r}")
