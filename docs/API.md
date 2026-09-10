# Interfaces: MQTT, Home Assistant discovery, 2N HTTP API

The bridge has no API of its own: it consumes the 2N HTTP API and exposes
MQTT topics. Topic roots in this document:

- `<STATE_PREFIX>`: `STATE_PREFIX`, default `2n/<DEVICE_ID>`
- `<DISCOVERY_PREFIX>`: `DISCOVERY_PREFIX`, default `homeassistant`

## MQTT topics

Binary payloads are `ON`/`OFF`. Nothing is ever subscribed: the bridge only
publishes.

| # | Topic under `<STATE_PREFIX>` | Payload | Retained | QoS | Published |
|---|---|---|---|---|---|
| 1 | `availability` | `online` / `offline` | yes | 1 | On change; re-asserted on every MQTT (re)connect. `offline` is also the LWT and the last message on shutdown |
| 2 | `device/reachable` | `ON` / `OFF` | yes | 0 | Every I/O cycle: `ON` after a successful cycle, `OFF` on an authentication or transport failure, unchanged on an application error |
| 3 | `device/firmware` | `swVersion` of `/api/system/info` | yes | 0 | Every I/O cycle |
| 4 | `device/uptime` | seconds, `upTime` of `/api/system/status` | yes | 0 | Every I/O cycle |
| 5 | `device/restarted` | container-local time, `%Y-%m-%dT%H:%M:%S%z` | yes | 1 | When uptime goes backwards between two cycles |
| 6 | `io/<port>` | `ON` / `OFF` | yes | 0 | Every I/O cycle for each published port, and on `InputChanged` |
| 7 | `io/tamper` | `ON` / `OFF` | yes | 0 | Alias of `IO_PORT_TAMPER` (not repeated when the port id already is `tamper`); forced `ON` by `TamperSwitchActivated` |
| 8 | `io/door` | `ON` / `OFF` | yes | 0 | Alias of `IO_PORT_DOOR`; also set by `DoorStateChanged` |
| 9 | `access/user` | name, else the event `uuid`, else empty; opaque id with `PUBLISH_USER_NAMES=false` | yes | 0 | Every classified access event |
| 10 | `access/time` | the event `utcTime` (fallback `time`) exactly as the device sends it | yes | 0 | Every classified access event |
| 11 | `access/result` | `granted` / `denied` | yes | 0 | Every classified access event |
| 12 | `access/event` | JSON: `event`, `time`, `user`, `result`, `direction` | **no** | 1 | Every classified access event |

Notes from the verified firmware (Access Unit 2.0, 2.46.1.70.4):

