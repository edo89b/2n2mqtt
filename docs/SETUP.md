# Setup

From a clean clone to a verified bridge. Settings are described in the
[README configuration table](../README.md#configuration) and in `.env.example`.

## Prerequisites

| # | What | Why |
|---|---|---|
| 1 | Docker Engine with Compose v2 (`docker compose`) | The image is built locally from the `Dockerfile` |
| 2 | An MQTT broker reachable from the container | Output; username/password optional |
| 3 | A 2N device with the HTTP API enabled, reachable over HTTPS from the container | Input |
| 4 | A dedicated API account on the device with the **Monitoring** and **I/O** privileges only | Read-only access; never reuse an account another system depends on |
| 5 | Optional: Home Assistant with the MQTT integration | Consumes the discovery topics |

Create the account in the device web UI under *Services → HTTP API →
Account*. Do not grant switch or control privileges: an account that can
drive the relay can open the door.

## Install

1. Clone and create the settings file:

   ```bash
   git clone https://github.com/edo89b/2n2mqtt.git
   cd 2n2mqtt
   cp .env.example .env && chmod 600 .env
   ```

2. Read the port ids of your model and wiring with the new account (curl
   prompts for the password, so it stays out of the shell history):

   ```bash
   curl -sk --digest -u '<account>' https://<device>/api/io/caps
   curl -sk --digest -u '<account>' https://<device>/api/log/caps
   ```

   The first gives the values for `IO_PORT_TAMPER`, `IO_PORT_DOOR` and
   `IO_PORTS_EXTRA`; the second the event names your firmware supports, for
   `LOG_FILTER` and the two `ACCESS_*_EVENTS` lists.

3. Fill in `.env`: at least `TWON_HOST`, `TWON_USER`, `TWON_PASS`,
   `MQTT_HOST`, and `MQTT_USER`/`MQTT_PASS` if the broker requires them.
   Set `ACCESS_DENIED_EVENTS=UserRejected,AccessBlocked,AccessLimited,UnauthorizedDoorOpen`
   (see trap 2 below). Quote any value containing `$` with single quotes.

4. If the default bridge network cannot reach the device, set up the
   networking override (next section).

5. Build and start:

   ```bash
   docker compose up -d --build
   ```

## Networking override

`docker-compose.yml` uses the default bridge network. When the device lives
on a network that bridge cannot reach (a macvlan on the device VLAN, for
example) or the broker has an internal network of its own, use the override:

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
```

- Compose merges `docker-compose.override.yml` automatically whenever it
  exists; delete the file to go back to the default network.
- The copy is gitignored because it describes one installation; the tracked
  template holds no deployment detail. Every value comes from `.env` through
  Compose interpolation: `DEVICE_NET_NAME`, `BROKER_NET_NAME`, `BRIDGE_IPV4`,
  `BRIDGE_IPV6`.
- Both networks are `external: true`: create them before `docker compose up`.
  If the device network has no IPv6, remove the `ipv6_address` line from
  your local copy.
- On a container attached to several networks, point `MQTT_HOST` at a name
  that resolves only on the broker network, otherwise the MQTT traffic may
  leave through the device network.
- `git pull` never touches the ignored copy: after an update, compare it with
  the template.

  ```bash
  diff docker-compose.override.yml.example docker-compose.override.yml
  ```

## Verify the installation

1. The container runs and is attached to the expected networks:

   ```bash
   docker compose ps
   docker inspect 2n2mqtt --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}'
   ```

2. The log shows a clean start (the values are yours):

   ```text
   [mqtt] <MQTT_HOST>:<MQTT_PORT> as <MQTT_USER>, base=<STATE_PREFIX>
   [2n] https://<TWON_HOST> as <TWON_USER>, io=on, log=on
   [io] publishing ports: [...]
   [discovery] <n> entities under homeassistant/
   [log] subscribed id=<id> filter=<LOG_FILTER>
   [2n] reachable -> online
   [log] backlog skipped, resuming after id=<id>
   ```

   ```bash
   docker compose logs --tail 50 2n2mqtt
   ```

   A `[config]` line means some classified events are not subscribed: fix
   it before going on.

3. The password reached the container, without printing it:

   ```bash
   docker exec 2n2mqtt sh -c 'test -n "$TWON_PASS" && echo set || echo EMPTY'
   ```

4. The topics arrive on the broker:

   ```bash
   mosquitto_sub -h <broker> -u <user> -P '<password>' -t '<STATE_PREFIX>/#' -v
   ```

5. Every input you rely on actually moves: open the door and the enclosure
   and watch for `[io] <port>: OFF -> ON` in the log. An input that is not
   wired produces a sensor that stays `OFF` forever, which is worse than no
   sensor.

6. In Home Assistant the device appears under the MQTT integration with the
   entities listed in [API.md](API.md#home-assistant-discovery).

## Troubleshooting

| # | Log line | Cause | Remedy |
|---|---|---|---|
| 1 | `[2n] authentication rejected: HTTP 401 ...` on every cycle | Wrong credentials, account without privileges, or `$` expanded by Compose | Fix `.env` (quote the password), then `docker compose up -d` |
| 2 | `[2n] unreachable: ...` | The container cannot reach the device, wrong `TWON_SCHEME`, TLS problem | Check the networking override and `TWON_HOST`; `TWON_VERIFY_SSL=false` for the self-signed certificate |
| 3 | `[io] disabled, the account cannot read inputs: ...` | Missing I/O privilege or licence | Grant the privilege, restart the container |
| 4 | `[log] access log disabled after a permanent error: ...` | The account or licence does not allow the event log | Same as row 3 |
| 5 | `[config] these access events are classified but not subscribed ...` | An `ACCESS_*_EVENTS` name is missing from `LOG_FILTER` or does not exist | Add it to `LOG_FILTER`, or remove it from the list |
| 6 | `[log] subscription lost, recreating: ...` | The device dropped the subscription (reboot) | None: automatic |
| 7 | `[io] publishing ports: none` | Auto mode and `/api/io/caps` returned nothing | Set `IO_PORT_*` explicitly |
| 8 | No `[discovery]` line and no `io/` topics after start | `/api/io/caps` failed on the first cycle in auto mode | Restart the container; setting `IO_PORT_*` avoids the call |

## Operational traps

One entry per failure that actually happened.

1. **Password containing `$`.** Compose expands `$NAME` inside unquoted
   `.env` values: `TWON_PASS=$ecret1` reaches the container empty and every
   request returns 401 while the file on disk looks right. Use single quotes
   (`TWON_PASS='$ecret1'`) and check with step 3 of the verification.
2. **Classified events that are never subscribed.** `UserRejected` was once
   classified as denied but missing from the default filter, so rejected
   badges never arrived. The bridge now warns at startup. `.env.example`
   still ships `ACCESS_DENIED_EVENTS` with `AccessDenied`, which
   `/api/log/caps` does not list on the verified firmware, and without
   `AccessBlocked`/`AccessLimited`: a fresh copy
   logs the `[config]` warning and does not count those two events as denied
   unless they carry a `valid` field. Use the value given in step 3 of Install.
3. **"Last access" stays unknown in Home Assistant.** `access/time` carries
   Unix epoch seconds while the entity is a `timestamp`; Home Assistant logs
   `Invalid state message '<epoch>' from '<STATE_PREFIX>/access/time'` on
   every badge. Known defect in the bridge; `access/event` has the same value
   for consumers that convert it.
4. **Isolated 401 with correct credentials.** A single
   `authentication rejected: HTTP 401 on /system/status` followed by
   `reachable -> online` on the next cycle has been observed with a working
   account. Availability goes `offline` for one poll interval; only a 401 on
   every cycle points to the credentials.
