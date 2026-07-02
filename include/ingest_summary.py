from __future__ import annotations

from typing import Any


# A landed object is recorded in the manifest inside the fetch task; if every fetch failed (e.g. the manifest DB
# was unreachable) nothing lands, yet the all_done summary task still succeeds and would report the run green —
# masking a total ingest outage. Treat "attempted but nothing landed" as a wholesale failure so the DAG reds.
def all_landings_failed(attempted: int, with_data: int) -> bool:
    return attempted > 0 and with_data == 0


def raise_if_all_landings_failed(summary: dict[str, Any], *, entity: str, label: str) -> None:
    attempted = summary[f"{entity}_attempted"]
    if all_landings_failed(attempted, summary[f"{entity}_with_data"]):
        raise RuntimeError(
            f"{label}: all {attempted} {entity} failed to land data — every fetch task raised "
            f"(see the fetch task logs); not emitting the landed asset"
        )
