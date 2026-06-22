from airflow.sdk import Asset

# Prefix matches the producer manifest's `stream` field (adsb_state), not OpenSky's states_raw.
adsb_raw_landed = Asset("s3://sancha1090/bronze/adsb_state/")
adsb_bronze_table = Asset("s3://sancha1090/warehouse/bronze.db/adsb_states/")
