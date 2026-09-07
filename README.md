# 2n2mqtt

Bridge from a **2N** IP intercom or Access Unit to MQTT, with Home Assistant
discovery. Read-only: it reports tamper, door contact, other inputs, access
events and device health. It never opens a door.

It talks to the device over its documented HTTP API, which accepts **Digest
authentication only** — the reason a generic HTTP-polling integration usually
fails against these devices.

Two sources run side by side, because neither is enough on its own:

- the **event log** (`/api/log/subscribe` + `/api/log/pull`) delivers access
  events and input transitions as they happen, blocking so they arrive in real
  time where the firmware supports it;
- a **slow poll** of `/api/io/status` and `/api/system/status` re-reads the
  authoritative state, which covers inputs that emit no event at all and
  re-synchronises after a restart on either side.

## What it publishes

Every topic is retained except `access/event`, so a consumer that connects late
still sees the current state. `access/event` is deliberately **not** retained: a
retained event would be replayed as fresh on every reconnect, turning an old
rejected badge into a new alert.

```
<STATE_PREFIX>/availability      online | offline   (LWT: this bridge)
<STATE_PREFIX>/device/reachable  ON | OFF           (the device answers its API)
<STATE_PREFIX>/device/firmware
<STATE_PREFIX>/device/uptime     seconds
<STATE_PREFIX>/device/restarted  timestamp, written when uptime goes backwards
<STATE_PREFIX>/io/<port>         ON | OFF           (identifiers from /api/io/caps)
<STATE_PREFIX>/io/tamper         ON | OFF           (alias of IO_PORT_TAMPER)
<STATE_PREFIX>/io/door           ON | OFF           (alias of IO_PORT_DOOR)
<STATE_PREFIX>/access/user       last user, or an opaque id
<STATE_PREFIX>/access/time       ISO 8601
<STATE_PREFIX>/access/result     granted | denied
<STATE_PREFIX>/access/event      JSON, not retained
```

Home Assistant discovery is published under
`<DISCOVERY_PREFIX>/<component>/<DEVICE_ID>/<key>/config`.

## Before you start

Create a **dedicated API account** on the device (*Services → HTTP API →
Account*) with the **Monitoring** and **I/O** privileges, and nothing else.
Do not reuse an account another system already depends on, and do not grant
switch or control privileges: an account that can drive the relay can open the
door.

Then read `GET /api/io/caps` with that account and put the identifiers it
returns into `IO_PORT_TAMPER` / `IO_PORT_DOOR`. They vary by model and wiring,
so there is nothing sensible to guess here.

Before building anything on an input, check that it actually moves: open the
door and the enclosure while watching `/api/io/status`. An input that is not
wired produces a sensor that stays green forever, which is worse than no sensor.

## Configuration

Everything is configured through the environment; see `.env.example`.

| Variable | Default | Description |
|---|---|---|
| `TWON_HOST` | `192.0.2.10` | Hostname or IP of the device |
| `TWON_SCHEME` | `https` | `http` sends the credentials in the clear |
| `TWON_USER`, `TWON_PASS` | — | Read-only API account |
| `TWON_VERIFY_SSL` | `false` | 2N ships a self-signed certificate |
| `TWON_TIMEOUT` | `8` | HTTP timeout, seconds |
| `IO_POLL_INTERVAL` | `60` | Input re-read; values below 30 are clamped |
| `LOG_LONG_POLL_TIMEOUT` | `30` | Blocking pull; `0` disables long-polling |
| `LOG_POLL_INTERVAL` | `3` | Interval used when long-polling is disabled |
| `LOG_FILTER` | built-in list | Event types to subscribe to |
| `ACCESS_GRANTED_EVENTS` | `UserAuthenticated,AccessTaken` | Events counted as granted |
| `ACCESS_DENIED_EVENTS` | `UserRejected,AccessDenied,UnauthorizedDoorOpen` | Events counted as denied |
| `ENABLE_IO`, `ENABLE_ACCESS_LOG`, `ENABLE_DISCOVERY` | `true` | Feature switches |
| `IO_PORT_TAMPER`, `IO_PORT_DOOR` | — | Port ids from `/api/io/caps` |
| `IO_PORTS_EXTRA` | — | Further ports, comma-separated |
| `PUBLISH_USER_NAMES` | `true` | `false` publishes a stable opaque id instead of a name |
| `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASS`, `MQTT_CLIENT_ID` | — | Broker |
| `STATE_PREFIX` | `2n/<DEVICE_ID>` | Root of every state topic |
| `DISCOVERY_PREFIX` | `homeassistant` | Discovery root |
| `DEVICE_ID`, `DEVICE_NAME`, `DEVICE_MANUFACTURER`, `DEVICE_MODEL`, `SUGGESTED_AREA` | | Identity in discovery; model is auto-detected when empty |
| `DEVICE_NET_NAME`, `BROKER_NET_NAME`, `BRIDGE_IPV4`, `BRIDGE_IPV6` | — | Only read by `docker-compose.override.yml` |

Leaving `IO_PORT_TAMPER`, `IO_PORT_DOOR` and `IO_PORTS_EXTRA` all empty makes the
bridge publish every port the device reports.

If an endpoint answers with a privilege or licence error, that feature is
switched off for the rest of the run and logged once. Some 2N API groups require
a paid licence; this bridge uses only what the device already exposes and makes
no attempt to work around a restriction.

## Run

```bash
cp .env.example .env      # then edit it, and chmod 600 .env
docker compose up -d --build
docker logs -f 2n2mqtt
```

**Quote any password containing `$`.** Compose expands `$NAME` inside unquoted
`.env` values, so `TWON_PASS=$ecret1` reaches the container as an empty string
and every request comes back 401 while the file on disk looks correct. Single
quotes are taken literally:

```
TWON_PASS='$ecret1'
```

Check what actually arrived with `docker exec 2n2mqtt printenv TWON_PASS`.

If the device is not reachable over the default bridge network, or your broker
has its own internal network:

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
# fill DEVICE_NET_NAME / BROKER_NET_NAME / BRIDGE_IPV4 / BRIDGE_IPV6 in .env
```

## MQTT user

If your broker requires authentication, give this bridge its own account rather
than reusing one:

```bash
mosquitto_passwd -b <password_file> 2n2mqtt '<MQTT_PASS>'
kill -HUP <mosquitto_pid>          # or: docker exec <broker> kill -HUP 1
```

## Privacy

Access events name real people. The bridge keeps nothing on disk, and
`PUBLISH_USER_NAMES=false` publishes a stable opaque id instead of the name, so
you can still tell visits apart without broadcasting who they were.

## Disclaimer

Independent project, not affiliated with, endorsed by or supported by
2N Telekomunikace a.s. or Axis Communications. "2N" is a trademark of its
respective owner and is used here only to state which devices this software
talks to. No vendor firmware, SDK, code or documentation is redistributed;
refer to the official 2N HTTP API documentation for the endpoints themselves.

## License

MIT — see [LICENSE](LICENSE).
