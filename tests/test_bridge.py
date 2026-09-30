import json
import queue

import httpx
import pytest
from fixtures import alarm_change, change, home

from homely2mqtt import bridge as bridge_module
from homely2mqtt.api import HomelyError
from homely2mqtt.bridge import Bridge
from homely2mqtt.config import Config
from homely2mqtt.stream import StreamError


class FakeClient:
    def __init__(self) -> None:
        self.fail: Exception | None = None
        self.home_calls = 0

    def locations(self):
        if self.fail:
            raise self.fail
        return [{"name": "Home", "locationId": "loc-1"}]

    def home(self, location_id):
        if self.fail:
            raise self.fail
        self.home_calls += 1
        return home()

    def access_token(self):
        return "token"


class FakeStream:
    def __init__(self) -> None:
        self.alive = False
        self.connects: list[tuple[str, str]] = []
        self.fail = False

    def connect(self, location_id, token):
        self.connects.append((location_id, token))
        if self.fail:
            raise StreamError("refused")
        self.alive = True

    def close(self):
        self.alive = False


class FakeMqtt:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload))

    def last(self, topic):
        return next((payload for t, payload in reversed(self.published) if t == topic), None)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(bridge_module.time, "monotonic", clock)
    return clock


@pytest.fixture
def bridge(clock):
    config = Config(homely_user="me", homely_password="pw", mqtt_host="mqtt")
    bridge = Bridge(config, FakeClient(), FakeStream(), queue.Queue())
    bridge.mqtt = FakeMqtt()
    return bridge


def state(bridge):
    return json.loads(bridge.mqtt.last("homely/state"))


def connect(bridge):
    bridge._run_timers()
    bridge._handle("connected", None)
    bridge._run_timers()


def test_first_run_publishes_state_and_connects_the_stream(bridge):
    connect(bridge)
    assert bridge.mqtt.last("homely/status") == "online"
    assert bridge.stream.connects == [("loc-1", "token")]
    assert state(bridge)["stream"] == "connected"
    # Connecting triggers a catch-up poll
    assert bridge.client.home_calls == 2


def test_events_are_published_right_away(bridge):
    connect(bridge)
    bridge._handle("event", change("door-1", "alarm", "alarm", True))
    door = next(device for device in state(bridge)["devices"] if device["id"] == "door-1")
    assert door["open"] is True

    count = len(bridge.mqtt.published)
    bridge._handle("event", change("door-1", "alarm", "alarm", True, updated="2026-09-30T06:00:01.000Z"))
    assert len(bridge.mqtt.published) == count

    bridge._handle("event", alarm_change("BREACHED"))
    assert state(bridge)["alarm"] == "triggered"


def test_unknown_device_triggers_a_poll(bridge, clock):
    connect(bridge)
    bridge._handle("event", change("new-device", "alarm", "alarm", True))
    calls = bridge.client.home_calls
    bridge._run_timers()
    assert bridge.client.home_calls == calls + 1


def test_lost_stream_reconnects_and_polls_meanwhile(bridge, clock):
    connect(bridge)
    bridge._handle("disconnected", "transport close")
    assert state(bridge)["stream"] == "disconnected"

    calls = bridge.client.home_calls
    clock.now += 10
    bridge._run_timers()
    assert len(bridge.stream.connects) == 2
    clock.now += 30
    bridge._run_timers()
    assert bridge.client.home_calls == calls + 1


def test_silently_dead_stream_is_noticed(bridge, clock):
    connect(bridge)
    bridge.stream.alive = False
    bridge._run_timers()
    assert state(bridge)["stream"] == "disconnected"


def test_failed_reconnects_back_off(bridge, clock):
    connect(bridge)
    bridge.stream.fail = True
    bridge._handle("disconnected", "transport close")
    delays = []
    for _ in range(10):
        clock.now = bridge.next_connect
        bridge._run_timers()
        delays.append(round(bridge.next_connect - clock.now))
    assert delays == [10, 10, 10, 60, 60, 60, 60, 60, 300, 300]


def test_failed_poll_with_live_stream_stays_online(bridge, clock):
    connect(bridge)
    bridge.client.fail = HomelyError("boom")
    clock.now += 300
    bridge._run_timers()
    assert bridge.mqtt.last("homely/status") == "online"


def test_failed_poll_without_stream_goes_offline(bridge, clock):
    bridge.client.fail = httpx.ConnectError("no network")
    bridge._run_timers()
    assert bridge.mqtt.last("homely/status") == "offline"
    assert bridge.stream.connects == []

    bridge.client.fail = None
    clock.now += 60
    bridge._run_timers()
    assert bridge.mqtt.last("homely/status") == "online"
    assert len(bridge.stream.connects) == 1
