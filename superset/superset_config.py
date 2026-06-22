"""Superset config overrides.

Mounted to /app/pythonpath/superset_config.py inside the container.
Superset auto-discovers it at startup.
"""

import os

# Where Superset stores its own metadata
SQLALCHEMY_DATABASE_URI = (
    f"postgresql+psycopg2://"
    f"{os.environ['DATABASE_USER']}:{os.environ['DATABASE_PASSWORD']}"
    f"@{os.environ['DATABASE_HOST']}:{os.environ['DATABASE_PORT']}"
    f"/{os.environ['DATABASE_DB']}"
)

SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]

# Feature flags. Embedded SQL editor and dashboard cross-filters are useful.
FEATURE_FLAGS = {
    "EMBEDDED_SUPERSET": False,
    "DASHBOARD_CROSS_FILTERS": True,
    "DASHBOARD_RBAC": True,
}

# In-process caching. Production would use Redis.
CACHE_CONFIG = {
    "CACHE_TYPE": "SimpleCache",
    "CACHE_DEFAULT_TIMEOUT": 300,
}

# Mapbox token for deck.gl basemaps. Optional — charts render without it.
MAPBOX_API_KEY = os.environ.get("MAPBOX_API_KEY", "")

# Disable telemetry
SCARF_ANALYTICS = False
