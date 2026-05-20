from airflow.sdk import Asset

bronze_flights = Asset("s3://opensky/bronze/flights/")
raw_states_landed = Asset("s3://opensky/bronze/states_raw/")
bronze_states_table = Asset("s3://opensky/warehouse/bronze.db/opensky_states/")