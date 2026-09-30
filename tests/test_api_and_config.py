import json

import httpx
import pytest
from fixtures import home

from homely2mqtt.api import AuthError, HomelyClient, HomelyError, pick_location
from homely2mqtt.config import Config, ConfigError

ENV = {"HOMELY_USER": "me@example.com", "HOMELY_PASSWORD": "secret", "MQTT_HOST": "mosquitto"}


class FakeHomely:
    """Records requests and answers like the Homely SDK API."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.valid_tokens: set[str] = set()
        self.issued = 0
        self.refresh_status = 200
        self.expires_in = 1800

    def _issue(self) -> httpx.Response:
        self.issued += 1
        token = f"access-{self.issued}"
        self.valid_tokens.add(token)
        return httpx.Response(200, json={
            "access_token": token,
            "expires_in": self.expires_in,
            "refresh_token": f"refresh-{self.issued}",
            "refresh_expires_in": 7776000,
        })

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/homely")
        self.requests.append(path)
        if path == "/oauth/token":
            body = json.loads(request.content)
            return self._issue() if body == {"username": "me@example.com", "password": "secret"} else httpx.Response(401)
        if path == "/oauth/refresh-token":
            if self.refresh_status != 200:
                return httpx.Response(self.refresh_status, json={"statusCode": self.refresh_status, "message": "Refresh token expired"})
            return self._issue()

        if request.headers.get("authorization", "").removeprefix("Bearer ") not in self.valid_tokens:
            return httpx.Response(401, json={"statusCode": 401, "message": "Unauthorized"})
        if path == "/locations":
            return httpx.Response(200, json=[{"name": "Home", "locationId": "loc-1", "role": "OWNER"}])
        if path == "/home/loc-1":
            return httpx.Response(200, json=home())
        return httpx.Response(404)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def api():
    return FakeHomely()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def client(api, clock):
    return HomelyClient(httpx.Client(transport=httpx.MockTransport(api.handler)), "me@example.com", "secret", clock)


def test_logs_in_once_and_reuses_the_token(client, api):
    assert client.locations()[0]["locationId"] == "loc-1"
    assert client.home("loc-1")["name"] == "Home"
    assert api.requests == ["/oauth/token", "/locations", "/home/loc-1"]


def test_refreshes_shortly_before_expiry(client, api, clock):
    client.locations()
    clock.now += 1800 - 61
    client.locations()
    assert "/oauth/refresh-token" not in api.requests
    clock.now += 2
    client.locations()
    assert api.requests[-2:] == ["/oauth/refresh-token", "/locations"]


def test_short_lived_tokens_are_still_used(client, api, clock):
    api.expires_in = 60
    client.locations()
    clock.now += 50
    client.locations()
    assert api.requests.count("/oauth/token") == 1
    assert "/oauth/refresh-token" not in api.requests


def test_rejected_refresh_falls_back_to_login(client, api, clock):
    client.locations()
    api.refresh_status = 400
    clock.now += 1800
    client.locations()
    assert api.requests[-3:] == ["/oauth/refresh-token", "/oauth/token", "/locations"]
    assert client.tokens.access_token == "access-2"


def test_refresh_server_error_falls_back_to_login(client, api, clock):
    client.locations()
    api.refresh_status = 503
    clock.now += 1800
    client.locations()
    assert api.requests[-2:] == ["/oauth/token", "/locations"]


def test_expired_refresh_token_goes_straight_to_login(client, api, clock):
    client.locations()
    clock.now += 7776000
    client.locations()
    assert api.requests[-2:] == ["/oauth/token", "/locations"]


def test_revoked_token_logs_in_again_and_retries_once(client, api):
    client.locations()
    api.valid_tokens.clear()
    assert client.home("loc-1")["name"] == "Home"
    assert api.requests[-3:] == ["/home/loc-1", "/oauth/token", "/home/loc-1"]


def test_wrong_password_is_an_auth_error(api, clock):
    client = HomelyClient(httpx.Client(transport=httpx.MockTransport(api.handler)), "me@example.com", "wrong", clock)
    with pytest.raises(AuthError):
        client.locations()


def test_token_response_without_token_is_rejected(clock):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"statusCode": 401}))
    client = HomelyClient(httpx.Client(transport=transport), "me@example.com", "secret", clock)
    with pytest.raises(AuthError):
        client.access_token()
    assert client.tokens is None


def test_unexpected_home_response(clock):
    def handler(request):
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "a", "expires_in": 1800})
        return httpx.Response(200, json={"statusCode": 500})

    client = HomelyClient(httpx.Client(transport=httpx.MockTransport(handler)), "me@example.com", "secret", clock)
    with pytest.raises(HomelyError):
        client.home("loc-1")


def test_pick_location():
    locations = [{"name": "Home", "locationId": "a"}, {"name": "Cabin", "locationId": "b"}]
    assert pick_location(locations[:1], None)["locationId"] == "a"
    assert pick_location(locations, "Cabin")["locationId"] == "b"
    assert pick_location(locations, "a")["name"] == "Home"
    with pytest.raises(LookupError, match="HOMELY_LOCATION"):
        pick_location(locations, None)
    with pytest.raises(LookupError, match="'Boat'"):
        pick_location(locations, "Boat")


def test_config_defaults():
    config = Config.from_env(ENV)
    assert (config.mqtt_topic_prefix, config.poll_interval, config.location) == ("homely", 300, None)


def test_config_overrides():
    config = Config.from_env({**ENV, "MQTT_TOPIC_PREFIX": "alarm/", "POLL_INTERVAL": "60", "HOMELY_LOCATION": "Cabin"})
    assert (config.mqtt_topic_prefix, config.poll_interval, config.location) == ("alarm", 60, "Cabin")


def test_config_errors():
    with pytest.raises(ConfigError, match="HOMELY_PASSWORD"):
        Config.from_env({"HOMELY_USER": "x", "MQTT_HOST": "y"})
    with pytest.raises(ConfigError, match="at least"):
        Config.from_env({**ENV, "POLL_INTERVAL": "5"})
