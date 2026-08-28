#!/usr/bin/env bash
# Read-only cost/duty instrument for transform_marts (#188/#189). system.query_log has a 14-day TTL
# (clickhouse/config.d/system_logs_retention.xml), so any trend must be captured within that window.
#
#   scripts/ch_transform_cost.sh [HOURS]   # default 6
set -euo pipefail

HOURS="${1:-6}"
# Whitelist before it ever reaches SQL — reject anything that isn't a bare positive integer.
if ! [[ "$HOURS" =~ ^[1-9][0-9]*$ ]]; then
  echo "invalid HOURS: '$HOURS' (expected a positive integer)" >&2
  exit 2
fi

PROJ="${COMPOSE_PROJECT_NAME:-sancha1090}"
CH="${PROJ}-clickhouse-1"
PG="${PROJ}-postgres-airflow-1"

ch() { docker exec -i "$CH" clickhouse-client --format PrettyCompactMonoBlock; }
pg() { docker exec -i "$PG" psql -U airflow -d airflow -v ON_ERROR_STOP=1; }

echo "== ClickHouse: per-model dbt cost, last ${HOURS}h (query_log, node_id from the leading query comment) =="
read -r -d '' SQL_MODEL <<'EOF' || true
SELECT extract(query, '"node_id": "model\\.sancha1090\\.([a-z0-9_]+)"') AS model, count() AS runs,
  round(avg(ProfileEvents['OSCPUVirtualTimeMicroseconds'])/1e6,1) AS cpu_s,
  round(avg(query_duration_ms)/1000,1) AS wall_s, round(max(memory_usage)/1e9,2) AS peak_gb,
  round(avg(read_rows)/1e6,1) AS read_mrows,
  countIf(ProfileEvents['ExternalAggregationWritePart'] > 0) AS spilled_runs
FROM system.query_log
WHERE event_time > now() - INTERVAL __HOURS__ HOUR AND type='QueryFinish' AND query_kind='Insert'
  AND query LIKE '%"node_id": "model.sancha1090.%'
GROUP BY model ORDER BY sum(ProfileEvents['OSCPUVirtualTimeMicroseconds']) DESC
EOF
echo "${SQL_MODEL//__HOURS__/$HOURS}" | ch

echo
echo "== ClickHouse: all dbt lanes total, last ${HOURS}h (sums over the window, never a sum of per-model averages — no DAG/invocation id in the comment, so per-DAG attribution is impossible) =="
read -r -d '' SQL_TOTAL <<'EOF' || true
SELECT 'all dbt lanes' AS lane, count() AS runs,
  round(sum(ProfileEvents['OSCPUVirtualTimeMicroseconds'])/1e6,1) AS total_cpu_s,
  round(sum(ProfileEvents['OSCPUVirtualTimeMicroseconds'])/1e6 / (__HOURS__*3600),2) AS avg_cores
FROM system.query_log
WHERE event_time > now() - INTERVAL __HOURS__ HOUR AND type='QueryFinish' AND query_kind='Insert'
  AND query LIKE '%"node_id": "model.sancha1090.%'
EOF
echo "${SQL_TOTAL//__HOURS__/$HOURS}" | ch

echo
echo "== ClickHouse: MEMORY_LIMIT_EXCEEDED (code 241) dbt query failures, last ${HOURS}h =="
read -r -d '' SQL_MEM <<'EOF' || true
SELECT count() AS memory_failures
FROM system.query_log
WHERE event_time > now() - INTERVAL __HOURS__ HOUR
  AND type IN ('ExceptionBeforeStart','ExceptionWhileProcessing')
  AND exception_code = 241
  AND query LIKE '%"node_id": "model.sancha1090.%'
EOF
echo "${SQL_MEM//__HOURS__/$HOURS}" | ch

echo
echo "== Airflow Postgres (authoritative duty measure): transform_marts run_type x state, last ${HOURS}h =="
# Overlap filter, not just start_date > win_start: a run already executing when the window opened
# (end_date NULL, still running) must still be counted, or a stuck run reads as clean silence.
pg <<EOF
-- state='queued' rows have no start_date yet; without the OR arm a queued backlog reads as clean silence.
SELECT run_type, state, count(*) AS n
FROM dag_run
WHERE dag_id = 'transform_marts'
  AND (state = 'queued'
       OR (start_date IS NOT NULL
           AND start_date < now()
           AND (end_date IS NULL OR end_date > now() - interval '${HOURS} hours')))
GROUP BY run_type, state
ORDER BY run_type, state;
EOF

echo
echo "== Airflow Postgres: transform_marts duty, last ${HOURS}h (busy seconds clipped to the window; running runs count up to now) =="
pg <<EOF
WITH win AS (
  SELECT now() - interval '${HOURS} hours' AS win_start, now() AS win_end
),
runs AS (
  SELECT dr.start_date,
         EXTRACT(EPOCH FROM (LEAST(COALESCE(dr.end_date, w.win_end), w.win_end)
                              - GREATEST(dr.start_date, w.win_start))) AS busy_s
  FROM dag_run dr, win w
  WHERE dr.dag_id = 'transform_marts'
    AND dr.start_date IS NOT NULL
    AND dr.start_date < w.win_end
    AND (dr.end_date IS NULL OR dr.end_date > w.win_start)
),
gaps AS (
  SELECT EXTRACT(EPOCH FROM (start_date - LAG(start_date) OVER (ORDER BY start_date))) AS gap_s
  FROM runs
)
SELECT
  (SELECT count(*) FROM runs) AS n_runs,
  round(avg(busy_s)::numeric, 1) AS avg_busy_s,
  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY busy_s)::numeric, 1) AS p50_busy_s,
  round(max(busy_s)::numeric, 1) AS max_busy_s,
  round((SELECT avg(gap_s) FROM gaps)::numeric, 1) AS avg_start_gap_s,
  round((SELECT min(gap_s) FROM gaps)::numeric, 1) AS min_start_gap_s,
  round((sum(busy_s) / (${HOURS} * 3600.0) * 100)::numeric, 1) AS duty_pct
FROM runs;
EOF
