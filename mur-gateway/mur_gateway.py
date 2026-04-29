#!/usr/bin/env python3
"""
Mur Gateway — Protocol gateway between Haven Trigger Server and Murmura devices.

Upstream (Trigger Server):
  - Opens a TCP server socket for the Trigger Server to connect to (TCP_SOCKET protocol)
  - Registers with the Trigger Server via POST /api/register
  - Receives newline-delimited JSON trigger events

Downstream (Mur Devices):
  - Listens for incoming TCP connections from Murmura ESP32 devices
  - Devices announce their ID and subscribe to trigger names
  - Forwards matching trigger events to subscribed devices

Usage:
  python mur_gateway.py --trigger-host 192.168.1.10 --trigger-port 5002
"""

import argparse
import asyncio
import json
import logging
import os
import random
import signal
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web

logger = logging.getLogger("mur-gateway")

VERSION = "1.0"
DEFAULT_DEVICE_PORT = 4000
DEFAULT_UPSTREAM_PORT = 5100
DEFAULT_TRIGGER_PORT = 5002
DEFAULT_STATUS_PORT = 4001
DEFAULT_SCENE_SERVICE_URL = "http://localhost:5003"
DEFAULT_SCENE_CACHE_TTL = 30   # seconds
REREGISTER_INTERVAL = 30       # seconds

# Well-known trigger name fired by scene_service on active-scene change.
SCENE_TRIGGER_NAME = "SceneChange"

