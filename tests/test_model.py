import pytest
from fixtures import LOCATION_ID, alarm_change, change, home

from homely2mqtt.model import Home, normalize_alarm, split_area


@pytest.fixture
def house():
    house = Home(LOCATION_ID)
    house.apply_snapshot(home())
    return house


def devices(house):
    return {device["id"]: device for device in house.to_state(stream_connected=True)["devices"]}


def test_snapshot_becomes_flat_device_state(house):
    state = house.to_state(stream_connected=True)
    assert state["location"] == "Home"
    assert state["gateway"] == "0200000000000001"
    assert state["alarm"] == "disarmed"
    assert state["alarm_raw"] == "DISARMED"
    assert state["stream"] == "connected"

    door = devices(house)["door-1"]
    assert door == {
        "id": "door-1",
        "name": "Inngangsdør",
        "type": "entry",
        "model": "Alarm Entry Sensor 2",
        "area": "Floor 1 - Entrance",
        "floor": 1,
        "room": "Entrance",
        "online": True,
        "updated": "2026-09-30T05:00:00.000Z",
        "open": False,
        "tamper": False,
        "temperature": 21.5,
        "battery_low": False,
        "battery_defect": False,
        "battery_voltage": 3,
        "link_strength": 98,
    }


def test_device_types(house):
    assert {id: device["type"] for id, device in devices(house).items()} == {
        "motion-1": "motion",
        "smoke-1": "smoke",
        "door-1": "entry",
        "lock-1": "lock",
        "meter-1": "meter",
    }


def test_alarm_reading_depends_on_the_sensor(house):
    all_devices = devices(house)
    assert all_devices["motion-1"]["motion"] is True
    assert "open" not in all_devices["motion-1"]
    assert all_devices["smoke-1"]["fire"] is False


def test_lock_and_meter_values(house):
    all_devices = devices(house)
    lock = all_devices["lock-1"]
    assert (lock["locked"], lock["door_closed"], lock["lock_event"], lock["battery_low"]) == (True, True, "DOORLOCK_MANUAL_LOCK", False)
    assert "soundvolume" not in lock and "added" not in lock

    meter = all_devices["meter-1"]
    assert (meter["power"], meter["energy_imported"], meter["energy_exported"]) == (1830, 1234.567, 0)
    assert meter["online"] is False
    assert (meter["floor"], meter["room"]) == (None, "Technical room")


def test_websocket_change_updates_a_reading(house):
    changed, unknown = house.apply_event(change("door-1", "alarm", "alarm", True))
    assert (changed, unknown) == (True, False)
    assert devices(house)["door-1"]["open"] is True
    assert devices(house)["door-1"]["updated"] == "2026-09-30T06:00:00.000Z"


def test_same_value_is_not_a_change(house):
    assert house.apply_event(change("door-1", "alarm", "alarm", False)) == (False, False)


def test_older_value_does_not_overwrite_a_newer_one(house):
    house.apply_event(change("door-1", "alarm", "alarm", True, updated="2026-09-30T06:00:00.000Z"))
    assert house.apply_event(change("door-1", "alarm", "alarm", False, updated="2026-09-30T05:59:00.000Z")) == (False, False)
    assert devices(house)["door-1"]["open"] is True


def test_snapshot_keeps_newer_websocket_value(house):
    house.apply_event(change("door-1", "alarm", "alarm", True, updated="2026-09-30T06:00:00.000Z"))
    house.apply_snapshot(home())  # still says closed, as of 05:00
    assert devices(house)["door-1"]["open"] is True


def test_newest_source_wins_for_the_lock(house):
    house.apply_event(change("lock-1", "report", "locked", False, updated="2026-09-30T07:00:00.000Z"))
    assert devices(house)["lock-1"]["locked"] is False
    house.apply_event(change("lock-1", "lock", "state", True, updated="2026-09-30T07:05:00.000Z"))
    assert devices(house)["lock-1"]["locked"] is True


def test_unknown_device_asks_for_a_snapshot(house):
    assert house.apply_event(change("new-device", "alarm", "alarm", True)) == (False, True)


def test_events_for_other_locations_are_ignored(house):
    assert house.apply_event(change("door-1", "alarm", "alarm", True, location_id="elsewhere")) == (False, False)
    assert house.apply_event(alarm_change("ARMED_AWAY", location_id="elsewhere")) == (False, False)


def test_alarm_change(house):
    assert house.apply_event(alarm_change("ARM_AWAY_PENDING")) == (True, False)
    assert house.alarm == "arming"
    assert house.apply_event(alarm_change("ARMED_AWAY")) == (True, False)
    assert house.to_state(stream_connected=False)["alarm"] == "armed_away"


def test_snapshot_without_alarm_keeps_the_last_state(house):
    snapshot = home()
    del snapshot["alarmState"]
    house.apply_event(alarm_change("ARMED_NIGHT"))
    house.apply_snapshot(snapshot)
    assert house.alarm == "armed_night"


def test_removed_device_disappears(house):
    snapshot = home()
    snapshot["devices"] = [device for device in snapshot["devices"] if device["id"] != "meter-1"]
    house.apply_snapshot(snapshot)
    assert "meter-1" not in devices(house)


def test_malformed_events_are_ignored(house):
    assert house.apply_event({"type": "device-state-changed"}) == (False, False)
    assert house.apply_event({"type": "device-state-changed", "data": {"deviceId": "door-1", "changes": [{"value": 1}, "junk"]}}) == (False, False)
    assert house.apply_event({"type": "something-new", "data": {}}) == (False, False)


@pytest.mark.parametrize("raw, expected", [
    ("DISARMED", "disarmed"),
    ("ARMED_AWAY", "armed_away"),
    ("ARMED_NIGHT", "armed_night"),
    ("ARMED_PARTLY", "armed_home"),
    ("ARMED_STAY", "armed_home"),
    ("ARMED_NIGHT_PENDING", "arming"),
    ("ARM_NIGHT_PENDING", "arming"),
    ("ARM_PENDING", "arming"),
    ("ARM_STAY_PENDING", "arming"),
    ("ALARM_PENDING", "pending"),
    ("ALARM_STAY_PENDING", "pending"),
    ("BREACHED", "triggered"),
    ("ARMED_VACATION_PENDING", "arming"),
    ("ARMED_VACATION", "armed_home"),
    ("SOMETHING_ELSE", "something_else"),
    (None, None),
])
def test_normalize_alarm(raw, expected):
    assert normalize_alarm(raw) == expected


def test_split_area():
    assert split_area("Floor 1 - Kid's room") == (1, "Kid's room")
    assert split_area("Garage") == (None, "Garage")
    assert split_area(None) == (None, None)
