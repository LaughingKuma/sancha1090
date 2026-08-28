from __future__ import annotations

import pendulum

from airflow.sdk import dag, task

from include.dag_defaults import default_args


def _truthy(raw) -> bool:
    # Strict truthy parse: JSON-string "false" must not read as True (bool("false") is True).
    return raw is True or str(raw).strip().lower() in ("1", "true", "yes", "on")


def _scoped_names(conf: dict, known: set[str]) -> list[str] | None:
    # Scope is MANDATORY here: an accidental unscoped rerun DROP/CREATEs every MV while the live insert lanes
    # keep writing, and the rows landing in that gap are lost forever. {"all": true} opts into that deliberately.
    from include.ch_incremental_mvs import validate_names

    all_flag = _truthy(conf.get("all", False))
    if all_flag and "names" in conf:
        # Same rule as the CLI: a conf carrying both scopes is contradictory, and silently letting "all" win
        # would broaden a deliberately narrow run into the miss-window-carrying redeploy of every MV.
        raise ValueError('ch_incremental_mvs_init: conf sets BOTH {"all": true} and "names" — '
                         f'pass exactly one scope; got names={conf["names"]!r}')
    if all_flag:
        return None
    if "names" not in conf:
        raise ValueError('ch_incremental_mvs_init: conf must scope the run — {"names": [...]} '
                         f'or {{"all": true}} for an unscoped redeploy; known: {sorted(known)}')
    return validate_names(conf["names"], known)


@dag(
    dag_id="ch_incremental_mvs_init",
    description="One-time: create + seed the P4 self-maintaining ClickHouse AggregatingMergeTree MVs",
    start_date=pendulum.datetime(2026, 6, 20, tz="UTC"),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    default_args=default_args(),
    tags=["sancha1090", "clickhouse", "manual"],
)
def ch_incremental_mvs_init():

    @task
    def create_and_seed() -> dict:
        # Idempotent; conf {"reseed": true} forces a destructive truncate + re-seed. Scope is required:
        # {"names": [...]} for specific specs, {"all": true} for the whole (miss-window-carrying) redeploy.
        from airflow.sdk import get_current_context

        from include.ch_incremental_mvs import SPECS, apply

        conf = get_current_context()["dag_run"].conf or {}
        return apply(reseed=_truthy(conf.get("reseed", False)), names=_scoped_names(conf, set(SPECS)))

    create_and_seed()


ch_incremental_mvs_init()
