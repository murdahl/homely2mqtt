import json
import logging
import queue
import time

import httpx
import paho.mqtt.client as mqtt

from .api import HomelyClient, HomelyError, describe_locations, pick_location
from .config import Config
from .model import Home
from .stream import HomelyStream, StreamError

log = logging.getLogger(__name__)

# Reconnect quickly a few times, then back off (the same schedule as python-homely)
RECONNECT_DELAYS = (10, 10, 10, 60, 60, 60, 60, 60, 300)
# While the websocket is down, poll more often so changes still come through
FALLBACK_POLL_INTERVAL = 60
# Also the longest we go without checking that the websocket is still alive
MAX_WAIT = 30


class Bridge:
    def __init__(self, config: Config, client: HomelyClient, stream: HomelyStream, events: queue.Queue) -> None:
        self.config = config
        self.client = client
        self.stream = stream
        self.events = events
        self.status_topic = f"{config.mqtt_topic_prefix}/status"
        self.state_topic = f"{config.mqtt_topic_prefix}/state"
        self.last_status: str | None = None
        self.last_state: str | None = None
        self.home: Home | None = None
        self.stream_connected = False
        self.reconnect_attempt = 0
        self.next_poll = 0.0
        self.next_connect = 0.0
        self.mqtt = self._create_mqtt_client()

    def _create_mqtt_client(self) -> mqtt.Client:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="homely2mqtt")
        if self.config.mqtt_username:
            client.username_pw_set(self.config.mqtt_username, self.config.mqtt_password)
        client.will_set(self.status_topic, "offline", retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = lambda _client, _userdata, _flags, reason, _props: log.warning(
            "MQTT disconnected: %s", reason
        )
        return client

    def _on_connect(self, client: mqtt.Client, _userdata, _flags, reason, _props) -> None:
        log.info("MQTT connected: %s", reason)
        # Re-publish on reconnect, since the broker may have fired our will in the meantime
        if self.last_status:
            client.publish(self.status_topic, self.last_status, retain=True)

    def _set_status(self, status: str) -> None:
        if status != self.last_status:
            self.mqtt.publish(self.status_topic, status, retain=True)
            self.last_status = status

    def _publish(self) -> None:
        if self.home is None:
            return
        state = json.dumps(self.home.to_state(self.stream_connected))
        if state != self.last_state:
            self.mqtt.publish(self.state_topic, state, retain=True)
            self.last_state = state
            log.debug("Published %s", state)

    def _poll(self) -> None:
        try:
            if self.home is None:
                locations = self.client.locations()
                log.info("Homely locations: %s", describe_locations(locations))
                location = pick_location(locations, self.config.location)
                self.home = Home(location["locationId"], location.get("name"))
            self.home.apply_snapshot(self.client.home(self.home.location_id))
        except (httpx.HTTPError, HomelyError) as error:
            log.warning("Homely unreachable: %s", error)
            # Live updates still arriving means Homely is fine; a failed poll alone isn't worth an "offline"
            if not self.stream_connected:
                self._set_status("offline")
            return
        self._publish()
        self._set_status("online")

    def _connect_stream(self) -> None:
        try:
            self.stream.connect(self.home.location_id, self.client.access_token())
        except (StreamError, httpx.HTTPError, HomelyError) as error:
            delay = RECONNECT_DELAYS[min(self.reconnect_attempt, len(RECONNECT_DELAYS) - 1)]
            self.reconnect_attempt += 1
            log.warning("Homely websocket connect failed (%s), retrying in %s s", error, delay)
            self.next_connect = time.monotonic() + delay

    def _handle(self, kind: str, data) -> None:
        if kind == "connected":
            log.info("Homely websocket connected")
            self.stream_connected = True
            self.reconnect_attempt = 0
            # Catch up on whatever changed while we weren't listening
            self.next_poll = 0.0
        elif kind == "disconnected":
            self._on_stream_lost(data)
        elif kind == "event" and self.home is not None:
            changed, unknown_device = self.home.apply_event(data)
            if unknown_device:
                log.info("Update for an unknown Homely device, fetching the device list")
                self.next_poll = 0.0
            if changed:
                self._publish()

    def _on_stream_lost(self, reason: str | None) -> None:
        if not self.stream_connected:
            return
        log.warning("Homely websocket disconnected: %s", reason)
        self.stream_connected = False
        self.stream.close()
        self.next_connect = time.monotonic() + RECONNECT_DELAYS[0]
        # If reconnecting doesn't work out, poll soon rather than waiting out the full interval
        self.next_poll = min(self.next_poll, time.monotonic() + MAX_WAIT)
        self._publish()

    def _run_timers(self) -> None:
        now = time.monotonic()
        if self.stream_connected and not self.stream.alive:
            self._on_stream_lost("connection lost")
        if now >= self.next_poll:
            self._poll()
            interval = self.config.poll_interval if self.stream_connected else min(self.config.poll_interval, FALLBACK_POLL_INTERVAL)
            self.next_poll = time.monotonic() + interval
        if self.home is not None and not self.stream_connected and not self.stream.alive and now >= self.next_connect:
            self._connect_stream()

    def run(self) -> None:
        self.mqtt.connect_async(self.config.mqtt_host, self.config.mqtt_port)
        self.mqtt.loop_start()
        try:
            while True:
                self._run_timers()
                wake = self.next_poll if self.stream_connected else min(self.next_poll, self.next_connect)
                timeout = min(max(0.0, wake - time.monotonic()), MAX_WAIT)
                try:
                    kind, data = self.events.get(timeout=timeout)
                except queue.Empty:
                    continue
                self._handle(kind, data)
        finally:
            self.stream.close()
            self.mqtt.publish(self.status_topic, "offline", retain=True).wait_for_publish(timeout=2)
            self.mqtt.loop_stop()
            self.mqtt.disconnect()


def run(config: Config) -> None:
    events: queue.Queue = queue.Queue()
    with httpx.Client(timeout=20, headers={"User-Agent": "homely2mqtt"}) as http:
        client = HomelyClient(http, config.homely_user, config.homely_password)
        Bridge(config, client, HomelyStream(events), events).run()
