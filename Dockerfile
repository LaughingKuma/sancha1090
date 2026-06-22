FROM apache/airflow:3.2.1-python3.12

USER airflow

# Pin the dbt-core stack explicitly: without dbt-postgres's constraints, pip backtracks endlessly resolving it.
RUN pip install --no-cache-dir \
    httpx==0.28.1 \
    polars==1.40.1 \
    pyarrow==24.0.0 \
    pandas==3.0.3 \
    psycopg2-binary==2.9.12 \
    s3fs==2026.4.0 \
    dbt-core==1.12.0b1 \
    dbt-adapters==1.24.2 \
    dbt-common==1.38.0 \
    dbt-protos==1.0.498 \
    dbt-clickhouse==1.9.2 \
    pytest==9.0.3 \
    clickhouse-connect==1.3.0