- `access/time` is Unix epoch seconds, not ISO 8601. The discovery entity
  declares `device_class: timestamp`, so Home Assistant rejects every value
  (known defect, see [SETUP.md](SETUP.md#operational-traps)). Consumers that
  need the time should read `access/event` and convert the epoch themselves.
- One badge produces two access events with the same time: `CardEntered`
  (classified by its `valid` field; `user` is a UUID) and then
  `UserAuthenticated` (`user` is the name). Count badges on one of the two
  event names, not on every message.
- `DoorStateChanged` is read as open for `open`, `opened`, `1` or `true`;
  anything else is closed.

## Home Assistant discovery

Config topic: `<DISCOVERY_PREFIX>/<component>/<DEVICE_ID>/<key>/config`,
retained, QoS 1. `unique_id` and `object_id` are `<DEVICE_ID>_<key>`; every
entity uses `<STATE_PREFIX>/availability` as availability topic and shares
one device block: `identifiers` = `DEVICE_ID`, `name`, `manufacturer`,
`model` (`DEVICE_MODEL`, else `variant` or `devType` from
`/api/system/info`, else `unknown`), `suggested_area` when set.

| # | Key | Component | Name | Device class / icon | State topic | Present when |
|---|---|---|---|---|---|---|
| 1 | `reachable` | binary_sensor | Reachable | connectivity | `device/reachable` | always |
| 2 | `firmware` | sensor | Firmware | `mdi:chip` | `device/firmware` | always |
| 3 | `uptime` | sensor | Uptime | duration, unit `s`, state class measurement | `device/uptime` | always |
| 4 | `tamper` | binary_sensor | Tamper | tamper | `io/tamper` | `IO_PORT_TAMPER` set |
| 5 | `door` | binary_sensor | Door | door | `io/door` | `IO_PORT_DOOR` set |
| 6 | `io_<port>` | binary_sensor | Input `<port>` | `mdi:electric-switch` | `io/<port>` | one per published port |
| 7 | `access_user` | sensor | Last user | `mdi:account` | `access/user` | `ENABLE_ACCESS_LOG=true` |
| 8 | `access_time` | sensor | Last access | timestamp | `access/time` | `ENABLE_ACCESS_LOG=true` |
| 9 | `access_result` | sensor | Last result | `mdi:key` | `access/result` | `ENABLE_ACCESS_LOG=true` |

`device/restarted` has no entity. Discovery is sent when the first
`/api/system/info` succeeds and again only if the detected model changes.
The bridge never deletes a config topic: to drop a stale entity, publish an
empty retained payload on its old config topic.

## 2N HTTP API census

Rule: before writing code that calls a new endpoint, adds a parameter or
handles a new event, add it to this census **in the same commit, first**.
Only read endpoints belong here; an endpoint that drives a switch, a relay or
an output is out of scope for this project.

All requests are `GET` on `<TWON_SCHEME>://<TWON_HOST>/api...` with Digest
authentication, timeout `TWON_TIMEOUT`, TLS verification per
`TWON_VERIFY_SSL`. The answer is `{"success": true, "result": {...}}` or
`{"success": false, "error": {"code": ..., "description": ...}}`, always with
HTTP 200 for application errors. The account needs the **Monitoring** and
**I/O** privileges.

| # | Endpoint | Parameters | Called | Fields used |
|---|---|---|---|---|
| 1 | `/api/system/info` | none | every I/O cycle | `variant`, `devType` (model), `swVersion` |
| 2 | `/api/system/status` | none | every I/O cycle | `upTime` |
| 3 | `/api/io/caps` | none | first cycle, only when no `IO_PORT_*` is set | `ports[].port` |
| 4 | `/api/io/status` | `port` | every I/O cycle, once per port | `ports[0].state` |
| 5 | `/api/log/subscribe` | `include=all`, `filter=<LOG_FILTER>` | start, and when the device reports the subscription invalid | `id` |
| 6 | `/api/log/pull` | `id`, `timeout=<LOG_LONG_POLL_TIMEOUT>` when blocking | continuously | `events[]`: `id`, `event`, `utcTime`, `time`, `params` |
| 7 | `/api/log/unsubscribe` | `id` | on shutdown, own subscription only | none |

Not called by the bridge, but used to configure it: `/api/io/caps` (port
ids for `IO_PORT_*`) and `/api/log/caps` (event names the firmware supports).

An error is permanent, and switches the feature off for the run, when its
description contains `privilege`, `licen`, `not supported` or `unsupported`.

### Events

Default `LOG_FILTER`: `DeviceState`, `TamperSwitchActivated`,
`UnauthorizedDoorOpen`, `DoorStateChanged`, `DoorOpenTooLong`,
`InputChanged`, `RexActivated`, `UserAuthenticated`, `UserRejected`,
`AccessTaken`, `AccessBlocked`, `AccessLimited`, `CardEntered`. A
subscription without a filter covers nothing, so an empty `LOG_FILTER` means
this list.

| # | Event | Effect |
|---|---|---|
| 1 | `InputChanged` | `io/<port>` updated at once, ahead of the next poll |
| 2 | `TamperSwitchActivated` | logged; `io/tamper` set `ON` when `IO_PORT_TAMPER` is set |
| 3 | `DoorStateChanged` | `io/door` updated when `IO_PORT_DOOR` is set |
| 4 | any event with a `valid` param | access event: `granted` if true, `denied` if false |
| 5 | name in `ACCESS_GRANTED_EVENTS` (default `UserAuthenticated,AccessTaken`) | access event, `granted` |
| 6 | name in `ACCESS_DENIED_EVENTS` (default `UserRejected,AccessBlocked,AccessLimited,UnauthorizedDoorOpen`) | access event, `denied` |
| 7 | anything else (`DeviceState`, `DoorOpenTooLong`, `RexActivated`, ...) | ignored |

Rows 4 to 6 are evaluated in that order; rows 1 to 3 also go through them.
An event named in the two `ACCESS_*_EVENTS` lists but absent from
`LOG_FILTER` is never delivered: the bridge warns about it at startup.
