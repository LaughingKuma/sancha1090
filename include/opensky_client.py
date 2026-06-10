"""OpenSky Network API client.

Handles OAuth2 token lifecycle, retries with exponential backoff,
and exposes typed methods for the endpoints we use.

Designed to be importable and testable without Airflow.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

OPENSKY_BASE_URL = "https://opensky-network.org/api"
OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/"
    "protocol/openid-connect/token"
)


class OpenSkyClient:
    """Lightweight OpenSky API client.

    Anonymous mode works (no client_id/secret) but is rate-limited to
    400 credits/day. Authenticated mode gets 4000 credits/day, and an
    active feeder (>=30% monthly receiver uptime) gets 8000 credits/day.
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 5,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.max_retries = max_retries
        self._token: str | None = None
        self._token_expiry: float = 0.0

    @classmethod
    def from_env(cls) -> "OpenSkyClient":
        """Build a client from OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET env vars.

        Returns an anonymous client if either is missing.
        """
        client_id = os.environ.get("OPENSKY_CLIENT_ID") or None
        client_secret = os.environ.get("OPENSKY_CLIENT_SECRET") or None
        return cls(client_id=client_id, client_secret=client_secret)

    @property
    def is_authenticated(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def _get_token(self) -> str | None:
        """Fetch or refresh OAuth2 token. Returns None for anonymous mode.

        Cached: only re-fetches when the current token is within 30 seconds of expiry.
        """
        if not self.is_authenticated:
            return None

        now = time.time()
        if self._token and now < self._token_expiry - 30:
            return self._token

        response = httpx.post(
            OPENSKY_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expiry = now + payload.get("expires_in", 1800)
        return self._token

    def _request(self, path: str, params: dict | None = None) -> Any:
        """GET {base}/{path} with retries on transient errors.

        Retries: 429 (rate limit) and 5xx with exponential backoff.
        Does NOT retry: 4xx-other (those are permanent).
        """
        url = f"{OPENSKY_BASE_URL}{path}"
        headers = {}
        token = self._get_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        backoff = 1.0
        for attempt in range(1, self.max_retries + 1):
            try:
                response = httpx.get(
                    url, params=params, headers=headers, timeout=self.timeout
                )

                if response.status_code == 429:
                    if attempt == self.max_retries:
                        response.raise_for_status()
                    logger.warning(
                        "OpenSky rate limit (attempt %d/%d), sleeping %.1fs",
                        attempt, self.max_retries, backoff,
                    )
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue

                if response.status_code >= 500:
                    logger.warning(
                        "OpenSky server error %d (attempt %d/%d), sleeping %.1fs",
                        response.status_code, attempt, self.max_retries, backoff,
                    )
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue

                response.raise_for_status()
                return response.json()

            except httpx.RequestError as exc:
                # Network-level errors (timeout, connection refused, DNS)
                # are transient — same retry behavior.
                logger.warning(
                    "OpenSky network error (attempt %d/%d): %s",
                    attempt, self.max_retries, exc,
                )
                if attempt == self.max_retries:
                    raise
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

        raise RuntimeError(
            f"OpenSky request to {path} failed after {self.max_retries} retries"
        )

    def get_states(
        self,
        bbox: tuple[float, float, float, float] | None = None,
        time_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Get state vectors for all aircraft, optionally bounded.

        bbox: (lamin, lomin, lamax, lomax) — latitude/longitude box
        time_seconds: epoch seconds for the snapshot (default: now)
        """
        params: dict[str, Any] = {}
        if bbox:
            lamin, lomin, lamax, lomax = bbox
            params.update(lamin=lamin, lomin=lomin, lamax=lamax, lomax=lomax)
        if time_seconds:
            params["time"] = time_seconds
        return self._request("/states/all", params=params)