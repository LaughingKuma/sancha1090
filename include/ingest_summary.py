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


# Shared body of the ingest summarize tasks: an all_done upstream yields None entries for raised
# fetches, so `succeeded` counts returned results and `with_data` the subset that landed a file.
def summarize_fetch_results(
    results: list[Any], *, plural: str, singular: str, banner: str, label: str
) -> dict[str, Any]:
    results = list(results)
    succeeded = sum(1 for r in results if r is not None)
    total_rows = sum(r["rows"] for r in results if r is not None)
    with_data = sum(1 for r in results if r is not None and r.get("uri"))
    summary = {
        "total_rows": total_rows,
        f"{plural}_with_data": with_data,
        f"{plural}_succeeded": succeeded,
        f"{plural}_attempted": len(results),
        f"per_{singular}": results,
    }
    print(f"{banner}: {summary}")
    raise_if_all_fetches_raised(summary, entity=plural, label=label)
    return summary
