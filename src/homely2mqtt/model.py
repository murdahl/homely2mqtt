import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Homely names its areas "Floor 1 - Living room"
_AREA = re.compile(r"^Floor\s+(\d+)\s*-\s*(.+)$")

# Homely's own alarm states, mapped to the Home Assistant vocabulary. The API documentation and the live API
# disagree on some names (ARMED_NIGHT_PENDING vs ARM_NIGHT_PENDING), so both are listed.
ALARM_STATES = {
    "DISARMED": "disarmed",
    "ARMED_AWAY": "armed_away",
    "ARMED_NIGHT": "armed_night",
    "ARMED_PARTLY": "armed_home",
    "ARMED_STAY": "armed_home",
    "ARMED_AWAY_PENDING": "arming",
    "ARM_AWAY_PENDING": "arming",
    "ARMED_NIGHT_PENDING": "arming",
    "ARM_NIGHT_PENDING": "arming",
    "ARM_STAY_PENDING": "arming",
    "ARM_PENDING": "arming",
    "ALARM_PENDING": "pending",
    "ALARM_STAY_PENDING": "pending",
    "BREACHED": "triggered",
}


def normalize_alarm(raw: str | None) -> str | None:
    if not raw:
        return None
    key = raw.strip().upper()
    if key in ALARM_STATES:
        return ALARM_STATES[key]
    log.warning("Unknown Homely alarm state %r", raw)
    if "BREACH" in key:
        return "triggered"
    if key.startswith("ALARM") and "PENDING" in key:
        return "pending"
    if "PENDING" in key:
        return "arming"
    if key.startswith("ARMED"):
        return "armed_home"
    return key.lower()


def _identity(value: Any) -> Any:
    return value


def _kwh(value: Any) -> Any:
    return round(value / 1000, 3) if isinstance(value, (int, float)) and not isinstance(value, bool) else value


# (feature, state) -> (key in the published state, conversion). Several sources for one key: the newest wins.
READINGS: dict[tuple[str, str], tuple[str, Callable[[Any], Any]]] = {
    ("alarm", "tamper"): ("tamper", _identity),
    ("alarm", "fire"): ("fire", _identity),
    ("alarm", "flood"): ("flood", _identity),
    ("temperature", "temperature"): ("temperature", _identity),
    ("battery", "low"): ("battery_low", _identity),
    ("battery", "defect"): ("battery_defect", _identity),
    ("battery", "voltage"): ("battery_voltage", _identity),
    ("diagnostic", "networklinkstrength"): ("link_strength", _identity),
    ("metering", "demand"): ("power", _identity),
    ("metering", "summationdelivered"): ("energy_imported", _kwh),
    ("metering", "summationreceived"): ("energy_exported", _kwh),
    ("lock", "state"): ("locked", _identity),
    ("report", "locked"): ("locked", _identity),
    ("report", "doorclosed"): ("door_closed", _identity),
    ("report", "event"): ("lock_event", _identity),
    ("report", "lowbat"): ("battery_low", _identity),
}

# What alarm.alarm means depends on the sensor
ALARM_READING = {"motion": "motion", "entry": "open"}


@dataclass
class Reading:
    value: Any
    updated: str | None


@dataclass
class Device:
    id: str
    name: str
    area: str | None = None
    model: str | None = None
    online: bool | None = None
    readings: dict[tuple[str, str], Reading] = field(default_factory=dict)

    @property
    def type(self) -> str:
        features = {feature for feature, _ in self.readings}
        if "lock" in features or ("report", "locked") in self.readings:
            return "lock"
        if ("alarm", "fire") in self.readings:
            return "smoke"
        if ("alarm", "flood") in self.readings:
            return "flood"
        if "metering" in features:
            return "meter"
        if "thermostat" in features:
            return "thermostat"
        if ("alarm", "alarm") in self.readings:
            return "motion" if "motion" in (self.model or "").lower() else "entry"
        return "other"

    def set_reading(self, feature: str, state: str, value: Any, updated: str | None) -> bool:
        """Stores a value unless we already hold a newer one. Returns whether anything changed."""
        current = self.readings.get((feature, state))
        if current and current.updated and updated and updated < current.updated:
            return False
        if current == Reading(value, updated):
            return False
        self.readings[(feature, state)] = Reading(value, updated)
        return current is None or current.value != value

    def values(self) -> dict[str, Any]:
        newest: dict[str, tuple[str, Any]] = {}
        alarm_key = ALARM_READING.get(self.type, "alarm")
        for (feature, state), reading in self.readings.items():
            if (feature, state) == ("alarm", "alarm"):
                key, convert = alarm_key, _identity
            elif (feature, state) in READINGS:
                key, convert = READINGS[(feature, state)]
            else:
                continue
            if reading.value is None:
                continue
            updated = reading.updated or ""
            if key not in newest or updated > newest[key][0]:
                newest[key] = (updated, convert(reading.value))
        return {key: value for key, (_, value) in sorted(newest.items())}

    def to_state(self) -> dict:
        floor, room = split_area(self.area)
        updated = max((reading.updated for reading in self.readings.values() if reading.updated), default=None)
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "model": self.model,
            "area": self.area,
            "floor": floor,
            "room": room,
            "online": self.online,
            "updated": updated,
            **self.values(),
        }


