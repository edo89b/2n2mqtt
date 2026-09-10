# 2n2mqtt

Read-only bridge from a **2N** IP intercom or Access Unit to MQTT, with Home
Assistant discovery. It reads the device through its documented HTTP API
(Digest authentication) and publishes tamper, door contact, other inputs,
access events and device health. It never opens a door and never drives an
output. Public open-source project, MIT licensed.

## Tech Stack

- Python 3.13 (`python:3.13-slim` base image), a single module: `2n2mqtt.py`
- `requests` 2.32.3 with `HTTPDigestAuth`: the 2N HTTP API accepts Digest only
- `paho-mqtt` 2.1.0, callback API version 2, retained LWT
- Docker Compose v2: one service, built locally from the `Dockerfile`
- Home Assistant MQTT discovery (optional, `ENABLE_DISCOVERY`)
- Developed and verified against a 2N Access Unit 2.0, firmware 2.46.1.70.4;
  event names differ between firmware families, which is why they are settings

## Directory Structure

```
2n2mqtt/
├── 2n2mqtt.py                           - the whole bridge: config, 2N client, MQTT, two worker threads
├── Dockerfile                           - image: pinned requirements + 2n2mqtt.py, runs `python -u`
├── docker-compose.yml                   - service `2n2mqtt`, default bridge network, env_file .env
├── docker-compose.override.yml.example  - optional networking template (tracked)
├── docker-compose.override.yml          - local copy of the template (gitignored, may be absent)
├── requirements.txt                     - pinned runtime dependencies
├── .env.example                         - every setting, with comments (tracked)
├── .env                                 - real settings and secrets (gitignored)
├── docs/                                - reference documentation (see Related documents)
├── scripts/
│   └── doc_check.py                     - documentation staleness check
├── .githooks/
│   └── pre-commit                       - runs the check before every commit
├── README.md                            - user-facing overview and configuration table
└── LICENSE                              - MIT
```

## Setup & Commands

```bash
cp .env.example .env && chmod 600 .env      # then fill it in: docs/SETUP.md
docker compose up -d --build                # build and start; also after any code or .env change
docker compose logs -f 2n2mqtt              # follow the log
docker compose down                         # stop and remove the container
python3 -m py_compile 2n2mqtt.py            # syntax check, needs no dependency
python3 scripts/doc_check.py                # documentation check (-v lists the documents)
```

