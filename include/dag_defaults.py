from __future__ import annotations

from datetime import timedelta


# Exponential-backoff quartet is pinned here so the three ingest lanes stay field-identical; anything
# that doesn't fit these parameters keeps its literal dict in the DAG file rather than stretching this.
def default_args(retries: int = 1, delay_min: int = 2, backoff: bool = False) -> dict:
    args = {
        "owner": "amit",
        "retries": retries,
        "retry_delay": timedelta(minutes=delay_min),
    }
    if backoff:
        args["retry_exponential_backoff"] = True
        args["max_retry_delay"] = timedelta(minutes=10)
    return args
