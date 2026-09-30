import logging
import queue
from typing import Any
from urllib.parse import urlencode

import socketio

log = logging.getLogger(__name__)

WEBSOCKET_URL = "https://sdk.iotiliti.cloud"
CONNECT_TIMEOUT = 10


class StreamError(Exception):
    pass


class HomelyStream:
    """Homely's live updates (Socket.IO). Callbacks run on the socket's own thread, so everything goes through a queue."""

    def __init__(self, events: queue.Queue) -> None:
        self.events = events
        self.client: socketio.Client | None = None
        # Callbacks from an older connection (e.g. its disconnect while we replace it) must not count for the new one
        self.generation = 0

    @property
    def alive(self) -> bool:
        return self.client is not None and self.client.connected

    def connect(self, location_id: str, token: str) -> None:
        self.close()
        self.generation += 1
        generation = self.generation

        def put(kind: str, data: Any = None) -> None:
            if generation == self.generation:
                self.events.put((kind, data))

        # Reconnecting is left to the bridge: every attempt needs a fresh token in the URL.
        # Never enable the Socket.IO loggers, they would log that URL including the token.
        client = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
        client.on("connect", lambda: put("connected"))
        client.on("disconnect", lambda *reason: put("disconnected", str(reason[0]) if reason else None))
        client.on("event", lambda data: put("event", data) if isinstance(data, dict) else None)
        client.on("*", lambda name, _data: log.debug("Ignoring websocket message %s", name))

        bearer = f"Bearer {token}"
        url = f"{WEBSOCKET_URL}?{urlencode({'locationId': location_id, 'token': bearer})}"
        try:
            client.connect(url, transports=["websocket"], headers={"Authorization": bearer}, wait_timeout=CONNECT_TIMEOUT)
        except socketio.exceptions.ConnectionError as error:
            raise StreamError(str(error) or "connection refused") from None
        self.client = client

    def close(self) -> None:
        client, self.client = self.client, None
        self.generation += 1
        if client is not None:
            try:
                client.disconnect()
            except Exception as error:  # already dead; nothing to clean up
                log.debug("Closing websocket: %s", error)
