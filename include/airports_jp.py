from __future__ import annotations

# The hubs whose traffic the antenna + Japan-context feed actually surface: the Tokyo
# pair plus the biggest domestic counterparties, so marquee routes (HND↔CTS, HND↔ITM,
# HND↔FUK) resolve from both ends. 7 airports × 3 calls/airport/day × 30 credits/call = 630/day, from the
# flights bucket which is independent of the /states feeder budget (verified 2026-06-10);
# tests/test_credit_budget.py enforces this. List-of-dicts so ingest_flights' dynamic
# task mapping mirrors ingest_states' region mapping.
AIRPORTS_JP: list[dict[str, str]] = [
    {"icao": "RJTT", "name": "Tokyo Haneda"},
    {"icao": "RJAA", "name": "Tokyo Narita"},
    {"icao": "RJBB", "name": "Osaka Kansai"},
    {"icao": "RJOO", "name": "Osaka Itami"},
    {"icao": "RJCC", "name": "Sapporo New Chitose"},
    {"icao": "RJFF", "name": "Fukuoka"},
    {"icao": "RJGG", "name": "Nagoya Chubu Centrair"},
]