def split_area(area: str | None) -> tuple[int | None, str | None]:
    if not area:
        return None, None
    match = _AREA.match(area.strip())
    if not match:
        return None, area.strip()
    return int(match.group(1)), match.group(2).strip()


def _states(device: dict):
    for feature, body in (device.get("features") or {}).items():
        for state, reading in ((body or {}).get("states") or {}).items():
            # Homely lists some states without a value, e.g. the lock's "added": {}
            if isinstance(reading, dict) and "value" in reading:
                yield feature, state, reading["value"], reading.get("lastUpdated")


class Home:
    """One Homely location: its alarm and devices, kept current from REST snapshots and websocket events."""

    def __init__(self, location_id: str, name: str | None = None) -> None:
        self.location_id = location_id
        self.name = name
        self.gateway: str | None = None
        self.alarm_raw: str | None = None
        self.devices: dict[str, Device] = {}

    @property
    def alarm(self) -> str | None:
        return normalize_alarm(self.alarm_raw)

    def apply_snapshot(self, home: dict) -> None:
        self.name = home.get("name") or self.name
        self.gateway = home.get("gatewayserial") or self.gateway
        # Keep the last known alarm state if a response lacks it
        self.alarm_raw = home.get("alarmState") or self.alarm_raw

        devices: dict[str, Device] = {}
        for raw in home.get("devices", []):
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            device = self.devices.get(raw["id"]) or Device(id=raw["id"], name=raw["id"])
            device.name = raw.get("name") or device.name
            device.area = raw.get("location") or device.area
            device.model = raw.get("modelName") or device.model
            device.online = raw.get("online") if isinstance(raw.get("online"), bool) else device.online
            for feature, state, value, updated in _states(raw):
                device.set_reading(feature, state, value, updated)
            devices[device.id] = device
        self.devices = devices

    def apply_event(self, event: dict) -> tuple[bool, bool]:
        """Applies a websocket event. Returns (changed, needs a snapshot because the device is unknown)."""
        kind, data = event.get("type"), event.get("data")
        if not isinstance(data, dict):
            return False, False
        if data.get("locationId") and data["locationId"] != self.location_id:
            return False, False

        if kind == "alarm-state-changed":
            state = data.get("state")
            if not state or state == self.alarm_raw:
                return False, False
            self.alarm_raw = state
            return True, False

        if kind == "device-state-changed":
            device = self.devices.get(data.get("deviceId"))
            if device is None:
                return False, True
            changed = False
            for change in data.get("changes") or []:
                if isinstance(change, dict) and change.get("feature") and change.get("stateName"):
                    changed |= device.set_reading(change["feature"], change["stateName"], change.get("value"), change.get("lastUpdated"))
            return changed, False

        log.debug("Ignoring websocket event %s", kind)
        return False, False

    def to_state(self, stream_connected: bool) -> dict:
        return {
            "location": self.name,
            "location_id": self.location_id,
            "gateway": self.gateway,
            "alarm": self.alarm,
            "alarm_raw": self.alarm_raw,
            "stream": "connected" if stream_connected else "disconnected",
            "devices": [device.to_state() for device in sorted(self.devices.values(), key=lambda device: (device.area or "", device.name))],
        }
