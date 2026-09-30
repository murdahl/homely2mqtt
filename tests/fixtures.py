import copy

LOCATION_ID = "loc-1"


def reading(value, updated="2026-09-30T05:00:00.000Z"):
    return {"value": value, "lastUpdated": updated}


def device(id, name, location, model, features, online=True):
    return {
        "id": id,
        "name": name,
        "serialNumber": "0015BC0000000000",
        "location": location,
        "online": online,
        "modelId": "model",
        "modelName": model,
        "features": {feature: {"states": states} for feature, states in features.items()},
    }


COMMON = {
    "temperature": {"temperature": reading(21.5)},
    "battery": {"low": reading(False), "defect": reading(False), "voltage": reading(3)},
    "diagnostic": {"networklinkstrength": reading(98), "networklinkaddress": reading("0015BC0000000001")},
}

# Shaped like a real GET /home/{locationId} response
_HOME = {
    "locationId": LOCATION_ID,
    "gatewayserial": "0200000000000001",
    "name": "Home",
    "alarmState": "DISARMED",
    "userRoleAtLocation": "OWNER",
    "devices": [
        device("motion-1", "Bevegelsessensor", "Floor 2 - Living room", "Alarm Motion Sensor 2", {
            "setup": {"appledenable": reading(False)},
            "alarm": {"alarm": reading(True), "tamper": reading(False), "sensitivitylevel": reading(3)},
            **COMMON,
        }),
        device("smoke-1", "Brannalarm", "Floor 1 - Kitchen", "Intelligent Smoke Alarm", {
            "alarm": {"fire": reading(False)},
            **COMMON,
        }),
        device("door-1", "Inngangsdør", "Floor 1 - Entrance", "Alarm Entry Sensor 2", {
            "alarm": {"alarm": reading(False), "tamper": reading(False)},
            **COMMON,
        }),
        device("lock-1", "Yale Doorman V2N", "Floor 1 - Entrance", "Yale Doorman V2N", {
            "lock": {"state": reading(True, "2026-09-30T05:47:47.775Z"), "soundvolume": reading(1)},
            "report": {
                "event": reading("DOORLOCK_MANUAL_LOCK", "2026-09-30T05:47:47.688Z"),
                "added": {},
                "locked": reading(True, "2026-09-30T05:47:47.707Z"),
                "doorclosed": reading(True, "2026-09-30T05:47:46.360Z"),
                "lowbat": reading(False, "2026-09-22T14:49:33.544Z"),
            },
        }),
        device("meter-1", "HAN", "Technical room", "EMI Norwegian HAN", {
            "metering": {"summationdelivered": reading(1234567), "summationreceived": reading(0), "demand": reading(1830)},
        }, online=False),
    ],
}


def home():
    return copy.deepcopy(_HOME)


def change(device_id, feature, state, value, updated="2026-09-30T06:00:00.000Z", location_id=LOCATION_ID):
    return {
        "type": "device-state-changed",
        "data": {
            "deviceId": device_id,
            "gatewayId": "gw",
            "locationId": location_id,
            "modelId": "model",
            "rootLocationId": location_id,
            "changes": [{"feature": feature, "stateName": state, "value": value, "lastUpdated": updated}],
        },
    }


def alarm_change(state, location_id=LOCATION_ID):
    return {
        "type": "alarm-state-changed",
        "data": {"locationId": location_id, "state": state, "userId": "u", "userName": "Me", "timestamp": "2026-09-30T06:00:00.000Z"},
    }
