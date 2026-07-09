from airflow.sdk import Asset

raw_states_landed = Asset("s3://sancha1090/bronze/states_raw/")
bronze_states_table = Asset("s3://sancha1090/warehouse/bronze.db/opensky_states/")
raw_flights_landed = Asset("s3://sancha1090/bronze/flights_raw/")
bronze_flights_table = Asset("s3://sancha1090/warehouse/bronze.db/opensky_flights/")
bronze_aircraft_db_table = Asset("s3://sancha1090/warehouse/bronze.db/aircraft_db/")
bronze_swim_table = Asset("s3://sancha1090/warehouse/bronze.db/swim_flightdata/")