FROM apache/airflow:3.2.1-python3.12

USER airflow

RUN pip install --no-cache-dir \
    httpx==0.27.0 \
    polars==1.5.0 \
    pyarrow==17.0.0 \
    pandas==2.1.4 \
    psycopg2-binary==2.9.9 \
    s3fs==2024.6.1 \
    dbt-postgres==1.8.2 \
    pytest==8.3.3 \
    respx==0.21.1
