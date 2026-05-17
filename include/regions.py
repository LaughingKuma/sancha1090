"""Geographic regions for splitting OpenSky requests.

Why split the world into regions?
1. Pedagogically: dynamic task mapping needs an input list. Geographic
   regions are a natural one.
2. Operationally: smaller bboxes finish faster, give finer-grained retry
   on failure (a 429 over Europe doesn't lose the data over Asia), and
   parallelize across workers.
3. Cost: OpenSky charges more credits for larger bboxes; smaller is cheaper.

Bounding box format: (lamin, lomin, lamax, lomax)
  Latitudes: -90 (south pole) to 90 (north pole)
  Longitudes: -180 (west) to 180 (east)
"""

from __future__ import annotations

REGIONS: list[dict[str, float | str]] = [
    {"name": "north_america", "lamin": 24.0,  "lomin": -125.0, "lamax": 60.0,  "lomax": -66.0},
    {"name": "europe",        "lamin": 35.0,  "lomin": -10.0,  "lamax": 60.0,  "lomax": 30.0},
    {"name": "east_asia",     "lamin": 20.0,  "lomin": 100.0,  "lamax": 50.0,  "lomax": 145.0},
    {"name": "south_asia",    "lamin": 5.0,   "lomin": 65.0,   "lamax": 35.0,  "lomax": 95.0},
    {"name": "oceania",       "lamin": -45.0, "lomin": 110.0,  "lamax": -10.0, "lomax": 180.0},
    {"name": "south_america", "lamin": -55.0, "lomin": -85.0,  "lamax": 15.0,  "lomax": -35.0},
    {"name": "africa",        "lamin": -35.0, "lomin": -20.0,  "lamax": 35.0,  "lomax": 55.0},
    {"name": "middle_east",   "lamin": 12.0,  "lomin": 30.0,   "lamax": 42.0,  "lomax": 65.0},
]