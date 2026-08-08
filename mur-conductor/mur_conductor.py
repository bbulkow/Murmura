#!/usr/bin/env python3
"""
Mur Conductor - ensemble playback coordinator for Murmura devices.

Makes several MURs play the same material together: starting together from a
cold boot, staying together for hours, and letting a solo-rebooted device
rejoin cleanly. Requires no firmware changes to the audio path.

Topology (Murmura-only - there is no Haven Trigger Server in this deployment;
the conductor takes over that role):

    [mur-conductor]  :5002 registration + ingest, :4002 status/admin
          | (TCP to gateway upstream port, newline-delimited JSON events)
    [mur-gateway | mur-abs-gateway]  :4000 devices, :4001 status
          | (devices connect outbound, announce + subscribe)
    [MUR devices]

How the sync works (see SYNC_DESIGN.md for the full rationale):
  - The conductor is a metronome. Every playlist entry boundary ("downbeat") it
    sends one trigger event. The gateway stamps it with an absolute WiFi-TSF
    deadline (target_tsf_us) and fans the *same* deadline out to every
    subscribed device, which defer execution to that deadline. Devices thus
    restart in lockstep to within the on-device scheduler's precision
    (~5-10 ms), without the conductor needing an accurate clock of its own.
  - The ensemble track is in TRIGGER mode, so it boots SILENT and plays exactly
    once per downbeat. That gives "silent until the next downbeat" for free: a
    device that reboots alone makes no sound until it rejoins the ensemble.
  - Because every beat is deferred by the same gateway-side fanout delay, that
    delay cancels out of the spacing between beats. Conductor->gateway network
    jitter shifts the whole grid, never device-vs-device alignment.

Two invariants this service must respect (both documented in SYNC_DESIGN.md):
  - Never fire "SceneChange" - that name is reserved for the scene service and
    is special-cased in the gateway. Enforced in config validation.
  - Never add an inbound injection endpoint to the gateway. The conductor
    reaches devices by *being* the upstream trigger source, not by poking the
    gateway.

All configuration lives in config.json alongside this file. SIGHUP reloads it.
"""

import asyncio
import json
import logging
import os
import re
import signal
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web

logger = logging.getLogger("mur-conductor")

VERSION = "1.0"

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

# Reserved for the scene service; the gateway special-cases it. See SYNC_DESIGN.md.
SCENE_TRIGGER_NAME = "SceneChange"

CONFIG_DEFAULTS = {
    "listen_port": 5002,          # gateway registers here; also manual ingest
    "status_port": 4002,          # status + admin API
    "gateway_status_url": "http://127.0.0.1:4001/status",
    "health_poll_interval_s": 10,
    "device_http_timeout_s": 3.0,
    "groups": [],
}

GROUP_DEFAULTS = {
    "enabled": True,
    "trigger_name": "",
    "scene_name": "",
    "track": 0,
    "expected_device_ids": [],
    "readiness_timeout_s": 120,
    "prep_lead_ms": 8000,
    "loop_playlist": True,
    "playlist": [],
}

# A downbeat later than this is treated as unreachable and skipped rather than
# fired late - firing a burst of backlogged beats would be worse than silence.
BEAT_LATE_TOLERANCE_S = 1.0

# Health poll backoff ceiling when the gateway status endpoint is unreachable.
HEALTH_BACKOFF_MAX_S = 60.0

_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def iso_now() -> str:
    return datetime.now().isoformat()


# ---------------------------------------------------------------------------
#  Configuration
# ---------------------------------------------------------------------------

@dataclass
class PlaylistEntry:
    file: str
    duration_ms: int
    gap_ms: int = 0

    @property
    def span_s(self) -> float:
        """Seconds from this entry's downbeat to the next one."""
        return (self.duration_ms + self.gap_ms) / 1000.0

    def to_dict(self) -> dict:
        return {"file": self.file, "duration_ms": self.duration_ms, "gap_ms": self.gap_ms}


@dataclass
class GroupConfig:
    name: str
    trigger_name: str
    scene_name: str
    track: int
    expected_device_ids: list
    readiness_timeout_s: float
    prep_lead_ms: int
    loop_playlist: bool
    playlist: list  # list[PlaylistEntry]
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "trigger_name": self.trigger_name,
            "scene_name": self.scene_name,
            "track": self.track,
            "expected_device_ids": list(self.expected_device_ids),
            "readiness_timeout_s": self.readiness_timeout_s,
            "prep_lead_ms": self.prep_lead_ms,
            "loop_playlist": self.loop_playlist,
            "playlist": [e.to_dict() for e in self.playlist],
        }


class ConfigError(Exception):
    """Fatal configuration problem - the service refuses to start."""