- `docker compose restart` does not re-read `.env`: use `docker compose up -d`.
- There is no test suite and no CI. A change is verified against a real device
  following [docs/SETUP.md](docs/SETUP.md#verify-the-installation): the three
  faults fixed in commit `a1d2ae6` only showed up on real hardware.
- Discovery is published once at startup: restart the container to re-send it.

## Coding Conventions

Naming and structure:

- One module, top to bottom: settings, helpers, discovery, I/O polling, event
  log, `main()`. Keep it that way unless the file stops being readable.
- Settings are read once at import time with `os.environ.get(NAME, default)`
  into UPPER_CASE constants, grouped by prefix: `TWON_*` device, `MQTT_*`
  broker, `IO_*` ports and poll, `LOG_*` event log, `ENABLE_*` feature
  switches, `DEVICE_*` discovery identity. Booleans are `"true"`/`"false"`.
- Module-level mutable state is private and underscored (`_avail`,
  `_io_state`, `_unsubscribed`), guarded by a lock when two threads touch it.
- Functions are `snake_case`, exceptions `CamelCase` (`AuthError`, `ApiError`).
- A new setting goes into `.env.example` (with a comment) and into the README
  Configuration table in the same commit.

Recurring patterns (the code comments give the reason for each; do not undo
them without reading it):

- Every API call goes through `get()`: HTTP 401/403 raises `AuthError`; a body
  with `success: false` raises `ApiError` (the device answers HTTP 200 to
  application errors). `ApiError.permanent` (privilege, licence, unsupported)
  switches the feature off for the rest of the run: never retry it, never work
  around it.
- Availability is published only through `publish_avail()`, which writes on
  change and remembers the value; `on_connect()` re-asserts it after every
  reconnection, otherwise the retained LWT `offline` would stay forever.
- Inputs are published only through `publish_port()`: it adds the `tamper`/
  `door` role alias only when it differs from the port id, and logs
  transitions (not the periodic re-publish).
- States are retained; `access/event` is never retained, or an old rejected
  badge would be replayed as a fresh alert on every reconnect.
- The event log uses its own subscription with `include=all` (with
  `include=new` the device returns a valid id and never delivers anything);
  the backlog is skipped by tracking the last event id.
- A blocking pull passes `LOG_LONG_POLL + TIMEOUT` as HTTP read timeout: it
  must outlast the device's hold, or every quiet period looks like a failure.
- The subscription is recreated only when the device says it is invalid,
  never on a transport error (that would orphan a queue and lose its events).
- Worker threads wait with `stop.wait()`, never `time.sleep()`, and `main()`
  installs a SIGTERM handler: as PID 1 the process has none by default and
  `docker stop` would wait out its whole timeout.
- Exceptions are logged through `short()` (one line, at most 160 characters).
  Log lines start with a subsystem tag: `[mqtt]`, `[2n]`, `[io]`, `[log]`,
  `[access]`, `[discovery]`, `[config]`.
- Comments, docstrings and log text are in English and explain why, not what.

Anti-patterns (never acceptable):

- Calling an endpoint that drives a switch, a relay or an output, or asking
  for privileges beyond Monitoring and I/O: the bridge is read-only by design.
- Working around a licence or privilege restriction of the device.
- Lowering the `IO_POLL` floor of 30 s: the device also runs real access control.
- Guessing port ids or event names in code: they come from `/api/io/caps` and
  `/api/log/caps` and live in settings.
- Retaining `access/event`, or publishing availability outside `publish_avail()`.
- Catching an exception just to keep going without logging it.
- Hardcoding a deployment value (address, network, account) in a tracked file.
- Dependencies without an exact pin in `requirements.txt`.

Known technical debt (documented, not fixed yet):

- `access/time` carries the device's `utcTime` as sent (Unix epoch seconds on
  the verified firmware), while discovery declares it a `timestamp`: Home
  Assistant rejects every value ("Invalid state message").
