from airflow.sdk import Asset

bronze_states = Asset("s3://opensky/bronze/states/")
bronze_flights = Asset("s3://opensky/bronze/flights/")
raw_states_landed = Asset("s3://opensky/bronze/states_raw/")