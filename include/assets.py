from airflow.sdk import Asset

bronze_states = Asset("s3://opensky/bronze/states/")
bronze_flights = Asset("s3://opensky/bronze/flights/")