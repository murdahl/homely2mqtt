# homely2mqtt

Publishes a Homely alarm system to MQTT: the alarm state, door and window sensors, motion, smoke, temperatures, batteries, and Yale Doorman locks. It uses the Homely SDK API. Changes arrive live over Homely's websocket, and a REST poll fills in anything the websocket missed.

The Homely API is read-only, so you can't arm the alarm or lock the door through it.

## Run

```sh
docker run -d --name homely2mqtt --restart unless-stopped \
  -e HOMELY_USER=you@example.com -e HOMELY_PASSWORD=secret \
  -e MQTT_HOST=192.168.1.10 -e MQTT_USERNAME=homely -e MQTT_PASSWORD=secret \
  ghcr.io/murdahl/homely2mqtt:latest
```

The account must be Owner or Admin of the location. If the login works but `/locations` doesn't, ask Homely support to enable API access for your account.

## Configuration

| Variable            | Default    | Description |
|---------------------|------------|-------------|
| `HOMELY_USER`       | (required) | Homely app email |
| `HOMELY_PASSWORD`   | (required) | Homely app password |
| `HOMELY_LOCATION`   |            | Location name or id. Only needed when the account has more than one location; run one bridge per location, each with its own `MQTT_TOPIC_PREFIX` |
| `MQTT_HOST`         | (required) | MQTT broker |
| `MQTT_PORT`         | `1883`     | |
| `MQTT_USERNAME`     |            | |
| `MQTT_PASSWORD`     |            | |
| `MQTT_TOPIC_PREFIX` | `homely`   | |
| `POLL_INTERVAL`     | `300`      | Seconds between REST polls (minimum 30). The bridge polls every 60 s while the websocket is down |
| `LOG_LEVEL`         | `INFO`     | |

## Topics

| Topic           | Retained | Payload |
|-----------------|----------|---------|
| `homely/status` | yes      | `online` / `offline`. Goes `offline` when Homely can't be reached (the poll fails and the websocket is down), or the bridge stops |
| `homely/state`  | yes      | JSON, see below. Republished on every change |

```json
{
  "location": "Home",
  "location_id": "3f2c9a10-…",
  "gateway": "0200000000000001",
  "alarm": "disarmed",
  "alarm_raw": "DISARMED",
  "stream": "connected",
  "devices": [
    {"id": "a1b2c3d4-…", "name": "Inngangsdør", "type": "entry", "model": "Alarm Entry Sensor 2",
     "area": "Floor 1 - Entrance", "floor": 1, "room": "Entrance", "online": true, "updated": "2026-09-30T05:47:46.170Z",
     "open": false, "tamper": false, "temperature": 21.5, "battery_low": false, "battery_defect": false,
     "battery_voltage": 3, "link_strength": 98},
    {"id": "e5f6a7b8-…", "name": "Yale Doorman V2N", "type": "lock", "area": "Floor 1 - Entrance", "floor": 1, "room": "Entrance",
     "online": true, "locked": true, "door_closed": true, "lock_event": "DOORLOCK_MANUAL_LOCK", "battery_low": false}
  ]
}
```

- **`alarm`:** in Home Assistant's vocabulary.
  - `disarmed`, `armed_away`, `armed_night` and `armed_home` (Homely's "partly" / "stay");
  - `arming` (exit delay);
  - `pending` (entry delay);
  - `triggered`.

  `alarm_raw` is Homely's own value.
- **`stream`:** whether the live websocket is connected. While it's `disconnected`, changes can take up to a minute.
- **`type`:** `entry`, `motion`, `smoke`, `flood`, `lock`, `meter`, `thermostat` or `other`.
- **Values:** only the ones a device has are included.

  | Value | Meaning |
  |---|---|
  | `open` | Entry sensors: the door or window is open |
  | `motion` | Motion sensors |
  | `fire`, `flood`, `tamper` | Alarm inputs |
  | `temperature` | °C |
  | `battery_low`, `battery_defect`, `battery_voltage` | Battery health |
  | `link_strength` | Radio link to the gateway, % |
  | `locked`, `door_closed`, `lock_event` | Yale locks |
  | `power` (W), `energy_imported`, `energy_exported` (kWh) | HAN meters |
- **`online`:** per device. It comes from the REST poll, since the websocket doesn't report it.
- **`updated`:** the newest change of any of the device's values.

## How it stays current

- **Tokens:**
  - The access token is renewed a minute before it expires.
  - If a refresh is refused, it logs in again.
  - A request rejected with 401 gets a fresh login and one retry.
- **Websocket:**
  - Every connection attempt uses a fresh token.
  - Reconnects back off: 10 s three times, then 60 s five times, then every 5 minutes.
  - A dead connection is noticed within about 45 s through Socket.IO's ping.
- **Catch-up:**
  - After every (re)connect, the bridge polls REST so no change is lost.
  - An update for a device it doesn't know yet also triggers a poll.
- **Order:** each value keeps Homely's timestamp, so an older value never overwrites a newer one.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

## Credits

- **API behaviour:** the token lifetimes, the websocket connection details and the alarm state names build on the research in [ludvikroed/homely-integration](https://github.com/ludvikroed/homely-integration) and [python-homely](https://github.com/ludvikroed/python-homely) (both MIT).
- **Inspiration:** [yusijs/homely-mqtt](https://github.com/yusijs/homely-mqtt).

This is an unofficial client, and Homely can change the API at any time.