# Sync-feature knobs (overridable via config.json — see SYNC_DESIGN.md).
SYNC_DEFAULTS = {
    "fanout_delay_ms": 2500,
    "tsf_query_interval_s": 30,
    "tsf_query_devices_count": 3,
    "tsf_jitter_warn_us": 1000,
    "tsf_map_max_age_s": 120,
}
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def load_sync_config(path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Load the sync-feature knobs from config.json, falling back to built-in
    defaults for any missing field. CLI args do NOT touch these (they live
    here so installs can edit on-site without reflashing or re-deploying).
    """
    cfg = dict(SYNC_DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        if isinstance(user_cfg, dict):
            for k in SYNC_DEFAULTS:
                if k in user_cfg:
                    cfg[k] = user_cfg[k]
            logger.info("Loaded sync config from %s", path)
        else:
            logger.warning("%s is not a JSON object — using defaults", path)
    except FileNotFoundError:
        logger.info("No %s found — using built-in sync defaults", path)
    except json.JSONDecodeError as e:
        logger.warning("Bad JSON in %s (%s) — using built-in sync defaults", path, e)
    return cfg


def parse_iso_to_unix_secs(s: str) -> float:
    """Parse an ISO 8601 string to a unix timestamp (seconds, fractional)."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        # Treat naive timestamps as UTC; sender should use Z or +00:00.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ---------------------------------------------------------------------------
#  Data structures
# ---------------------------------------------------------------------------

@dataclass
class DeviceConnection:
    """A connected Mur device."""
    device_id: str
    writer: asyncio.StreamWriter
    triggers: set = field(default_factory=set)
    connected_at: float = field(default_factory=time.time)
    peer: str = ""
    peer_ip: str = ""

    async def send_line(self, data: str) -> bool:
        """Send a newline-terminated JSON string. Returns False on failure."""
        try:
            self.writer.write((data + "\n").encode("utf-8"))
            await self.writer.drain()
            return True
        except (ConnectionError, OSError) as e:
            logger.warning("Send to %s failed: %s", self.device_id, e)
            return False


@dataclass
class _TsfSample:
    iso_recv: float        # gateway wall-clock timestamp at receive (unix secs)
    tsf_us:   int          # TSF reading reported by the MUR


class TsfMap:
    """ISO ↔ TSF translator.

    Single-AP simplifying assumption: all MURs share one TSF clock (the one
    locked to the AP via beacons). We keep the most recent sample per MUR
    for cross-device jitter checks, and a single canonical sample (most
    recently received from any MUR) for ISO→TSF translation.

    Rate is treated as 1µs/µs (the TSF drift test confirmed this is good to
    sub-ms over 30s). If samples grow stale beyond max_age_s we still
    translate (best effort) but log a warning.

    See SYNC_DESIGN.md for the why.
    """

    def __init__(self, jitter_warn_us: int, max_age_s: float):
        self.per_mur: dict[str, _TsfSample] = {}
        self.canonical: Optional[_TsfSample] = None
        self.jitter_warn_us = int(jitter_warn_us)
        self.max_age_s = float(max_age_s)

    def update(self, mur_id: str, iso_recv: float, tsf_us: int) -> None:
        sample = _TsfSample(iso_recv=iso_recv, tsf_us=int(tsf_us))

        was_empty = self.canonical is None
        prev_age = (time.time() - self.canonical.iso_recv) if self.canonical else 0.0
        was_stale = (not was_empty) and prev_age > self.max_age_s

        self.per_mur[mur_id] = sample
        if self.canonical is None or iso_recv >= self.canonical.iso_recv:
            self.canonical = sample

        if was_empty:
            logger.info("TSF map: first canonical sample from %s (tsf_us=%d)",
                        mur_id, sample.tsf_us)
        elif was_stale:
            logger.info("TSF map: canonical refreshed by %s (was %.0fs stale)",
                        mur_id, prev_age - self.max_age_s)

        self._check_jitter(mur_id, sample)

    def _check_jitter(self, fresh_id: str, fresh: _TsfSample) -> None:
        """If other MURs have recent samples, check pairwise TSF agreement."""
        for other_id, other in self.per_mur.items():
            if other_id == fresh_id:
                continue
            dt = fresh.iso_recv - other.iso_recv
            if abs(dt) > 1.0:
                # Older than 1 second apart — not a meaningful direct comparison.
                continue
            # Project the older sample forward by dt to compare at the same instant.
            other_tsf_at_fresh = other.tsf_us + int(round(dt * 1_000_000))
            jitter = abs(fresh.tsf_us - other_tsf_at_fresh)
            if jitter > self.jitter_warn_us:
                logger.warning(
                    "TSF jitter between %s and %s: %d us (>%d us threshold)",
                    fresh_id, other_id, jitter, self.jitter_warn_us,
                )

    def iso_to_tsf(self, iso_secs: float) -> Optional[int]:
        if self.canonical is None:
            return None
        delta_us = int(round((iso_secs - self.canonical.iso_recv) * 1_000_000))
        return self.canonical.tsf_us + delta_us

    def now_to_tsf(self) -> Optional[int]:
        return self.iso_to_tsf(time.time())

    def is_stale(self) -> bool:
        if self.canonical is None:
            return True
        return (time.time() - self.canonical.iso_recv) > self.max_age_s

    def status(self) -> dict:
        return {
            "have_canonical": self.canonical is not None,
            "canonical_age_seconds": (
                round(time.time() - self.canonical.iso_recv, 1)
                if self.canonical else None
            ),
            "mur_sample_count": len(self.per_mur),
            "max_age_seconds": self.max_age_s,
            "jitter_warn_us": self.jitter_warn_us,
        }


class MurGateway:
    """Main gateway coordinating upstream and downstream connections."""

    def __init__(self, args: argparse.Namespace, sync_cfg: dict):
        self.trigger_host: str = args.trigger_host
        self.trigger_port: int = args.trigger_port
        self.device_port: int = args.device_port
        self.upstream_port: int = args.upstream_port
        self.status_port: int = args.status_port
        self.gateway_name: str = args.name
        self.scene_service_url: str = args.scene_service_url.rstrip("/")
        self.scene_cache_ttl: float = float(args.scene_cache_ttl)

        # Sync feature config (from config.json, see SYNC_DESIGN.md).
        self.fanout_delay_ms: int = int(sync_cfg["fanout_delay_ms"])
        self.tsf_query_interval_s: float = float(sync_cfg["tsf_query_interval_s"])
        self.tsf_query_devices_count: int = int(sync_cfg["tsf_query_devices_count"])
        self.tsf_map: TsfMap = TsfMap(
            jitter_warn_us=int(sync_cfg["tsf_jitter_warn_us"]),
            max_age_s=float(sync_cfg["tsf_map_max_age_s"]),
        )

        # Connected devices keyed by (reader, writer) id for uniqueness
        self.devices: dict[int, DeviceConnection] = {}
        # Trigger name → set of device connection ids
        self.subscriptions: dict[str, set[int]] = {}
        # Upstream connection from Trigger Server
        self.upstream_reader: Optional[asyncio.StreamReader] = None
        self.upstream_writer: Optional[asyncio.StreamWriter] = None
        self._register_task: Optional[asyncio.Task] = None
        self._prime_task: Optional[asyncio.Task] = None
        self._upstream_task: Optional[asyncio.Task] = None
        self._tsf_query_task: Optional[asyncio.Task] = None
        self._running = True

        # Scene cache — primed from scene service at startup, refreshed by
        # SceneChange trigger events (push) and lazy refresh on device query
        # when the cache ages past scene_cache_ttl.
        self.cached_scene: Optional[str] = None
        self.cached_scene_at: float = 0.0   # monotonic timestamp of last cache write
        # Serializes refresh-from-scene-service to avoid thundering-herd HTTP
        # calls when many devices pull right after TTL expiry. Created lazily
        # because asyncio locks must be instantiated inside a running loop.
        self._scene_refresh_lock: Optional[asyncio.Lock] = None

    # -------------------------------------------------------------------
    #  Upstream: Trigger Server connection
    # -------------------------------------------------------------------

    async def _register_with_trigger_server(self):
        """POST /api/register to the Trigger Server so it connects back to us."""
        url = f"http://{self.trigger_host}:{self.trigger_port}/api/register"
        my_ip = self._get_local_ip()
        body = {
            "name": self.gateway_name,
            "host": my_ip,
            "port": self.upstream_port,
            "protocol": "TCP_SOCKET",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status in (200, 201):
                        logger.info("Registered with Trigger Server at %s:%d (our upstream port %d)",
                                    self.trigger_host, self.trigger_port, self.upstream_port)
                    else:
                        text = await resp.text()
                        logger.warning("Registration returned HTTP %d: %s", resp.status, text)
        except Exception as e:
            logger.warning("Cannot register with Trigger Server at %s:%d: %s",
                           self.trigger_host, self.trigger_port, e)

    async def _registration_loop(self):
        """Re-register with the Trigger Server only when not connected."""
        while self._running:
            if self.upstream_writer is None or self.upstream_writer.is_closing():
                await self._register_with_trigger_server()
            await asyncio.sleep(REREGISTER_INTERVAL)

    async def _handle_upstream_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle incoming TCP connection from the Trigger Server."""
        peer = writer.get_extra_info("peername")
        logger.info("Trigger Server connected from %s", peer)

        # Close previous upstream if any
        if self.upstream_writer and not self.upstream_writer.is_closing():
            self.upstream_writer.close()

        self.upstream_reader = reader
        self.upstream_writer = writer

        buf = ""
        try:
            while self._running:
                data = await reader.read(4096)
                if not data:
                    logger.info("Trigger Server disconnected")
                    break
                buf += data.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        await self._handle_trigger_event(line)
        except (ConnectionError, OSError) as e:
            logger.warning("Upstream connection error: %s", e)
        finally:
            writer.close()

    def _resolve_target_tsf(self, event: dict, subscriber_count: int
                            ) -> tuple[Optional[int], Optional[str]]:
        """Translate the time fields on an incoming trigger event into an
        absolute TSF target (or None for "now" passthrough).

        Returns (target_tsf_us, error_msg). target_tsf_us is None to mean
        "send without target_tsf_us" (i.e. fire immediately on the MUR).
        error_msg non-None means drop the event.

        Field rules (mutually exclusive):
        - target_tsf_us: pass through as-is (test/debug)
        - iso_time:      ISO 8601 absolute → translate via TsfMap
        - delta_ms:      milliseconds from now → translate via TsfMap.now_to_tsf
        - none:          if 1 subscriber and not SceneChange, "now";
                         otherwise add fanout_delay_ms.

        SceneChange always gets fanout_delay_ms regardless of subscriber count
        so single-MUR test runs behave identically to multi-MUR production
        (and so a synchronized scene's activation is always deferred-dispatched,
        not fired in the listener's immediate path).

        ===== CRITICAL DESIGN FEATURE — DO NOT "FIX" =====
        The 1-subscriber non-SceneChange passthrough is INTENTIONAL. A single
        MUR receiving a regular trigger has no one to sync with, so it fires
        on receipt for snappy local response. The fanout_delay_ms only kicks
        in when there's actual work for it to do (>1 subscriber) or when the
        sync-scene path requires it (SceneChange). Do not remove this branch
        thinking the delay should be "applied everywhere" — that change has
        been proposed and rejected.

        See SYNC_DESIGN.md.
        """
        has_target = "target_tsf_us" in event
        has_iso    = "iso_time" in event
        has_delta  = "delta_ms" in event
        n = sum([has_target, has_iso, has_delta])
        if n > 1:
            return None, "multiple time fields specified (target_tsf_us, iso_time, delta_ms)"

        if has_target:
            try:
                return int(event["target_tsf_us"]), None
            except (TypeError, ValueError):
                return None, "target_tsf_us is not an integer"

        if has_iso:
            iso = event["iso_time"]
            if not isinstance(iso, str):
                return None, "iso_time must be a string"
            try:
                iso_secs = parse_iso_to_unix_secs(iso)
            except (ValueError, TypeError) as e:
                return None, f"bad iso_time '{iso}': {e}"
            target = self.tsf_map.iso_to_tsf(iso_secs)
            if target is None:
                logger.warning("No TSF map yet — passing iso_time event as 'now'")
                return None, None
            if self.tsf_map.is_stale():
                logger.warning("TSF map is stale (>%ds) — translating anyway",
                               int(self.tsf_map.max_age_s))
            return target, None

        if has_delta:
            try:
                delta_ms = int(event["delta_ms"])
            except (TypeError, ValueError):
                return None, "delta_ms is not an integer"
            base = self.tsf_map.now_to_tsf()
            if base is None:
                logger.warning("No TSF map yet — passing delta_ms event as 'now'")
                return None, None
            return base + delta_ms * 1000, None

        # No time field — implicit "now". SceneChange always gets fanout
        # for predictable sync-scene activation (see docstring).
        is_scene_change = event.get("name") == SCENE_TRIGGER_NAME
        if subscriber_count > 1 or is_scene_change:
            base = self.tsf_map.now_to_tsf()
            if base is None:
                logger.warning(
                    "No TSF map yet — %s falls back to passthrough",
                    "SceneChange" if is_scene_change else "multi-subscriber 'now'",
                )
                return None, None
            return base + self.fanout_delay_ms * 1000, None

        return None, None  # 0 or 1 subscribers, non-SceneChange — direct passthrough

    async def _handle_trigger_event(self, line: str):
        """Parse a trigger event from upstream and fan out to subscribed devices."""
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Bad JSON from Trigger Server: %.100s", line)
            return

        trigger_name = event.get("name")
        if not trigger_name:
            logger.warning("Trigger event missing 'name': %.100s", line)
            return

        trigger_value = event.get("value", "?")
        logger.info("Trigger event: %s value=%s", trigger_name, trigger_value)

        # Fast-path scene cache update: SceneChange events carry the new active
        # scene name in the 'value' field. Update the cache so subsequent
        # get_scene queries answer correctly without an HTTP round-trip.
        # Fan-out to subscribed devices still happens below as normal.
        if trigger_name == SCENE_TRIGGER_NAME:
            new_value = event.get("value")
            new_scene = new_value if isinstance(new_value, str) and new_value else None
            self.cached_scene = new_scene
            self.cached_scene_at = time.monotonic()
            logger.info("Scene cache updated from trigger: %s", new_scene)

        # Find all device connections subscribed to this trigger
        conn_ids = self.subscriptions.get(trigger_name, set())
        if not conn_ids:
            logger.info("No subscribers for trigger '%s'", trigger_name)
            return

        # Translate time fields → absolute TSF target (or None for "now").
        target_tsf_us, err = self._resolve_target_tsf(event, len(conn_ids))
        if err is not None:
            logger.warning("Dropping trigger '%s': %s", trigger_name, err)
            return

        out_event = dict(event)
        if target_tsf_us is not None:
            out_event["target_tsf_us"] = int(target_tsf_us)
        # Drop incoming-only fields so we don't ship redundant time info to MURs.
        out_event.pop("iso_time", None)
        out_event.pop("delta_ms", None)
        out_line = json.dumps(out_event)

        # Fan out — send re-serialized line (with target_tsf_us injected if any).
        failed = []
        sent_count = 0
        for conn_id in list(conn_ids):
            device = self.devices.get(conn_id)
            if device:
                ok = await device.send_line(out_line)
                if ok:
                    logger.info("  → forwarded to %s%s", device.device_id,
                                f" target_tsf_us={target_tsf_us}" if target_tsf_us else "")
                    sent_count += 1
                else:
                    failed.append(conn_id)

        logger.info("Trigger '%s' sent to %d device(s)", trigger_name, sent_count)

        # Clean up failed connections
        for conn_id in failed:
            self._remove_device(conn_id)

    # -------------------------------------------------------------------
    #  Downstream: Mur device connections
    # -------------------------------------------------------------------

    async def _handle_device_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle an incoming TCP connection from a Mur device."""
        peer = writer.get_extra_info("peername")
        conn_id = id(writer)
        peer_ip = peer[0] if peer else None
        logger.info("Device connection from %s", peer)

        # Enable TCP keepalive so the OS detects dead connections faster
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "TCP_KEEPIDLE"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 5)
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
            if hasattr(socket, "TCP_KEEPCNT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)

        # Evict any existing connections from the same IP address
        if peer_ip:
            stale = [
                cid for cid, d in self.devices.items()
                if d.peer_ip == peer_ip
            ]
            for cid in stale:
                stale_dev = self.devices.get(cid)
                if stale_dev:
                    logger.info("Evicting stale connection for %s from %s", stale_dev.device_id, peer_ip)
                    stale_dev.writer.close()
                self._remove_device(cid)

        device = DeviceConnection(
            device_id="(unannounced)",
            writer=writer,
            peer=str(peer),
            peer_ip=peer_ip or "",
        )
        self.devices[conn_id] = device

        buf = ""
        try:
            while self._running:
                data = await reader.read(4096)
                if not data:
                    logger.info("Device %s disconnected", device.device_id)
                    break
                buf += data.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        self._process_device_message(conn_id, line)
        except (ConnectionError, OSError) as e:
            logger.info("Device %s connection error: %s", device.device_id, e)
        finally:
            self._remove_device(conn_id)
            writer.close()

    def _process_device_message(self, conn_id: int, line: str):
        """Parse and handle a message from a Mur device."""
        device = self.devices.get(conn_id)
        if not device:
            return

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Bad JSON from device %s: %.100s", device.device_id, line)
            return

        msg_type = msg.get("type")

        if msg_type == "announce":
            device_id = msg.get("id", "(unknown)")
            device.device_id = device_id
            logger.info("Device announced: %s from %s", device_id, device.peer)
            # Seed the TSF map if the announce carried a tsf_us reading.
            tsf_us = msg.get("tsf_us")
            if isinstance(tsf_us, (int, float)) and tsf_us > 0:
                self.tsf_map.update(device_id, time.time(), int(tsf_us))
                logger.info("TSF map: seeded from %s announce (tsf_us=%d)",
                            device_id, int(tsf_us))
            else:
                logger.info("TSF map: %s announced without tsf_us (firmware predates sync feature?)",
                            device_id)
            # Send welcome
            welcome = json.dumps({"type": "welcome", "gateway": "mur-gateway", "version": VERSION})
            asyncio.ensure_future(device.send_line(welcome))

        elif msg_type == "tsf_reply":
            tsf_us = msg.get("tsf_us")
            if isinstance(tsf_us, (int, float)) and tsf_us > 0:
                self.tsf_map.update(device.device_id, time.time(), int(tsf_us))
                logger.info("TSF map: reply from %s (tsf_us=%d)",
                            device.device_id, int(tsf_us))
            else:
                logger.warning("tsf_reply from %s missing/invalid tsf_us", device.device_id)

        elif msg_type == "subscribe":
            triggers = msg.get("triggers", [])
            if isinstance(triggers, list):
                # Treat subscribe as the authoritative set for this device.
                # The device sends its complete current trigger list on every
                # config change; if we accumulated, names dropped by the
                # device would leak forever and force a reboot to clear.
                for old_name in list(device.triggers):
                    subs = self.subscriptions.get(old_name)
                    if subs is not None:
                        subs.discard(conn_id)
                        if not subs:
                            del self.subscriptions[old_name]
                device.triggers.clear()

                for t in triggers:
                    device.triggers.add(t)
                    if t not in self.subscriptions:
                        self.subscriptions[t] = set()
                    self.subscriptions[t].add(conn_id)
                logger.info("Device %s subscribed to: %s", device.device_id, triggers)

        elif msg_type == "unsubscribe":
            triggers = msg.get("triggers", [])
            if isinstance(triggers, list):
                for t in triggers:
                    device.triggers.discard(t)
                    if t in self.subscriptions:
                        self.subscriptions[t].discard(conn_id)
                        if not self.subscriptions[t]:
                            del self.subscriptions[t]
                logger.info("Device %s unsubscribed from: %s", device.device_id, triggers)

        elif msg_type == "get_scene":
            # Device pull: answer from cache, lazily refreshing from scene
            # service if the cache is older than scene_cache_ttl.
            asyncio.ensure_future(self._answer_get_scene(conn_id))

        else:
            logger.warning("Unknown message type '%s' from device %s", msg_type, device.device_id)

    # -------------------------------------------------------------------
    #  Scene cache
    # -------------------------------------------------------------------

    async def _answer_get_scene(self, conn_id: int):
        """Respond to a device's get_scene query.

        Lazy refresh: if the cache is stale, attempt one HTTP fetch from the
        Scene Service under a lock (so concurrent device queries collapse into
        a single upstream call). On refresh failure, keep the stale value and
        still answer — devices prefer stale truth to silence.

        Cache invariant: cached_scene is updated synchronously the moment a
        SceneChange trigger arrives (see _handle_trigger_event). Combined
        with TCP per-connection FIFO, this means we never hand a MUR an
        old scene name after that MUR's TCP connection has already been
        sent the new trigger. See SYNC_DESIGN.md.
        """
        age = time.monotonic() - self.cached_scene_at
        if age >= self.scene_cache_ttl:
            await self._refresh_scene_cache()

        device = self.devices.get(conn_id)
        if not device:
            return
        resp = json.dumps({"type": "scene", "value": self.cached_scene})
        await device.send_line(resp)
        logger.debug("Answered get_scene → %s (value=%s)", device.device_id, self.cached_scene)

    async def _refresh_scene_cache(self):
        """Fetch current scene from the Scene Service (one in-flight call max)."""
        if self._scene_refresh_lock is None:
            self._scene_refresh_lock = asyncio.Lock()
        async with self._scene_refresh_lock:
            # Re-check age inside the lock — another coroutine may have just
            # refreshed while we were waiting.
            if time.monotonic() - self.cached_scene_at < self.scene_cache_ttl:
                return
            url = f"{self.scene_service_url}/api/scenes/active"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            active = data.get("active_scene")
                            self.cached_scene = active if isinstance(active, str) and active else None
                            self.cached_scene_at = time.monotonic()
                            logger.debug("Scene cache refreshed: %s", self.cached_scene)
                        else:
                            logger.warning("Scene service returned HTTP %d; keeping stale cache (%s)",
                                           resp.status, self.cached_scene)
            except Exception as e:
                logger.warning("Scene service unreachable (%s); keeping stale cache (%s)",
                               e, self.cached_scene)

    async def _prime_scene_cache(self):
        """Background task: prime the scene cache at startup with retries.

        Mirrors the scene_service → trigger_gateway registration retry pattern
        (30 attempts × 2 s) so a startup race between the scene service and
        this gateway doesn't leave the cache empty forever.
        """
        MAX_ATTEMPTS = 30
        RETRY_DELAY = 2
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if not self._running:
                return
            url = f"{self.scene_service_url}/api/scenes/active"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            active = data.get("active_scene")
                            self.cached_scene = active if isinstance(active, str) and active else None
                            self.cached_scene_at = time.monotonic()
                            logger.info("Scene cache primed: %s (attempt %d)",
                                        self.cached_scene, attempt)
                            return
                        logger.warning("Scene service prime got HTTP %d (attempt %d/%d)",
                                       resp.status, attempt, MAX_ATTEMPTS)
            except Exception as e:
                logger.warning("Scene service prime failed (attempt %d/%d): %s",
                               attempt, MAX_ATTEMPTS, e)
            await asyncio.sleep(RETRY_DELAY)
        logger.error("Gave up priming scene cache from %s after %d attempts.",
                     self.scene_service_url, MAX_ATTEMPTS)

    # -------------------------------------------------------------------
    #  TSF pull loop
    # -------------------------------------------------------------------

    async def _tsf_query_loop(self):
        """Periodically pull TSF readings from a sample of connected MURs to
        keep the ISO↔TSF map fresh. Cadence and sample size are config knobs;
        all MURs share one AP, so any MUR's TSF is canonical (we sample
        several for jitter checking — see TsfMap._check_jitter).

        Logs an INFO heartbeat each cycle showing how many devices were
        queried and the current map state (empty/fresh/stale, canonical age,
        sample count). This is the operator's primary visibility into the
        sync subsystem.
        """
        # Initial delay so the first announce flurry has settled.
        await asyncio.sleep(min(5.0, self.tsf_query_interval_s))
        query_msg = json.dumps({"type": "tsf_query"})
        while self._running:
            try:
                ids = list(self.devices.keys())
                k = 0
                if ids:
                    k = min(self.tsf_query_devices_count, len(ids))
                    chosen = random.sample(ids, k)
                    for conn_id in chosen:
                        device = self.devices.get(conn_id)
                        if device:
                            await device.send_line(query_msg)

                status = self.tsf_map.status()
                if not status["have_canonical"]:
                    state = "empty"
                elif self.tsf_map.is_stale():
                    state = "stale"
                else:
                    state = "fresh"
                age = status["canonical_age_seconds"]
                age_str = f"{age:.1f}s" if age is not None else "n/a"
                logger.info(
                    "TSF pull: queried %d/%d device(s); map %s "
                    "(canonical age %s, %d MUR sample(s))",
                    k, len(ids), state, age_str, status["mur_sample_count"],
                )
            except Exception as e:
                logger.warning("tsf_query loop iteration failed: %s", e)
            await asyncio.sleep(self.tsf_query_interval_s)

    def _remove_device(self, conn_id: int):
        """Remove a device and clean up its subscriptions."""
        device = self.devices.pop(conn_id, None)
        if not device:
            return

        for t in device.triggers:
            if t in self.subscriptions:
                self.subscriptions[t].discard(conn_id)
                if not self.subscriptions[t]:
                    del self.subscriptions[t]

        logger.info("Removed device %s (%s)", device.device_id, device.peer)

    # -------------------------------------------------------------------
    #  HTTP status endpoint
    # -------------------------------------------------------------------

    async def _handle_status(self, request: web.Request) -> web.Response:
        """GET /status — show connected devices and subscriptions."""
        devices = []
        for conn_id, dev in self.devices.items():
            devices.append({
                "id": dev.device_id,
                "peer": dev.peer,
                "triggers": sorted(dev.triggers),
                "connected_at": dev.connected_at,
                "uptime_seconds": round(time.time() - dev.connected_at, 1),
            })

        upstream_connected = (
            self.upstream_writer is not None
            and not self.upstream_writer.is_closing()
        )

        scene_age = (
            round(time.monotonic() - self.cached_scene_at, 1)
            if self.cached_scene_at > 0 else None
        )
        body = {
            "gateway": self.gateway_name,
            "version": VERSION,
            "trigger_server": f"{self.trigger_host}:{self.trigger_port}",
            "upstream_connected": upstream_connected,
            "device_port": self.device_port,
            "devices": devices,
            "device_count": len(devices),
            "subscriptions": {k: len(v) for k, v in self.subscriptions.items()},
            "scene_service_url": self.scene_service_url,
            "cached_scene": self.cached_scene,
            "cached_scene_age_seconds": scene_age,
            "scene_cache_ttl": self.scene_cache_ttl,
            "sync": {
                "fanout_delay_ms": self.fanout_delay_ms,
                "tsf_query_interval_s": self.tsf_query_interval_s,
                "tsf_query_devices_count": self.tsf_query_devices_count,
                "tsf_map": self.tsf_map.status(),
            },
        }
        return web.json_response(body)

    async def _handle_triggers(self, request: web.Request) -> web.Response:
        """GET /triggers — proxy trigger list from upstream Trigger Server."""
        url = f"http://{self.trigger_host}:{self.trigger_port}/api/triggers"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        triggers = data.get("triggers", [])
                        names = sorted(t.get("name", "") for t in triggers if t.get("name"))
                        # Include type info for filtering (e.g. Discrete-only for scene triggers)
                        typed = sorted(
                            [{"name": t["name"], "type": t.get("type", ""),
                              **({"range": t["range"]} if "range" in t else {})}
                             for t in triggers if t.get("name")],
                            key=lambda x: x["name"],
                        )
                        return web.json_response({"trigger_names": names, "triggers": typed})
                    else:
                        return web.json_response(
                            {"error": f"Trigger Server returned HTTP {resp.status}"},
                            status=502,
                        )
        except Exception as e:
            logger.warning("Failed to fetch triggers from %s: %s", url, e)
            return web.json_response(
                {"error": f"Cannot reach Trigger Server: {e}"},
                status=502,
            )

    # -------------------------------------------------------------------
    #  Utilities
    # -------------------------------------------------------------------

    def _get_local_ip(self) -> str:
        """Best-effort determination of our local IP address."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((self.trigger_host, self.trigger_port))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    # -------------------------------------------------------------------
    #  Main run
    # -------------------------------------------------------------------

    async def run(self):
        """Start all servers and the registration loop."""
        # Start upstream TCP server (for Trigger Server to connect to)
        upstream_server = await asyncio.start_server(
            self._handle_upstream_connection,
            "0.0.0.0",
            self.upstream_port,
        )
        logger.info("Upstream listener on port %d (for Trigger Server)", self.upstream_port)

        # Start downstream TCP server (for Mur devices)
        device_server = await asyncio.start_server(
            self._handle_device_connection,
            "0.0.0.0",
            self.device_port,
        )
        logger.info("Device listener on port %d (for Mur devices)", self.device_port)

        # Start HTTP status server
        status_app = web.Application()
        status_app.router.add_get("/status", self._handle_status)
        status_app.router.add_get("/triggers", self._handle_triggers)
        runner = web.AppRunner(status_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.status_port)
        await site.start()
        logger.info("Status HTTP on port %d (GET /status, GET /triggers)", self.status_port)

        # Start registration loop
        self._register_task = asyncio.create_task(self._registration_loop())

        # Prime the scene cache from the Scene Service (background, with retry)
        self._prime_task = asyncio.create_task(self._prime_scene_cache())

        # Start the periodic TSF pull loop
        self._tsf_query_task = asyncio.create_task(self._tsf_query_loop())

        logger.info("Mur Gateway ready.")
        logger.info("  Trigger Server: %s:%d", self.trigger_host, self.trigger_port)
        logger.info("  Upstream port:  %d", self.upstream_port)
        logger.info("  Device port:    %d", self.device_port)
        logger.info("  Status port:    %d", self.status_port)
        logger.info("  Scene Service:  %s (cache TTL %.0fs)",
                    self.scene_service_url, self.scene_cache_ttl)
        logger.info("  Sync:           fanout %d ms, tsf_query every %.0fs (sample %d)",
                    self.fanout_delay_ms, self.tsf_query_interval_s, self.tsf_query_devices_count)

        # Wait until shutdown
        stop_event = asyncio.Event()

        def _signal_handler():
            logger.info("Shutdown signal received")
            self._running = False
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

        try:
            await stop_event.wait()
        except KeyboardInterrupt:
            pass

        # Cleanup
        logger.info("Shutting down...")
        self._register_task.cancel()
        if self._prime_task:
            self._prime_task.cancel()
        if self._tsf_query_task:
            self._tsf_query_task.cancel()
        upstream_server.close()
        device_server.close()
        await runner.cleanup()

        # Close all device connections
        for conn_id, dev in list(self.devices.items()):
            dev.writer.close()
        self.devices.clear()
        self.subscriptions.clear()


# ---------------------------------------------------------------------------
#  CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Mur Gateway — bridge between Haven Trigger Server and Murmura devices"
    )
    parser.add_argument("--trigger-host", default="localhost",
                        help="Haven Trigger Server IP/hostname (default: localhost)")
    parser.add_argument("--trigger-port", type=int, default=DEFAULT_TRIGGER_PORT,
                        help=f"Haven Trigger Server port (default: {DEFAULT_TRIGGER_PORT})")
    parser.add_argument("--device-port", type=int, default=DEFAULT_DEVICE_PORT,
                        help=f"TCP port for Mur device connections (default: {DEFAULT_DEVICE_PORT})")
    parser.add_argument("--upstream-port", type=int, default=DEFAULT_UPSTREAM_PORT,
                        help=f"TCP port for Trigger Server connection (default: {DEFAULT_UPSTREAM_PORT})")
    parser.add_argument("--status-port", type=int, default=DEFAULT_STATUS_PORT,
                        help=f"HTTP port for status endpoint (default: {DEFAULT_STATUS_PORT})")
    parser.add_argument("--name", default="mur-gateway",
                        help="Gateway name used in Trigger Server registration (default: mur-gateway)")
    parser.add_argument("--scene-service-url", default=DEFAULT_SCENE_SERVICE_URL,
                        help=f"Scene Service base URL for get_scene queries (default: {DEFAULT_SCENE_SERVICE_URL})")
    parser.add_argument("--scene-cache-ttl", type=float, default=DEFAULT_SCENE_CACHE_TTL,
                        help=f"Scene cache freshness window in seconds (default: {DEFAULT_SCENE_CACHE_TTL})")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    sync_cfg = load_sync_config()
    gateway = MurGateway(args, sync_cfg)

    if sys.platform == "win32":
        # Windows needs ProactorEventLoop for subprocess support,
        # but SelectorEventLoop is fine for sockets and is the default in 3.12+
        # Just handle KeyboardInterrupt gracefully
        try:
            asyncio.run(gateway.run())
        except KeyboardInterrupt:
            logger.info("Interrupted")
    else:
        asyncio.run(gateway.run())


if __name__ == "__main__":
    main()
