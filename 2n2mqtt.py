#!/usr/bin/env python3
"""Publish 2N intercom / Access Unit state to MQTT, with Home Assistant discovery.

The device is read through its documented HTTP API. Two sources run side by side
because neither is sufficient on its own:

  * the event log (/api/log/subscribe + /api/log/pull) delivers access events and
    input transitions as they happen, optionally blocking so they arrive in real
    time;
  * a slow poll of /api/io/status and /api/system/status re-reads the authoritative
    state, which covers inputs that emit no event at all and re-synchronises after
    a restart on either side.

Everything is retained except the raw access event: a retained event would be
replayed as fresh the next time a consumer connects, turning an old rejected badge
into a new alert.

Only endpoints the device already exposes are used. When one answers with a
privilege or licence error the corresponding feature is switched off for the rest
of the run instead of being retried or worked around.
"""
import hashlib
import json
import os
import signal
import threading
import time

import requests
from requests.auth import HTTPDigestAuth
import paho.mqtt.client as mqtt

# --- Device -----------------------------------------------------------------
HOST = os.environ.get("TWON_HOST", "192.0.2.10")
SCHEME = os.environ.get("TWON_SCHEME", "https").lower()
USER = os.environ.get("TWON_USER", "")
PASSWORD = os.environ.get("TWON_PASS", "")
# 2N devices ship a self-signed certificate, so verification is off by default.
VERIFY_SSL = os.environ.get("TWON_VERIFY_SSL", "false").lower() == "true"
TIMEOUT = int(os.environ.get("TWON_TIMEOUT", "8"))

# --- Polling ----------------------------------------------------------------
# This is an embedded device that also runs real access control: do not hammer it.
IO_POLL = max(30, int(os.environ.get("IO_POLL_INTERVAL", "60")))
# /api/log/pull can block until an event shows up. 0 falls back to short polling.
LOG_LONG_POLL = int(os.environ.get("LOG_LONG_POLL_TIMEOUT", "30"))
LOG_POLL = int(os.environ.get("LOG_POLL_INTERVAL", "3"))

# A subscription without an explicit filter covers no event type at all, so a
# default list is always sent. Trim or extend it from GET /api/log/caps.
LOG_FILTER = os.environ.get("LOG_FILTER", "").strip() or ",".join([
    "DeviceState",
    "TamperSwitchActivated",
    "UnauthorizedDoorOpen",
    "DoorStateChanged",
    "DoorOpenTooLong",
    "InputChanged",
    "RexActivated",
    "UserAuthenticated",
    "UserRejected",
    "AccessTaken",
    "AccessBlocked",
    "AccessLimited",
    "CardEntered",
])

# Event names differ between firmware families; keep them configurable rather
# than guessing in code.
GRANTED_EVENTS = {e for e in os.environ.get(
    "ACCESS_GRANTED_EVENTS", "UserAuthenticated,AccessTaken").split(",") if e}
DENIED_EVENTS = {e for e in os.environ.get(
    "ACCESS_DENIED_EVENTS",
    "UserRejected,AccessBlocked,AccessLimited,UnauthorizedDoorOpen").split(",") if e}

# Whatever is named in GRANTED_EVENTS or DENIED_EVENTS has to appear in
# LOG_FILTER too, otherwise the subscription simply never delivers it and the
# classification below is never reached.
_unsubscribed = (GRANTED_EVENTS | DENIED_EVENTS) - set(LOG_FILTER.split(","))

# --- Features ---------------------------------------------------------------
ENABLE_IO = os.environ.get("ENABLE_IO", "true").lower() == "true"
ENABLE_ACCESS_LOG = os.environ.get("ENABLE_ACCESS_LOG", "true").lower() == "true"
ENABLE_DISCOVERY = os.environ.get("ENABLE_DISCOVERY", "true").lower() == "true"

