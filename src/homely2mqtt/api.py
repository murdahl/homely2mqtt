import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://sdk.iotiliti.cloud/homely"
# Renew a little before the token actually expires, but never more than a tenth of its lifetime
EXPIRY_MARGIN = 60


class HomelyError(Exception):
    pass


class AuthError(HomelyError):
    pass


@dataclass
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: float
    refresh_expires_at: float

    @classmethod
    def from_response(cls, body: dict, now: float) -> "Tokens":
        if not isinstance(body, dict) or not body.get("access_token"):
            raise AuthError("Token response without an access token")
        lifetime = float(body.get("expires_in") or 60)
        refresh_lifetime = float(body.get("refresh_expires_in") or 0)
        return cls(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_at=now + lifetime - min(EXPIRY_MARGIN, lifetime / 10),
            refresh_expires_at=now + refresh_lifetime - min(EXPIRY_MARGIN, refresh_lifetime / 10),
        )


class HomelyClient:
    """Client for the Homely SDK API. Read-only: Homely offers no commands."""

    def __init__(self, http: httpx.Client, username: str, password: str, clock: Callable[[], float] = time.time) -> None:
        self.http = http
        self.username = username
        self.password = password
        self.clock = clock
        self.tokens: Tokens | None = None

    def _token_request(self, path: str, body: dict) -> None:
        response = self.http.post(f"{BASE_URL}{path}", json=body)
        if response.status_code in (400, 401, 403):
            raise AuthError(f"{path} rejected ({response.status_code})")
        response.raise_for_status()
        self.tokens = Tokens.from_response(response.json(), self.clock())

    def login(self) -> None:
        log.info("Logging in to Homely as %s", self.username)
        self._token_request("/oauth/token", {"username": self.username, "password": self.password})

    def _renew(self) -> None:
        tokens = self.tokens
        if tokens and tokens.refresh_token and self.clock() < tokens.refresh_expires_at:
            try:
                self._token_request("/oauth/refresh-token", {"refresh_token": tokens.refresh_token})
                return
            except (AuthError, httpx.HTTPError) as error:
                log.warning("Token refresh failed (%s), logging in again", error)
        self.login()

    def access_token(self) -> str:
        if not self.tokens or self.clock() >= self.tokens.expires_at:
            self._renew()
        return self.tokens.access_token

    def _get(self, path: str):
        for attempt in range(2):
            response = self.http.get(f"{BASE_URL}{path}", headers={"Authorization": f"Bearer {self.access_token()}"})
            if response.status_code in (401, 403) and attempt == 0:
                # Revoked before it expired (e.g. password changed); get a new one and retry once
                self.tokens = None
                continue
            if response.status_code in (401, 403):
                raise AuthError(f"{path} rejected ({response.status_code})")
            response.raise_for_status()
            return response.json()
        raise AssertionError("unreachable")

    def locations(self) -> list[dict]:
        locations = self._get("/locations")
        if not isinstance(locations, list):
            raise HomelyError("Unexpected /locations response")
        return locations

    def home(self, location_id: str) -> dict:
        home = self._get(f"/home/{location_id}")
        if not isinstance(home, dict) or not isinstance(home.get("devices"), list):
            raise HomelyError("Unexpected /home response")
        return home


def pick_location(locations: list[dict], wanted: str | None) -> dict:
    if wanted:
        for location in locations:
            if wanted in (location.get("locationId"), location.get("name")):
                return location
        raise LookupError(f"No Homely location named {wanted!r}; found {describe_locations(locations)}")
    if len(locations) != 1:
        raise LookupError(f"Set HOMELY_LOCATION to one of {describe_locations(locations)}")
    return locations[0]


def describe_locations(locations: list[dict]) -> str:
    return ", ".join(f"{location.get('name')!r} ({location.get('locationId')})" for location in locations) or "none"