- `.env.example` and the code disagree: `.env.example` sets
  `ACCESS_DENIED_EVENTS` with `AccessDenied`, which `/api/log/caps` does not
  list on the verified firmware, and without `AccessBlocked`/`AccessLimited`
  (the code default has them). See
  [docs/SETUP.md](docs/SETUP.md#operational-traps).
- Auto port mode (`IO_PORT_*` all empty): if `/api/io/caps` fails with a
  transport error on the first poll, `state["model"]` is already stored, so
  neither ports nor discovery are retried until the container restarts.
- With `IO_PORT_TAMPER=tamper` discovery creates two entities (`tamper` and
  `io_tamper`) on the same topic.
- Discovery never removes entities: after renaming `DEVICE_ID` or dropping a
  port, the old retained config topics stay on the broker.

## Git Workflow and Operating Rules

- Remote: `origin` on GitHub, public. History is linear on `main`: no other
  branches, no merge commits, no tags.
- Commit messages in English: imperative subject, capitalised, no trailing
  period, no type prefix (`Log input transitions`). The body explains why,
  wrapped at about 72 columns, one bullet per fault when a commit fixes several.
- Never commit `.env`, `.env.*` (only `.env.example` is tracked),
  `docker-compose.override.yml`, `*.bak`, `*.bak-*`: all gitignored. The
  repository is public: no real hostname, address, network name, account or
  person's name in tracked files or commit messages. Examples use the
  documentation range `192.0.2.0/24` and `changeme`.
- The published history contains no secret: keep it that way. A secret that
  reaches GitHub even once is rotated on the device or broker, not just
  removed in a later commit.
- No environments and no registry: a "release" is a push to `main`; each
  installation updates with `git pull` then `docker compose up -d --build`.
  After a pull, compare the local override with its template
  (`diff docker-compose.override.yml.example docker-compose.override.yml`):
  git never updates the ignored copy.
- Testing against a device: use a dedicated API account, never one another
  system depends on, and a separate `DEVICE_ID`/`STATE_PREFIX` if a production
  bridge publishes on the same broker (retained topics outlive the test).
- Documentation and code stay consistent: a change that makes a sentence of
  the documentation false fixes it in the same commit. `scripts/doc_check.py`
  catches broken references (names, paths, services that no longer exist), not
  descriptions that became false. When it fails, fix the document, do not
  silence the check; the `<!-- doc-check:ignore -->` markers are only for
  historical quotes and things not built yet.
- The check runs as a pre-commit hook. Enable it once per clone:

  ```bash
  git config core.hooksPath .githooks
  ```

  Run it by hand with `python3 scripts/doc_check.py`.

## Key Files & Directories

- `2n2mqtt.py`: everything; the module docstring summarises the design.
- `.env.example`: authoritative list of settings with defaults and comments.
- `docker-compose.yml` + optional `docker-compose.override.yml`: runtime;
  Compose merges the override automatically when the file exists.
- `Dockerfile`, `requirements.txt`: image; `.dockerignore` keeps `.env` and
  every compose file out of the build context.
- Tests: none. Documentation: `README.md` (users) and `docs/` (reference).

## Related Documents

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): threads, data flow, error
  model, design decisions and load on the device
- [docs/SETUP.md](docs/SETUP.md): installation, networking override,
  verification, troubleshooting, operational traps
- [docs/API.md](docs/API.md): MQTT topics, discovery entities, census of the
  2N HTTP API endpoints and events used
- [README.md](README.md): overview and full configuration table

## Tools and Integrations

- **2N HTTP API** (the device): HTTPS with a self-signed certificate, Digest
  auth, account with Monitoring and I/O privileges only; some API groups need
  a paid licence. Endpoint and event census: [docs/API.md](docs/API.md#2n-http-api-census).
- **MQTT broker**: any; username/password optional. Give the bridge its own
  broker account rather than sharing one.
- **Home Assistant**: consumes the discovery topics; nothing else is
  required on its side.

## Environment Variables and Secrets

- Everything is configured in `.env`, created from `.env.example`; the full
  table is in the [README](README.md#configuration).
- Required: `TWON_HOST`, `TWON_USER`, `TWON_PASS`, `MQTT_HOST`; plus
  `MQTT_USER`/`MQTT_PASS` when the broker requires authentication. Strongly
  recommended: `IO_PORT_TAMPER`/`IO_PORT_DOOR` from `GET /api/io/caps`.
- Secrets: `TWON_PASS` and `MQTT_PASS`, only in `.env` (`chmod 600`). The
  2N account is created in the device web UI (*Services → HTTP API →
  Account*); the MQTT account on your broker. Quote any value containing `$`.
- `DEVICE_NET_NAME`, `BROKER_NET_NAME`, `BRIDGE_IPV4`, `BRIDGE_IPV6` are read
  only by `docker-compose.override.yml`, through Compose interpolation.
- Excluded from git: `.env`, `.env.*` except `.env.example`, the override.
  Excluded from the image: `.env`, `.env.*`, every compose file (`.dockerignore`).
- To align a `.env` with a newer `.env.example` without printing any value:

  ```bash
  diff <(grep -o '^[A-Z0-9_]*=' .env.example | sort) <(grep -o '^[A-Z0-9_]*=' .env | sort)
  ```

- Access events name real people: `PUBLISH_USER_NAMES=false` publishes a
  stable opaque id instead. The bridge stores nothing on disk.
