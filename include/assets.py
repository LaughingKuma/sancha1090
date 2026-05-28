from airflow.sdk import Asset

bronze_flights = Asset("s3://sancha1090/bronze/flights/")
raw_states_landed = Asset("s3://sancha1090/bronze/states_raw/")
bronze_states_table = Asset("s3://sancha1090/warehouse/bronze.db/opensky_states/")