# --- I/O ports --------------------------------------------------------------
# Real identifiers come from GET /api/io/caps and vary by model and wiring.
# Leave all three empty to publish every port the device reports.
PORT_TAMPER = os.environ.get("IO_PORT_TAMPER", "").strip()
PORT_DOOR = os.environ.get("IO_PORT_DOOR", "").strip()
PORTS_EXTRA = [p.strip() for p in os.environ.get("IO_PORTS_EXTRA", "").split(",") if p.strip()]

# --- Privacy ----------------------------------------------------------------
# Access events carry the name of a real person. Turning this off publishes a
# stable opaque id instead, so the topics stay useful without naming anyone.
PUBLISH_USER_NAMES = os.environ.get("PUBLISH_USER_NAMES", "true").lower() == "true"

# --- MQTT -------------------------------------------------------------------
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "2n2mqtt")

DEVICE_ID = os.environ.get("DEVICE_ID", "2n_access_unit")
DEVICE_NAME = os.environ.get("DEVICE_NAME", "2N Access Unit")
DEVICE_MANUFACTURER = os.environ.get("DEVICE_MANUFACTURER", "2N")
DEVICE_MODEL = os.environ.get("DEVICE_MODEL", "")  # auto-detected when empty
SUGGESTED_AREA = os.environ.get("SUGGESTED_AREA", "")

DISCOVERY_PREFIX = os.environ.get("DISCOVERY_PREFIX", "homeassistant").rstrip("/")
BASE = os.environ.get("STATE_PREFIX", f"2n/{DEVICE_ID}").rstrip("/")

# Topic layout, one branch per kind of thing:
#   <BASE>/availability
#   <BASE>/device/<key>
#   <BASE>/io/<port>          plus the role aliases io/tamper and io/door
#   <BASE>/access/<key>
AVAIL = f"{BASE}/availability"
TOPIC_DEVICE = f"{BASE}/device"
TOPIC_IO = f"{BASE}/io"
TOPIC_ACCESS = f"{BASE}/access"

API = f"{SCHEME}://{HOST}/api"


def log(*a):
    print(*a, flush=True)


def short(e, limit=160):
    """One-line, trimmed exception text.

    urllib3 spells out the whole request URL inside connection errors, which
    turns a device being unplugged into several hundred bytes per retry.
    """
    text = " ".join(str(e).split())
    return text if len(text) <= limit else text[:limit] + "..."


# Last availability asserted on the broker. Module level so on_connect() can
# re-assert it after a reconnection (see publish_avail).
_avail = {"state": None}
_avail_lock = threading.Lock()


def publish_avail(client, state):
    """Publish the retained availability, only when it actually changes.

    Returns True if the value changed, so the caller can log the transition.

    The last asserted value is remembered because the LWT is retained: when the
    connection drops the broker publishes a retained "offline". paho reconnects
    on its own, but without this bookkeeping nothing would ever overwrite that
    retained "offline" and every consumer would keep seeing the bridge as down.
    """
    with _avail_lock:
        if _avail["state"] == state:
            return False
        _avail["state"] = state
    client.publish(AVAIL, state, qos=1, retain=True)
    return True


def on_connect(client, userdata, flags, rc, properties=None):
    """Re-assert the current availability on every successful (re)connection."""
    with _avail_lock:
        state = _avail["state"]
    if state is not None:
        client.publish(AVAIL, state, qos=1, retain=True)
        log(f"[mqtt] (re)connected -> re-asserted {state}")


if not VERIFY_SSL:
    requests.packages.urllib3.disable_warnings()

api = requests.Session()
api.auth = HTTPDigestAuth(USER, PASSWORD)
api.verify = VERIFY_SSL


class AuthError(Exception):
    """The device rejected the credentials, or none were supplied.

    Kept apart from a transport failure: both stop the data flowing, but they
    send you looking in completely different places.
    """


