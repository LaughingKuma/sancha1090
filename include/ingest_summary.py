from __future__ import annotations

from typing import Any


# A fetch task that succeeds but legitimately finds no data (e.g. an empty region) is a designed success path
# and must not red the run, so this keys on `succeeded` (tasks that returned at all), not `with_data`.
def all_fetches_raised(attempted: int, succeeded: int) -> bool:
    return attempted > 0 and succeeded == 0


def raise_if_all_fetches_raised(summary: dict[str, Any], *, entity: str, label: str) -> None:
    attempted = summary[f"{entity}_attempted"]
    if all_fetches_raised(attempted, summary[f"{entity}_succeeded"]):
        raise RuntimeError(
            f"{label}: all {attempted} {entity} fetch tasks raised (see the fetch task logs); "
            f"not emitting the landed asset"
        )