def _parse_playlist(raw, where: str) -> list:
    entries = []
    if not isinstance(raw, list):
        raise ConfigError(f"{where}: playlist must be a list")
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: playlist[{i}] must be an object")
        file_path = item.get("file") or item.get("file_path")
        if not isinstance(file_path, str) or not file_path.strip():
            raise ConfigError(f"{where}: playlist[{i}].file must be a non-empty string")
        try:
            duration_ms = int(item["duration_ms"])
        except (KeyError, TypeError, ValueError):
            raise ConfigError(f"{where}: playlist[{i}].duration_ms must be an integer (ms)")
        if duration_ms < 2000:
            raise ConfigError(
                f"{where}: playlist[{i}].duration_ms={duration_ms} is below the 2000 ms floor"
            )
        gap_ms = item.get("gap_ms", 0)
        try:
            gap_ms = int(gap_ms)
        except (TypeError, ValueError):
            raise ConfigError(f"{where}: playlist[{i}].gap_ms must be an integer (ms)")
        if gap_ms < 0:
            raise ConfigError(f"{where}: playlist[{i}].gap_ms must not be negative")
        entries.append(PlaylistEntry(file=file_path.strip(),
                                     duration_ms=duration_ms, gap_ms=gap_ms))
    return entries


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Load and validate config.json. Raises ConfigError on anything fatal."""
    cfg = dict(CONFIG_DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"No {path} found - the group list is the whole point of this service")
    except json.JSONDecodeError as e:
        raise ConfigError(f"Bad JSON in {path}: {e}")

    if not isinstance(user_cfg, dict):
        raise ConfigError(f"{path} must be a JSON object")

    for key in ("listen_port", "status_port", "health_poll_interval_s"):
        if key in user_cfg:
            try:
                cfg[key] = int(user_cfg[key])
            except (TypeError, ValueError):
                raise ConfigError(f"{key} must be an integer")
    if "device_http_timeout_s" in user_cfg:
        try:
            cfg["device_http_timeout_s"] = float(user_cfg["device_http_timeout_s"])
        except (TypeError, ValueError):
            raise ConfigError("device_http_timeout_s must be a number")
    if "gateway_status_url" in user_cfg:
        cfg["gateway_status_url"] = str(user_cfg["gateway_status_url"]).rstrip("/")

    raw_groups = user_cfg.get("groups", [])
    if not isinstance(raw_groups, list):
        raise ConfigError("groups must be a list")

    groups = []
    seen_names = set()
    seen_triggers = set()
    for i, raw in enumerate(raw_groups):
        if not isinstance(raw, dict):
            raise ConfigError(f"groups[{i}] must be an object")
        merged = dict(GROUP_DEFAULTS)
        merged.update(raw)
        name = str(merged.get("name") or f"group{i}")
        where = f"group '{name}'"

        trigger_name = str(merged["trigger_name"] or "").strip()
        if not trigger_name:
            raise ConfigError(f"{where}: trigger_name is required")
        if trigger_name == SCENE_TRIGGER_NAME:
            raise ConfigError(
                f"{where}: trigger_name must not be '{SCENE_TRIGGER_NAME}' - that name is "
                "reserved for the scene service and is special-cased by the gateway "
                "(see SYNC_DESIGN.md)"
            )
        if trigger_name in seen_triggers:
            raise ConfigError(f"{where}: trigger_name '{trigger_name}' is already used by another group")
        if name in seen_names:
            raise ConfigError(f"duplicate group name '{name}'")
        seen_names.add(name)
        seen_triggers.add(trigger_name)

        scene_name = str(merged["scene_name"] or "").strip()
        if not scene_name:
            raise ConfigError(f"{where}: scene_name is required (the ensemble scene on each device)")

        try:
            track = int(merged["track"])
        except (TypeError, ValueError):
            raise ConfigError(f"{where}: track must be an integer")
        if not 0 <= track <= 2:
            raise ConfigError(f"{where}: track must be 0..2 (MAX_TRACKS is 3)")

        device_ids = merged["expected_device_ids"]
        if not isinstance(device_ids, list) or not all(isinstance(d, str) for d in device_ids):
            raise ConfigError(f"{where}: expected_device_ids must be a list of strings")

        playlist = _parse_playlist(merged["playlist"], where)

        try:
            prep_lead_ms = int(merged["prep_lead_ms"])
        except (TypeError, ValueError):
            raise ConfigError(f"{where}: prep_lead_ms must be an integer")
        if prep_lead_ms < 0:
            raise ConfigError(f"{where}: prep_lead_ms must not be negative")

        try:
            readiness_timeout_s = float(merged["readiness_timeout_s"])
        except (TypeError, ValueError):
            raise ConfigError(f"{where}: readiness_timeout_s must be a number")

        group = GroupConfig(
            name=name,
            trigger_name=trigger_name,
            scene_name=scene_name,
            track=track,
            expected_device_ids=list(device_ids),
            readiness_timeout_s=readiness_timeout_s,
            prep_lead_ms=prep_lead_ms,
            loop_playlist=bool(merged["loop_playlist"]),
            playlist=playlist,
            enabled=bool(merged["enabled"]),
        )

        # Non-fatal sanity checks - the service still runs, the operator gets told.
        for j, entry in enumerate(playlist):
            if prep_lead_ms >= entry.duration_ms + entry.gap_ms:
                logger.warning(
                    "%s: prep_lead_ms=%d is >= entry %d's span (%d ms) - the file swap for the "
                    "following entry will happen immediately after its downbeat",
                    where, prep_lead_ms, j, entry.duration_ms + entry.gap_ms)
        if not playlist:
            logger.warning("%s: playlist is empty - the group will idle until one is set", where)
        if not device_ids:
            logger.warning("%s: expected_device_ids is empty - readiness cannot be checked", where)

        groups.append(group)

    cfg["groups"] = groups
    logger.info("Loaded %d group(s) from %s", len(groups), path)
    return cfg


def save_config(cfg: dict, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Persist config back to disk (used by the admin API).

    Written via a temp file + replace so a crash mid-write cannot leave the
    service with a truncated config it would refuse to start from.
    """
    body = {
        "listen_port": cfg["listen_port"],
        "status_port": cfg["status_port"],
        "gateway_status_url": cfg["gateway_status_url"],
        "health_poll_interval_s": cfg["health_poll_interval_s"],
        "device_http_timeout_s": cfg["device_http_timeout_s"],
        "groups": [g.to_dict() for g in cfg["groups"]],
    }
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    logger.info("Wrote config to %s", path)


