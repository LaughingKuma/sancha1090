FROM apache/airflow:3.2.1-python3.12

USER airflow

RUN pip install --no-cache-dir \
    httpx==0.28.1 \
    polars==1.40.1 \
    pyarrow==24.0.0 \
    pandas==3.0.3 \
    psycopg2-binary==2.9.12 \
    s3fs==2026.4.0 \
    dbt-postgres==1.10.0 \
    pytest==9.0.3 \
    respx==0.23.1 \
    "pyiceberg[sql-postgres,pyiceberg-core]==0.11.1"