class ApiError(Exception):
    """An application-level failure reported inside the JSON body."""

    def __init__(self, err):
        self.code = err.get("code")
        self.description = str(err.get("description", ""))
        super().__init__(f"code={self.code} {self.description}")

    @property
    def permanent(self):
        """True when retrying cannot help: missing privilege, or missing licence.

        Features that hit one of these are switched off for the rest of the run.
        The bridge never tries to work around a licence restriction.
        """
        text = self.description.lower()
        return any(k in text for k in ("privilege", "licen", "not supported", "unsupported"))


def get(path, _timeout=None, **params):
    """GET an API endpoint and return its `result`.

    The device answers HTTP 200 even for application errors, so the `success`
    field is the real outcome and is what decides here.
    """
    r = api.get(f"{API}{path}", params=params or None, timeout=_timeout or TIMEOUT)
    if r.status_code in (401, 403):
        raise AuthError(f"HTTP {r.status_code} on {path}: check TWON_USER / TWON_PASS "
                        f"and that the account has the Monitoring and I/O privileges")
    r.raise_for_status()
    body = r.json()
    if not body.get("success"):
        raise ApiError(body.get("error") or {})
    return body.get("result") or {}


def opaque(value):
    """Short stable id standing in for a name when names must not be published."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Home Assistant discovery
# ---------------------------------------------------------------------------

def device_block(model):
    dev = {
        "identifiers": [DEVICE_ID],
        "name": DEVICE_NAME,
        "manufacturer": DEVICE_MANUFACTURER,
        "model": model or "unknown",
    }
    if SUGGESTED_AREA:
        dev["suggested_area"] = SUGGESTED_AREA
    return dev


def entity_config(dev, key, label, state_topic, component="sensor",
                  dclass=None, icon=None, extra=None):
    obj = f"{DEVICE_ID}_{key}"
    payload = {
        "name": label,
        "state_topic": state_topic,
        "unique_id": obj,
        "object_id": obj,
        "device": dev,
        "availability_topic": AVAIL,
    }
    if dclass:
        payload["device_class"] = dclass
    if icon:
        payload["icon"] = icon
    if extra:
        payload.update(extra)
    return f"{DISCOVERY_PREFIX}/{component}/{DEVICE_ID}/{key}/config", json.dumps(payload)


def publish_discovery(client, model, ports):
    if not ENABLE_DISCOVERY:
        return
    dev = device_block(model)
    binary = {"payload_on": "ON", "payload_off": "OFF"}

    entities = [
        ("reachable", "Reachable", f"{TOPIC_DEVICE}/reachable", "binary_sensor",
         "connectivity", None, binary),
        ("firmware", "Firmware", f"{TOPIC_DEVICE}/firmware", "sensor",
         None, "mdi:chip", None),
        ("uptime", "Uptime", f"{TOPIC_DEVICE}/uptime", "sensor",
         "duration", None, {"unit_of_measurement": "s", "state_class": "measurement"}),
    ]
    if PORT_TAMPER:
        entities.append(("tamper", "Tamper", f"{TOPIC_IO}/tamper", "binary_sensor",
                         "tamper", None, binary))
    if PORT_DOOR:
        entities.append(("door", "Door", f"{TOPIC_IO}/door", "binary_sensor",
                         "door", None, binary))
    for port in ports:
        entities.append((f"io_{port}", f"Input {port}", f"{TOPIC_IO}/{port}",
                         "binary_sensor", None, "mdi:electric-switch", binary))
    if ENABLE_ACCESS_LOG:
        entities += [
            ("access_user", "Last user", f"{TOPIC_ACCESS}/user", "sensor",
             None, "mdi:account", None),
            ("access_time", "Last access", f"{TOPIC_ACCESS}/time", "sensor",
             "timestamp", None, None),
            ("access_result", "Last result", f"{TOPIC_ACCESS}/result", "sensor",
             None, "mdi:key", None),
        ]

    for key, label, topic, component, dclass, icon, extra in entities:
        client.publish(*entity_config(dev, key, label, topic, component, dclass, icon, extra),
                       qos=1, retain=True)
    log(f"[discovery] {len(entities)} entities under {DISCOVERY_PREFIX}/")


# ---------------------------------------------------------------------------
# I/O and system polling
# ---------------------------------------------------------------------------

def wanted_ports():
    """Ports to publish: the configured ones, or every port the device reports."""
    configured = [p for p in ([PORT_TAMPER, PORT_DOOR] + PORTS_EXTRA) if p]
    if configured:
        return list(dict.fromkeys(configured))
    ports = [p.get("port") for p in get("/io/caps").get("ports", [])]
    return [p for p in ports if p]


# Last state published per port, so a transition can be logged without turning
# the periodic re-publish into one log line a minute per input.
_io_state = {}


def publish_port(client, port, state):
    """Publish a port under its own id, and under its role name when it has one.

    A device may already call its input by the role name — the tamper input
    usually does — in which case the two topics are the same one and publishing
    it twice would only duplicate the traffic.

    Transitions are logged. An input that changes between two polls is otherwise
    only visible as a retained value that has already moved on, which makes
    "did this input ever move?" impossible to answer after the fact — the one
    question worth asking of a door contact or a tamper switch.
    """
    if _io_state.get(port) != state:
        if port in _io_state:
            log(f"[io] {port}: {_io_state[port]} -> {state}")
        _io_state[port] = state
    client.publish(f"{TOPIC_IO}/{port}", state, qos=0, retain=True)
    for role, mapped in (("tamper", PORT_TAMPER), ("door", PORT_DOOR)):
        if port == mapped and port != role:
            client.publish(f"{TOPIC_IO}/{role}", state, qos=0, retain=True)


def publish_io(client, ports):
    """Read every wanted port and publish it, plus the tamper/door role aliases.

    Publishing under the role name as well as the raw port id keeps consumers
    independent of how this particular device names its inputs.
    """
    for port in ports:
        try:
            res = get("/io/status", port=port)
        except ApiError as e:
            if e.permanent:
                raise
            log(f"[io] {port}: {e}")
            continue
        entries = res.get("ports") or []
        if not entries:
            continue
        state = "ON" if entries[0].get("state") else "OFF"
        publish_port(client, port, state)


def io_loop(client, state, stop):
    """Slow authoritative re-read of inputs and system status."""
    ports = []
    io_enabled = ENABLE_IO
    while not stop.is_set():
        try:
            info = get("/system/info")
            model = DEVICE_MODEL or info.get("variant") or info.get("devType", "")
            if state.get("model") != model:
                state["model"] = model
                if io_enabled and not ports:
                    try:
                        ports = wanted_ports()
                        log(f"[io] publishing ports: {ports or 'none'}")
                    except ApiError as e:
                        if e.permanent:
                            io_enabled = False
                            log(f"[io] disabled, the account cannot read inputs: {e}")
                        else:
                            log(f"[io] caps: {e}")
                publish_discovery(client, model, ports)
            client.publish(f"{TOPIC_DEVICE}/firmware", info.get("swVersion", ""),
                           qos=0, retain=True)

            uptime = get("/system/status").get("upTime")
            if uptime is not None:
                previous = state.get("uptime")
                if previous is not None and uptime < previous:
                    log(f"[2n] unexpected restart: uptime went {previous} -> {uptime}")
                    client.publish(f"{TOPIC_DEVICE}/restarted",
                                   time.strftime("%Y-%m-%dT%H:%M:%S%z"), qos=1, retain=True)
                state["uptime"] = uptime
                client.publish(f"{TOPIC_DEVICE}/uptime", uptime, qos=0, retain=True)

            if io_enabled and ports:
                publish_io(client, ports)

            client.publish(f"{TOPIC_DEVICE}/reachable", "ON", qos=0, retain=True)
            if publish_avail(client, "online"):
                log("[2n] reachable -> online")
        except AuthError as e:
            client.publish(f"{TOPIC_DEVICE}/reachable", "OFF", qos=0, retain=True)
            if publish_avail(client, "offline"):
                log(f"[2n] authentication rejected: {e}")
        except ApiError as e:
            if e.permanent:
                io_enabled = False
                log(f"[io] disabled after a permanent error: {e}")
            else:
                log(f"[2n] api error: {e}")
        except Exception as e:
            client.publish(f"{TOPIC_DEVICE}/reachable", "OFF", qos=0, retain=True)
            if publish_avail(client, "offline"):
                log(f"[2n] unreachable: {short(e)}")
        stop.wait(IO_POLL)


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------

def subscribe():
    """Create our own log subscription.

    include=all is deliberate: with include=new the device accepts the
    subscription, returns a valid id and never delivers a single event. Already
    seen events are dropped by tracking the last id instead.

    Each subscription owns an independent queue, so this never steals events
    from another consumer of the same device.
    """
    res = get("/log/subscribe", include="all", filter=LOG_FILTER)
    sid = res.get("id")
    log(f"[log] subscribed id={sid} filter={LOG_FILTER}")
    return sid


def pull(sid, blocking):
    """One page of events.

    A blocking pull is held open by the device for up to LOG_LONG_POLL seconds,
    so the HTTP read timeout has to outlast it — with the ordinary timeout every
    quiet period would look like a failure.
    """
    params = {"id": sid}
    timeout = None
    if blocking and LOG_LONG_POLL > 0:
        params["timeout"] = LOG_LONG_POLL
        timeout = LOG_LONG_POLL + TIMEOUT
    return get("/log/pull", _timeout=timeout, **params).get("events") or []


def classify(event_name, params):
    """granted / denied / None, from the event name and its own fields."""
    if "valid" in params:
        return "granted" if params.get("valid") else "denied"
    if event_name in GRANTED_EVENTS:
        return "granted"
    if event_name in DENIED_EVENTS:
        return "denied"
    return None


def handle_event(client, ev):
    name = ev.get("event", "")
    params = ev.get("params") or {}
    when = ev.get("utcTime") or ev.get("time") or ""

    # Input transitions reported by the device arrive here too, ahead of the
    # next slow poll.
    if name == "InputChanged":
        port = params.get("port")
        if port:
            publish_port(client, port, "ON" if params.get("state") else "OFF")
    elif name == "TamperSwitchActivated":
        log(f"[io] tamper switch activated at {when}")
        if PORT_TAMPER:
            client.publish(f"{TOPIC_IO}/tamper", "ON", qos=0, retain=True)
    elif name == "DoorStateChanged" and PORT_DOOR:
        # The field is not documented as a fixed type: accept the spellings a
        # device may use rather than silently reading every door as closed.
        raw_state = params.get("state")
        opened = raw_state in ("open", "opened", 1, True, "1", "true")
        client.publish(f"{TOPIC_IO}/door", "ON" if opened else "OFF", qos=0, retain=True)

    if not ENABLE_ACCESS_LOG:
        return
    result = classify(name, params)
    if result is None:
        return

    who = params.get("name") or params.get("uuid") or ""
    published_who = who if PUBLISH_USER_NAMES else (opaque(who) if who else "")
    client.publish(f"{TOPIC_ACCESS}/user", published_who, qos=0, retain=True)
    client.publish(f"{TOPIC_ACCESS}/time", when, qos=0, retain=True)
    client.publish(f"{TOPIC_ACCESS}/result", result, qos=0, retain=True)
    # Not retained on purpose: a retained event would be replayed as fresh on
    # every reconnect, turning an old rejected badge into a new alert.
    client.publish(f"{TOPIC_ACCESS}/event", json.dumps({
        "event": name,
        "time": when,
        "user": published_who,
        "result": result,
        "direction": params.get("direction", ""),
    }), qos=1, retain=False)
    log(f"[access] {name} {result} {published_who or '-'} {when}")


def log_loop(client, stop):
    """Own subscription, drained page by page, recreated if the device drops it."""
    if not ENABLE_ACCESS_LOG:
        return
    sid = None
    seen = 0
    blocking = LOG_LONG_POLL > 0
    backoff = LOG_POLL
    while not stop.is_set():
        try:
            if sid is None:
                sid = subscribe()
                # First drain is the backlog: publish nothing from it, just take
                # note of where the live stream starts.
                while True:
                    events = pull(sid, blocking=False)
                    if not events:
                        break
                    seen = max([seen] + [e.get("id", 0) for e in events])
                log(f"[log] backlog skipped, resuming after id={seen}")

            # A pull returns at most one page, not the whole arrears: keep asking
            # until the device answers with nothing.
            events = pull(sid, blocking=blocking)
            while events:
                for ev in events:
                    if ev.get("id", 0) <= seen:
                        continue
                    seen = ev.get("id", seen)
                    handle_event(client, ev)
                events = pull(sid, blocking=False)
            backoff = LOG_POLL
            if not blocking:
                stop.wait(LOG_POLL)
        except ApiError as e:
            if "subscription" in e.description.lower():
                log(f"[log] subscription lost, recreating: {e}")
                sid = None
                continue
            if e.permanent:
                log(f"[log] access log disabled after a permanent error: {e}")
                return
            log(f"[log] {e}")
            stop.wait(LOG_POLL)
        except AuthError as e:
            log(f"[log] authentication rejected: {e}; retrying in {backoff}s")
            sid = None
            stop.wait(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as e:
            # A transport failure says nothing about the subscription, which stays
            # valid on the device with our events queued behind it. Dropping it
            # here would orphan a queue and lose whatever it still held; if it
            # really is gone, the next pull says so and the branch above recreates
            # it. Back off meanwhile rather than retrying every few seconds.
            log(f"[log] {short(e)}; retrying in {backoff}s")
            stop.wait(backoff)
            backoff = min(backoff * 2, 60)
    unsubscribe(sid)


def unsubscribe(sid):
    """Release our own subscription, and only ours."""
    if not sid:
        return
    try:
        get("/log/unsubscribe", id=sid)
        log(f"[log] unsubscribed id={sid}")
    except Exception as e:
        log(f"[log] unsubscribe failed: {short(e)}")


# ---------------------------------------------------------------------------

def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=MQTT_CLIENT_ID, clean_session=True)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.will_set(AVAIL, "offline", qos=1, retain=True)
    client.on_connect = on_connect
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    log(f"[mqtt] {MQTT_HOST}:{MQTT_PORT} as {MQTT_USER or 'anonymous'}, base={BASE}")
    if _unsubscribed:
        log(f"[config] these access events are classified but not subscribed, so they "
            f"will never arrive; add them to LOG_FILTER: {sorted(_unsubscribed)}")
    log(f"[2n] {SCHEME}://{HOST} as {USER or 'anonymous'}, "
        f"io={'on' if ENABLE_IO else 'off'}, log={'on' if ENABLE_ACCESS_LOG else 'off'}")

    stop = threading.Event()
    state = {}

    # As PID 1 inside a container the process gets no default signal handling,
    # so without this SIGTERM is dropped, `docker stop` waits out its whole
    # timeout and the shutdown below never runs.
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    threads = [
        threading.Thread(target=io_loop, args=(client, state, stop), daemon=True),
        threading.Thread(target=log_loop, args=(client, stop), daemon=True),
    ]
    for t in threads:
        t.start()

    try:
        while not stop.is_set() and any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=LOG_LONG_POLL + 5)
        client.publish(AVAIL, "offline", qos=1, retain=True)
        client.loop_stop()
        client.disconnect()
        log("[mqtt] stopped")


if __name__ == "__main__":
    main()
