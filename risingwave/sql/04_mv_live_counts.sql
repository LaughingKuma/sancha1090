-- Long format (dim, val, aircraft): the Superset Live panel charts one dataset and
-- filters by dim. Numbers must reconcile with silver/gold for the same window.
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_live_counts AS
SELECT 'total' AS dim, 'all' AS val, count(*) AS aircraft FROM mv_current_aircraft
UNION ALL
SELECT 'military', 'military', count(*) FROM mv_current_aircraft
WHERE is_military
UNION ALL
SELECT 'helicopter', 'helicopter', count(*) FROM mv_current_aircraft
WHERE is_helicopter
UNION ALL
SELECT 'mlat', 'mlat', count(*) FROM mv_current_aircraft
WHERE position_source = 'mlat'
UNION ALL
SELECT 'category', category, count(*) FROM mv_current_aircraft
WHERE category IS NOT NULL
GROUP BY category
UNION ALL
SELECT 'airline', airline_name, count(*) FROM mv_current_aircraft
WHERE airline_name IS NOT NULL
GROUP BY airline_name
UNION ALL
SELECT 'country', reg_country, count(*) FROM mv_current_aircraft
WHERE reg_country IS NOT NULL
GROUP BY reg_country;