# ---------------------------------------------------------------------------
#  Upstream role: the gateway registers with us, we send it trigger events
# ---------------------------------------------------------------------------

@dataclass
class RegisteredService:
    name: str
    host: str
    port: int
    protocol: str
    writer: Optional[asyncio.StreamWriter] = None
    registered_at: str = field(default_factory=iso_now)

    @property
    def connected(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()


class GatewayLink:
    """Implements the Trigger-Server side of the gateway's upstream protocol.

    The gateway POSTs /api/register telling us its host and upstream port; we
    then open a TCP connection *to* it and push newline-delimited JSON trigger
    events. This is the same handshake mock-trigger-server implements, and the
    gateway end of it lives in mur_gateway.py (_register_with_trigger_server /
    _handle_upstream_connection).

    Note on recovery timing: the gateway only re-registers when it notices its
    upstream connection is down, on a 30 s loop. So after a conductor restart
    there can be up to ~30 s before events can flow again. The readiness gate
    covers this - it waits for a live link before the first downbeat.
    """

    def __init__(self):
        self.services: dict[str, RegisteredService] = {}
        self._event_id = 0

    @property
    def connected(self) -> bool:
        return any(s.connected for s in self.services.values())

    def _next_event_id(self) -> int:
        self._event_id += 1
        return self._event_id

    async def register(self, name: str, host: str, port: int, protocol: str) -> None:
        """Connect back to a service that just registered. Raises on failure."""
        if protocol != "TCP_SOCKET":
            raise ValueError(f"unsupported protocol: {protocol}")

        old = self.services.get(name)
        if old and old.writer is not None and not old.writer.is_closing():
            old.writer.close()

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=10)
        self.services[name] = RegisteredService(
            name=name, host=host, port=port, protocol=protocol, writer=writer)
        logger.info("Connected to %s at %s:%d", name, host, port)

        # Drain whatever the peer sends (the gateway sends nothing upstream, but
        # reading is how we notice the connection dying).
        asyncio.create_task(self._watch(name, reader))

    async def _watch(self, name: str, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                logger.debug("Unexpected upstream data from %s: %.100s", name, data)
        except (ConnectionError, OSError) as e:
            logger.info("Connection to %s errored: %s", name, e)
        finally:
            svc = self.services.get(name)
            if svc and svc.writer is not None:
                try:
                    svc.writer.close()
                except Exception:
                    pass
                svc.writer = None
            logger.info("Connection to %s closed - awaiting re-registration", name)

    async def send_event(self, name: str, value="") -> tuple[int, int]:
        """Send a trigger event to every connected service.

        Returns (services_sent_to, event_id). The wire shape matches what the
        gateway expects from a Trigger Server: name/value/id/timestamp, one
        JSON object per line. We deliberately send no time fields - the
        gateway supplies the shared deadline itself (fanout_delay_ms) once
        more than one device is subscribed, which is exactly the behavior we
        want and the only path verified to work end to end.
        """
        eid = self._next_event_id()
        event = {"name": name, "value": value, "id": eid, "timestamp": iso_now()}
        line = (json.dumps(event) + "\n").encode("utf-8")

        sent = 0
        for svc in list(self.services.values()):
            if not svc.connected:
                continue
            try:
                svc.writer.write(line)
                await svc.writer.drain()
                sent += 1
            except (ConnectionError, OSError) as e:
                logger.warning("Send to %s failed: %s", svc.name, e)
                try:
                    svc.writer.close()
                except Exception:
                    pass
                svc.writer = None
        return sent, eid

    def close(self) -> None:
        for svc in self.services.values():
            if svc.writer is not None:
                try:
                    svc.writer.close()
                except Exception:
                    pass
                svc.writer = None


# ---------------------------------------------------------------------------
#  Health: what the gateway can tell us about the fleet
# ---------------------------------------------------------------------------

def _ip_from_status_device(dev: dict) -> str:
    """Best-effort device IP from a gateway status entry.

    Prefers the explicit peer_ip field; falls back to digging an IPv4 address
    out of the stringified peer tuple so the conductor also works against a
    gateway that predates that field.
    """
    ip = dev.get("peer_ip")
    if isinstance(ip, str) and ip:
        return ip
    m = _IPV4_RE.search(str(dev.get("peer", "")))
    return m.group(1) if m else ""


class HealthMonitor:
    """Polls the gateway's read-only status endpoint.

    Deliberately the *only* thing that watches the fleet: it never opens HTTP
    connections to the devices themselves. A second concurrent TCP connection
    to an ESP32 causes drops, which is why mur-config-server serializes per
    device - so liveness here is derived from the gateway's own view of its
    device connections instead.
    """

    def __init__(self, status_url: str, interval_s: float, session_factory):
        self.status_url = status_url
        self.interval_s = interval_s
        self._session_factory = session_factory
        self.snapshot: Optional[dict] = None
        self.snapshot_at: float = 0.0
        self.last_error: Optional[str] = None
        self.reachable = False
        # device_id -> connected_at, for reboot detection
        self._connected_at: dict[str, float] = {}
        self._running = True

    def device(self, device_id: str) -> Optional[dict]:
        if not self.snapshot:
            return None
        for dev in self.snapshot.get("devices", []):
            if dev.get("id") == device_id:
                return dev
        return None

    def is_subscribed(self, device_id: str, trigger_name: str) -> bool:
        dev = self.device(device_id)
        return bool(dev) and trigger_name in (dev.get("triggers") or [])

    @property
    def fanout_delay_ms(self) -> Optional[int]:
        if not self.snapshot:
            return None
        return (self.snapshot.get("sync") or {}).get("fanout_delay_ms")

    @property
    def age_s(self) -> Optional[float]:
        if not self.snapshot_at:
            return None
        return round(time.monotonic() - self.snapshot_at, 1)

    async def run(self) -> None:
        backoff = self.interval_s
        while self._running:
            try:
                session = self._session_factory()
                async with session.get(
                        self.status_url,
                        timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        self._absorb(await resp.json())
                        self.reachable = True
                        self.last_error = None
                        backoff = self.interval_s
                    else:
                        self.reachable = False
                        self.last_error = f"HTTP {resp.status}"
                        logger.warning("Gateway status returned HTTP %d", resp.status)
                        backoff = min(backoff * 2, HEALTH_BACKOFF_MAX_S)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self.reachable or self.last_error is None:
                    logger.warning("Cannot reach gateway status at %s: %s", self.status_url, e)
                self.reachable = False
                self.last_error = str(e)
                backoff = min(backoff * 2, HEALTH_BACKOFF_MAX_S)
            await asyncio.sleep(backoff)

    def _absorb(self, body: dict) -> None:
        self.snapshot = body
        self.snapshot_at = time.monotonic()

        # Reboot detection: a device whose connected_at moved forward has
        # dropped and re-established its gateway connection. That is the
        # signal mur-config-server cannot give us (it discards uptime).
        seen = set()
        for dev in body.get("devices", []):
            dev_id = dev.get("id")
            if not isinstance(dev_id, str) or dev_id == "(unannounced)":
                continue
            seen.add(dev_id)
            connected_at = dev.get("connected_at")
            if not isinstance(connected_at, (int, float)):
                continue
            prev = self._connected_at.get(dev_id)
            if prev is None:
                logger.info("Device %s present at gateway", dev_id)
            elif connected_at > prev + 1.0:
                logger.warning(
                    "Device %s reconnected (probable reboot) - it is silent and will "
                    "rejoin at the next downbeat", dev_id)
            self._connected_at[dev_id] = connected_at

        for gone in set(self._connected_at) - seen:
            logger.warning("Device %s is no longer connected to the gateway", gone)
            del self._connected_at[gone]

        if not body.get("upstream_connected"):
            logger.warning("Gateway reports upstream_connected=false - "
                           "downbeats will not reach devices")
        tsf = (body.get("sync") or {}).get("tsf_map") or {}
        if not tsf.get("have_canonical"):
            logger.warning("Gateway has no TSF canonical sample - downbeats will pass "
                           "through unscheduled (devices will not be aligned)")
        else:
            age = tsf.get("canonical_age_seconds")
            max_age = tsf.get("max_age_seconds")
            if isinstance(age, (int, float)) and isinstance(max_age, (int, float)) and age > max_age:
                logger.warning("Gateway TSF map is stale (%.0fs > %.0fs)", age, max_age)

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
#  The metronome
# ---------------------------------------------------------------------------

class GroupRunner:
    """Drives one ensemble group: readiness gate, then the downbeat sequence."""

    def __init__(self, group: GroupConfig, conductor: "Conductor"):
        self.group = group
        self.c = conductor

        self.state = "starting"
        self.ready = False
        self.index = 0
        self.beat_count = 0
        self.last_beat_iso: Optional[str] = None
        self.last_beat_sent_to: Optional[int] = None
        self.next_deadline: Optional[float] = None   # monotonic
        self.last_prep: dict[str, str] = {}          # device_id -> "ok" | error
        self._pending_playlist: Optional[list] = None
        self._pending_loop: Optional[bool] = None
        self._running = True

    # -- public control ---------------------------------------------------

    def set_playlist(self, playlist: list, loop_playlist: Optional[bool] = None) -> None:
        """Queue a new playlist. Applied at the next downbeat boundary so we
        never cut a playing entry off mid-file."""
        self._pending_playlist = playlist
        if loop_playlist is not None:
            self._pending_loop = loop_playlist
        logger.info("Group '%s': %d-entry playlist queued for the next boundary",
                    self.group.name, len(playlist))

    def stop(self) -> None:
        self._running = False

    # -- status -----------------------------------------------------------

    def status(self) -> dict:
        g = self.group
        members = []
        for dev_id in g.expected_device_ids:
            dev = self.c.health.device(dev_id)
            members.append({
                "id": dev_id,
                "present": dev is not None,
                "subscribed": self.c.health.is_subscribed(dev_id, g.trigger_name),
                "ip": _ip_from_status_device(dev) if dev else "",
                "gateway_uptime_seconds": dev.get("uptime_seconds") if dev else None,
                "last_prep": self.last_prep.get(dev_id),
            })
        entry = self._entry(self.index)
        next_in = None
        if self.next_deadline is not None:
            next_in = round(self.next_deadline - time.monotonic(), 1)
        return {
            "name": g.name,
            "enabled": g.enabled,
            "state": self.state,
            "ready": self.ready,
            "trigger_name": g.trigger_name,
            "scene_name": g.scene_name,
            "track": g.track,
            "loop_playlist": g.loop_playlist,
            "playlist": [e.to_dict() for e in g.playlist],
            "playlist_length": len(g.playlist),
            "current_index": self.index if entry else None,
            "current_file": entry.file if entry else None,
            "beat_count": self.beat_count,
            "last_beat_at": self.last_beat_iso,
            "last_beat_sent_to_services": self.last_beat_sent_to,
            "next_downbeat_in_s": next_in,
            "members": members,
            "missing_device_ids": [m["id"] for m in members if not m["present"]],
            "unsubscribed_device_ids": [
                m["id"] for m in members if m["present"] and not m["subscribed"]
            ],
        }

    def _entry(self, idx: int) -> Optional[PlaylistEntry]:
        if 0 <= idx < len(self.group.playlist):
            return self.group.playlist[idx]
        return None

    # -- main loop --------------------------------------------------------

    async def run(self) -> None:
        g = self.group
        if not g.enabled:
            self.state = "disabled"
            logger.info("Group '%s' is disabled - not starting", g.name)
            return
        try:
            await self._await_readiness()
            await self._sequence()
        except asyncio.CancelledError:
            self.state = "stopped"
            raise
        except Exception:
            self.state = "error"
            logger.exception("Group '%s' runner failed", g.name)

    async def _await_readiness(self) -> None:
        """Block until the fleet can actually start together.

        This is what makes a cold boot work. When everything powers up at once
        the MURs typically reach the gateway before the Pi has finished
        starting its services; they sit there silent and armed. Rather than
        firing into a half-assembled fleet (or making everyone wait out a whole
        entry), we wait for the members to show up and then start immediately.
        """
        g = self.group
        self.state = "waiting_readiness"
        started = time.monotonic()
        logged_wait = False

        while self._running:
            missing = []
            unsubscribed = []
            for dev_id in g.expected_device_ids:
                if self.c.health.device(dev_id) is None:
                    missing.append(dev_id)
                elif not self.c.health.is_subscribed(dev_id, g.trigger_name):
                    unsubscribed.append(dev_id)

            link_ok = self.c.link.connected
            fleet_ok = not missing and not unsubscribed and bool(g.expected_device_ids)

            if link_ok and fleet_ok:
                self.ready = True
                logger.info("Group '%s' ready: %d device(s) subscribed to '%s' after %.1fs",
                            g.name, len(g.expected_device_ids), g.trigger_name,
                            time.monotonic() - started)
                return

            waited = time.monotonic() - started
            if waited >= g.readiness_timeout_s:
                logger.warning(
                    "Group '%s': readiness timeout after %.0fs - starting anyway "
                    "(gateway_link=%s missing=%s unsubscribed=%s)",
                    g.name, waited, "up" if link_ok else "down", missing or "none",
                    unsubscribed or "none")
                return

            if not logged_wait:
                logger.info(
                    "Group '%s' waiting for readiness (gateway_link=%s missing=%s "
                    "unsubscribed=%s, timeout %.0fs)",
                    g.name, "up" if link_ok else "down", missing or "none",
                    unsubscribed or "none", g.readiness_timeout_s)
                logged_wait = True

            await asyncio.sleep(1.0)

    async def _sequence(self) -> None:
        """Fire downbeats on an absolute timeline.

        Deadlines are computed as t0 + sum(spans) on a monotonic clock rather
        than by sleeping for each span, so scheduling jitter and slow file
        POSTs cannot accumulate into the musical timeline.
        """
        g = self.group
        self.state = "running"

        while self._running:
            self._apply_pending()
            if not g.playlist:
                self.state = "idle_no_playlist"
                self.next_deadline = None
                await asyncio.sleep(1.0)
                self.state = "running"
                continue

            self.index = min(self.index, len(g.playlist) - 1)

            # The first entry's file must be on the devices before its downbeat,
            # so prep comes first and the downbeat follows immediately.
            await self._prep(self.index)
            deadline = time.monotonic()
            await self._fire(self.index)

            while self._running:
                entry = self._entry(self.index)
                if entry is None:
                    break

                nxt = self.index + 1
                if nxt >= len(g.playlist):
                    if not g.loop_playlist and self._pending_playlist is None:
                        logger.info("Group '%s': playlist finished (loop_playlist=false)", g.name)
                        self.state = "finished"
                        self.next_deadline = None
                        return
                    nxt = 0

                deadline = deadline + entry.span_s
                self.next_deadline = deadline

                # Skip beats we can no longer hit rather than firing a backlog.
                now = time.monotonic()
                while deadline < now - BEAT_LATE_TOLERANCE_S:
                    logger.warning(
                        "Group '%s': downbeat for entry %d was %.1fs late - skipping it "
                        "to stay on the original timeline", g.name, nxt,
                        now - deadline)
                    skipped = self._entry(nxt)
                    if skipped is None:
                        break
                    deadline += skipped.span_s
                    nxt = (nxt + 1) % len(g.playlist)
                    self.next_deadline = deadline

                await self._sleep_until(deadline - g.prep_lead_ms / 1000.0)
                if not self._running:
                    return
                await self._prep(nxt)

                await self._sleep_until(deadline)
                if not self._running:
                    return

                self.index = nxt
                await self._fire(self.index)

                if self._pending_playlist is not None:
                    self._apply_pending()
                    self.index = 0
                    break  # re-enter the outer loop to re-prime from entry 0

    def _apply_pending(self) -> None:
        if self._pending_playlist is not None:
            self.group.playlist = self._pending_playlist
            self._pending_playlist = None
            logger.info("Group '%s': new playlist active (%d entries)",
                        self.group.name, len(self.group.playlist))
        if self._pending_loop is not None:
            self.group.loop_playlist = self._pending_loop
            self._pending_loop = None

    async def _sleep_until(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        while self._running and remaining > 0:
            await asyncio.sleep(min(remaining, 1.0))
            remaining = deadline - time.monotonic()

    async def _fire(self, idx: int) -> None:
        """Send the downbeat for entry idx."""
        g = self.group
        entry = self._entry(idx)
        # value="On" is stripped by the gateway's On/Off->OneShot conversion, so
        # devices receive a canonical valueless OneShot event.
        sent, eid = await self.c.link.send_event(g.trigger_name, "On")
        self.beat_count += 1
        self.last_beat_iso = iso_now()
        self.last_beat_sent_to = sent
        if sent == 0:
            logger.warning(
                "Group '%s': downbeat %d (entry %d, %s) reached NO gateway - "
                "the fleet will not advance", g.name, self.beat_count, idx,
                entry.file if entry else "?")
        else:
            logger.info("Group '%s': downbeat %d -> entry %d (%s), event_id=%d, %d gateway(s)",
                        g.name, self.beat_count, idx,
                        entry.file if entry else "?", eid, sent)

    async def _prep(self, idx: int) -> None:
        """Push entry idx's file into every member's ensemble scene track.

        Done to all members every entry, not just changed ones, so a device
        that rebooted since the last entry is brought back in line without
        any special case.
        """
        entry = self._entry(idx)
        if entry is None:
            return
        g = self.group
        targets = []
        for dev_id in g.expected_device_ids:
            dev = self.c.health.device(dev_id)
            ip = _ip_from_status_device(dev) if dev else ""
            if not ip:
                self.last_prep[dev_id] = "no ip (device not at gateway)"
                continue
            targets.append((dev_id, ip))

        if not targets:
            logger.warning("Group '%s': no reachable members to prep entry %d (%s)",
                           g.name, idx, entry.file)
            return

        results = await asyncio.gather(*(
            self.c.post_file_path(dev_id, ip, g.scene_name, g.track,
                                  entry.file, g.trigger_name)
            for dev_id, ip in targets
        ))
        ok = 0
        for (dev_id, _ip), err in zip(targets, results):
            if err is None:
                self.last_prep[dev_id] = "ok"
                ok += 1
            else:
                self.last_prep[dev_id] = err
        logger.info("Group '%s': prepped entry %d (%s) on %d/%d member(s)",
                    g.name, idx, entry.file, ok, len(targets))


# ---------------------------------------------------------------------------
#  Conductor
# ---------------------------------------------------------------------------

class Conductor:
    def __init__(self, cfg: dict, config_path: Path):
        self.cfg = cfg
        self.config_path = config_path
        self.link = GatewayLink()
        self._session: Optional[aiohttp.ClientSession] = None
        self.health = HealthMonitor(
            cfg["gateway_status_url"], float(cfg["health_poll_interval_s"]),
            self._get_session)
        self.runners: dict[str, GroupRunner] = {}
        self._tasks: list[asyncio.Task] = []
        self._config_lock = asyncio.Lock()
        self.started_at = time.time()
        self._running = True
        self._reload_requested = asyncio.Event()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # -- device I/O -------------------------------------------------------

    async def post_file_path(self, device_id: str, ip: str, scene_name: str,
                             track: int, file_path: str,
                             trigger_name: str) -> Optional[str]:
        """Patch one device's ensemble track to play file_path next.

        Returns None on success, or a short error string.

        The `"active": true` and `"mode": "trigger"` fields are NOT cosmetic and
        must not be dropped. In scene_apply_patch (main/scene_manager.c) a
        file_path change on the active scene restarts the track immediately if
        it is playing - which would cut to the next entry early and out of
        sync, once per device, defeating the whole design. Supplying `active`
        routes the patch into the trigger-mode branch, which only re-enables
        the track (an inert flag set, see AUDIO_ACTION_ENABLE_TRACK in
        main/murmura.c) and never reaches that restart. The new file_path is
        still stored for the next trigger to pick up.
        """
        url = f"http://{ip}/api/scenes"
        body = {
            scene_name: {
                "tracks": [{
                    "track": track,
                    "mode": "trigger",
                    "trigger_type": "OneShot",
                    "trigger_name": trigger_name,
                    "active": True,
                    "file_path": file_path,
                }]
            }
        }
        timeout = aiohttp.ClientTimeout(total=float(self.cfg["device_http_timeout_s"]))
        last_err = "unknown"
        for attempt in (1, 2):
            try:
                session = self._get_session()
                async with session.post(url, json=body, timeout=timeout) as resp:
                    if 200 <= resp.status < 300:
                        return None
                    text = (await resp.text())[:200]
                    last_err = f"HTTP {resp.status}: {text}"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Include the type: aiohttp connection errors often stringify
                # to nothing at all, which makes for a useless log line.
                last_err = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            if attempt == 1:
                await asyncio.sleep(0.5)
        logger.warning("Prep POST to %s (%s) failed: %s", device_id, ip, last_err)
        return last_err

    # -- HTTP: upstream role (gateway registers here) ---------------------

    async def _h_register(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)
        name = data.get("name", "")
        host = data.get("host", "localhost")
        port = data.get("port")
        protocol = data.get("protocol", "TCP_SOCKET")
        if not name or not port:
            return web.json_response({"error": "name and port required"}, status=400)

        logger.info("Registration from %s at %s:%s (%s)", name, host, port, protocol)
        try:
            await self.link.register(name, host, int(port), protocol)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except Exception as e:
            logger.warning("Failed to connect back to %s at %s:%s: %s", name, host, port, e)
            return web.json_response({"error": f"Failed to connect: {e}"}, status=500)

        return web.json_response({
            "message": "Service registered successfully",
            "registration": {"name": name, "host": host, "port": int(port),
                             "protocol": protocol, "socket_status": "connected"},
        })

    async def _h_registrations(self, request: web.Request) -> web.Response:
        return web.json_response({"services": [
            {"name": s.name, "host": s.host, "port": s.port, "protocol": s.protocol,
             "socket_connected": s.connected, "registered_at": s.registered_at}
            for s in self.link.services.values()
        ]})

    async def _h_triggers(self, request: web.Request) -> web.Response:
        """GET /api/triggers - the trigger names we conduct.

        Keeps the existing dropdown chain working: mur-config-server asks the
        gateway, which proxies this.
        """
        triggers = [{"name": g.trigger_name, "type": "OneShot"}
                    for g in self.cfg["groups"]]
        triggers.sort(key=lambda t: t["name"])
        return web.json_response({"triggers": triggers, "last_modified": iso_now()})

    async def _h_trigger_event(self, request: web.Request) -> web.Response:
        """POST /api/trigger-event - manual injection, for testing.

        Same shape as mock-trigger-server so existing tooling works. This does
        not disturb any group's timeline; it just puts one event on the wire.
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            return web.json_response({"error": "name must be a non-empty string"}, status=400)
        sent, eid = await self.link.send_event(name.strip(), data.get("value", ""))
        logger.info("Manual trigger '%s' -> %d gateway(s) (event_id=%d)", name, sent, eid)
        return web.json_response({"event_id": eid, "sent_to_services": sent})

    # -- HTTP: status + admin --------------------------------------------

    async def _h_status(self, request: web.Request) -> web.Response:
        tsf = (self.health.snapshot or {}).get("sync", {}).get("tsf_map", {})
        return web.json_response({
            "service": "mur-conductor",
            "version": VERSION,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "listen_port": self.cfg["listen_port"],
            "status_port": self.cfg["status_port"],
            "gateway": {
                "link_connected": self.link.connected,
                "registered_services": [
                    {"name": s.name, "host": s.host, "port": s.port,
                     "connected": s.connected}
                    for s in self.link.services.values()
                ],
                "status_url": self.cfg["gateway_status_url"],
                "status_reachable": self.health.reachable,
                "status_age_seconds": self.health.age_s,
                "status_error": self.health.last_error,
                "upstream_connected": (self.health.snapshot or {}).get("upstream_connected"),
                "device_count": (self.health.snapshot or {}).get("device_count"),
                "fanout_delay_ms": self.health.fanout_delay_ms,
                "tsf_map": tsf,
            },
            "groups": [r.status() for r in self.runners.values()],
        })

    def _find_group(self, name: str) -> Optional[GroupConfig]:
        for g in self.cfg["groups"]:
            if g.name == name:
                return g
        return None

    async def _h_set_playlist(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        group = self._find_group(name)
        runner = self.runners.get(name)
        if group is None:
            return web.json_response({"error": f"no such group: {name}"}, status=404)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)
        try:
            playlist = _parse_playlist(data.get("playlist", []), f"group '{name}'")
        except ConfigError as e:
            return web.json_response({"error": str(e)}, status=400)

        loop_playlist = data.get("loop_playlist")
        if loop_playlist is not None:
            loop_playlist = bool(loop_playlist)

        async with self._config_lock:
            group.playlist = playlist
            if loop_playlist is not None:
                group.loop_playlist = loop_playlist
            try:
                save_config(self.cfg, self.config_path)
            except OSError as e:
                logger.warning("Could not persist config: %s", e)

        if runner:
            runner.set_playlist(playlist, loop_playlist)
        return web.json_response({
            "group": name, "playlist_length": len(playlist),
            "applies": "at the next downbeat boundary",
        })

    async def _h_set_group(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        group = self._find_group(name)
        if group is None:
            return web.json_response({"error": f"no such group: {name}"}, status=404)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)

        changed = []
        async with self._config_lock:
            if "enabled" in data:
                group.enabled = bool(data["enabled"])
                changed.append("enabled")
            if "expected_device_ids" in data:
                ids = data["expected_device_ids"]
                if not isinstance(ids, list) or not all(isinstance(d, str) for d in ids):
                    return web.json_response(
                        {"error": "expected_device_ids must be a list of strings"}, status=400)
                group.expected_device_ids = list(ids)
                changed.append("expected_device_ids")
            if "loop_playlist" in data:
                group.loop_playlist = bool(data["loop_playlist"])
                changed.append("loop_playlist")
            if changed:
                try:
                    save_config(self.cfg, self.config_path)
                except OSError as e:
                    logger.warning("Could not persist config: %s", e)

        needs_restart = "enabled" in changed
        if needs_restart:
            await self._restart_group(name)
        return web.json_response({
            "group": name, "changed": changed,
            "note": "group runner restarted" if needs_restart
                    else "membership/loop changes apply immediately",
        })

    # -- lifecycle --------------------------------------------------------

    async def _restart_group(self, name: str) -> None:
        group = self._find_group(name)
        old = self.runners.pop(name, None)
        if old:
            old.stop()
        if group is None:
            return
        runner = GroupRunner(group, self)
        self.runners[name] = runner
        self._tasks.append(asyncio.create_task(runner.run()))
        logger.info("Group '%s' runner (re)started", name)

    def request_reload(self) -> None:
        self._reload_requested.set()

    async def _reload_loop(self) -> None:
        while self._running:
            await self._reload_requested.wait()
            self._reload_requested.clear()
            logger.info("SIGHUP - reloading %s", self.config_path)
            try:
                new_cfg = load_config(self.config_path)
            except ConfigError as e:
                logger.error("Reload rejected, keeping running config: %s", e)
                continue
            async with self._config_lock:
                self.cfg["groups"] = new_cfg["groups"]
                self.cfg["health_poll_interval_s"] = new_cfg["health_poll_interval_s"]
                self.cfg["device_http_timeout_s"] = new_cfg["device_http_timeout_s"]
                self.health.interval_s = float(new_cfg["health_poll_interval_s"])
            # Ports and the gateway status URL are bound at startup; changing
            # them needs a real restart.
            if new_cfg["gateway_status_url"] != self.cfg["gateway_status_url"] or \
                    new_cfg["listen_port"] != self.cfg["listen_port"] or \
                    new_cfg["status_port"] != self.cfg["status_port"]:
                logger.warning("Ports and gateway_status_url changes need a service restart")
            for name in list(self.runners):
                await self._restart_group(name)
            for g in self.cfg["groups"]:
                if g.name not in self.runners:
                    await self._restart_group(g.name)

    async def run(self) -> None:
        # Upstream/ingest HTTP (the gateway registers here)
        ingest = web.Application()
        ingest.router.add_post("/api/register", self._h_register)
        ingest.router.add_get("/api/registrations", self._h_registrations)
        ingest.router.add_get("/api/triggers", self._h_triggers)
        ingest.router.add_post("/api/trigger-event", self._h_trigger_event)
        ingest_runner = web.AppRunner(ingest)
        await ingest_runner.setup()
        await web.TCPSite(ingest_runner, "0.0.0.0", self.cfg["listen_port"]).start()
        logger.info("Ingest HTTP on port %d (POST /api/register, POST /api/trigger-event, "
                    "GET /api/triggers)", self.cfg["listen_port"])

        # Status + admin HTTP
        admin = web.Application()
        admin.router.add_get("/status", self._h_status)
        admin.router.add_post("/api/groups/{name}/playlist", self._h_set_playlist)
        admin.router.add_post("/api/groups/{name}", self._h_set_group)
        admin_runner = web.AppRunner(admin)
        await admin_runner.setup()
        await web.TCPSite(admin_runner, "0.0.0.0", self.cfg["status_port"]).start()
        logger.info("Status HTTP on port %d (GET /status)", self.cfg["status_port"])

        self._tasks.append(asyncio.create_task(self.health.run()))
        self._tasks.append(asyncio.create_task(self._reload_loop()))

        for g in self.cfg["groups"]:
            runner = GroupRunner(g, self)
            self.runners[g.name] = runner
            self._tasks.append(asyncio.create_task(runner.run()))

        logger.info("Mur Conductor ready.")
        logger.info("  Gateway status: %s (poll every %ss)",
                    self.cfg["gateway_status_url"], self.cfg["health_poll_interval_s"])
        for g in self.cfg["groups"]:
            logger.info("  Group '%s': trigger='%s' scene='%s' track=%d %d entr(ies) "
                        "%d member(s)%s", g.name, g.trigger_name, g.scene_name, g.track,
                        len(g.playlist), len(g.expected_device_ids),
                        "" if g.enabled else " [disabled]")

        stop_event = asyncio.Event()

        def _signal_handler():
            logger.info("Shutdown signal received")
            self._running = False
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except (NotImplementedError, AttributeError):
                pass   # Windows
        if hasattr(signal, "SIGHUP"):
            try:
                loop.add_signal_handler(signal.SIGHUP, self.request_reload)
            except (NotImplementedError, AttributeError):
                pass

        try:
            await stop_event.wait()
        except KeyboardInterrupt:
            pass

        logger.info("Shutting down - devices will finish the current entry and fall silent")
        self._running = False
        self.health.stop()
        for runner in self.runners.values():
            runner.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.link.close()
        if self._session and not self._session.closed:
            await self._session.close()
        await ingest_runner.cleanup()
        await admin_runner.cleanup()


def preflight_bind_check(port: int, what: str) -> None:
    """Fail fast on a port clash.

    A duplicate conductor would double every downbeat, so this is worth
    catching loudly at startup rather than debugging by ear later.
    """
    test = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        test.bind(("0.0.0.0", port))
    except OSError as e:
        print(f"\n  [FATAL] Cannot bind 0.0.0.0:{port} ({what}): {e}")
        print(f"          Another service is already using this port.")
        print(f"          Check with: ss -lntp 'sport = :{port}'    (Linux)")
        print(f"                  or: netstat -ano | findstr :{port}  (Windows)")
        sys.exit(1)
    finally:
        test.close()


def main() -> None:
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config_path = DEFAULT_CONFIG_PATH
    for i, arg in enumerate(sys.argv):
        if arg == "--config" and i + 1 < len(sys.argv):
            config_path = Path(sys.argv[i + 1]).resolve()

    try:
        cfg = load_config(config_path)
    except ConfigError as e:
        print(f"\n  [FATAL] {e}")
        sys.exit(1)

    preflight_bind_check(cfg["listen_port"], "ingest")
    preflight_bind_check(cfg["status_port"], "status")

    conductor = Conductor(cfg, config_path)
    try:
        asyncio.run(conductor.run())
    except KeyboardInterrupt:
        logger.info("Interrupted")


if __name__ == "__main__":
    main()
