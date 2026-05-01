#!/usr/bin/env python3
"""
Mur Abstract Gateway — drop-in replacement for mur_gateway with an "abstract
trigger" layer on top.

Why:
  Murmura ESP32 devices are limited to 3 tracks, each with one trigger name.
  Real installations need ~5 buttons mapped to one device. We can't ship new
  firmware to deployed units, so the gateway fans out N upstream triggers to
  one logical "AbstractTriggerFoo" by swapping the device's track-2 file_path
  over HTTP and dispatching a OneShot under the abstract name.

Layered on top of mur_gateway behavior:
  - Upstream events whose name appears in any abstract trigger's mappings
    additionally fire the abstract pipeline (POST /api/scenes, then send a
    renamed event to abstract-subscribers).
  - Upstream events not mapped to any abstract trigger pass through as
    plain mur_gateway behavior — devices subscribed to the raw name still
    receive raw events.

See README.md and the plan doc.
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
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web

logger = logging.getLogger("mur-abs-gateway")

VERSION = "1.0"
DEFAULT_DEVICE_PORT = 4000
DEFAULT_UPSTREAM_PORT = 5100
DEFAULT_TRIGGER_PORT = 5002
DEFAULT_STATUS_PORT = 4001
DEFAULT_UI_PORT = 5101
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
    # Abstract-gateway additions:
    "abstract_track_index": 2,
    "ui_port": DEFAULT_UI_PORT,
    "file_cache_ttl_s": 600,
}
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
DEFAULT_ABSTRACT_PATH = Path(__file__).resolve().parent / "abstract_triggers.json"

# Ring-buffer size for the live event log surfaced to the UI.
LOG_BUFFER_SIZE = 50

# Fallback scene name if the gateway has no cached scene yet — matches the
# firmware default first-boot scene name.
DEFAULT_SCENE_FALLBACK = "default"


def load_sync_config(path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Load gateway knobs from config.json, falling back to built-in defaults
    for any missing field. CLI args do NOT touch these.
    """
    cfg = dict(SYNC_DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        if isinstance(user_cfg, dict):
            for k in SYNC_DEFAULTS:
                if k in user_cfg:
                    cfg[k] = user_cfg[k]
            logger.info("Loaded config from %s", path)
        else:
            logger.warning("%s is not a JSON object — using defaults", path)
    except FileNotFoundError:
        logger.info("No %s found — using built-in defaults", path)
    except json.JSONDecodeError as e:
        logger.warning("Bad JSON in %s (%s) — using built-in defaults", path, e)
    return cfg


def parse_iso_to_unix_secs(s: str) -> float:
    """Parse an ISO 8601 string to a unix timestamp (seconds, fractional)."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ---------------------------------------------------------------------------
#  Data structures (inherited from mur_gateway)
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
        try:
            self.writer.write((data + "\n").encode("utf-8"))
            await self.writer.drain()
            return True
        except (ConnectionError, OSError) as e:
            logger.warning("Send to %s failed: %s", self.device_id, e)
            return False


@dataclass
class _TsfSample:
    iso_recv: float
    tsf_us:   int


class TsfMap:
    """ISO ↔ TSF translator. See SYNC_DESIGN.md."""

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
        for other_id, other in self.per_mur.items():
            if other_id == fresh_id:
                continue
            dt = fresh.iso_recv - other.iso_recv
            if abs(dt) > 1.0:
                continue
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


# ---------------------------------------------------------------------------
#  Abstract-gateway support classes (NEW)
# ---------------------------------------------------------------------------

class AbstractTriggerRegistry:
    """In-memory abstract-trigger config, backed by abstract_triggers.json.

    Source of truth is the JSON file. The UI reads/writes through this class
    and the file gets atomically rewritten on every change. The reverse index
    `upstream_trigger -> set(abstract_name)` is rebuilt after every load.
    """

    def __init__(self, path: Path):
        self.path = path
        # abstract_name -> {"associated_device_id": str|None,
        #                   "mappings": {upstream_name: file_path}}
        self.triggers: dict[str, dict] = {}
        # upstream_name -> set(abstract_name)
        self.reverse_index: dict[str, set[str]] = {}

    def load(self) -> None:
        if not self.path.exists():
            logger.info("No %s — starting with empty abstract config", self.path)
            self.triggers = {}
            self._rebuild_reverse_index()
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Failed to read %s: %s — keeping previous config", self.path, e)
            return
        try:
            self.triggers = self._validate(data)
        except ValueError as e:
            logger.error("Invalid %s: %s — keeping previous config", self.path, e)
            return
        self._rebuild_reverse_index()
        logger.info("Loaded %d abstract trigger(s) from %s",
                    len(self.triggers), self.path)

    def save(self, data: dict) -> None:
        """Validate, then atomically write the new config."""
        validated = self._validate(data)
        out = {
            "version": 1,
            "abstract_triggers": {
                name: {
                    "associated_device_id": cfg.get("associated_device_id"),
                    "mappings": [
                        {"upstream": u, "file_path": fp}
                        for u, fp in cfg["mappings"].items()
                    ],
                }
                for name, cfg in validated.items()
            },
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, self.path)
        self.triggers = validated
        self._rebuild_reverse_index()
        logger.info("Saved %d abstract trigger(s) to %s",
                    len(self.triggers), self.path)

    @staticmethod
    def _validate(raw: dict) -> dict:
        """Normalize raw JSON (either on-disk or POSTed) into the canonical
        in-memory shape. Raises ValueError on any structural problem.
        """
        if not isinstance(raw, dict):
            raise ValueError("config must be a JSON object")
        atriggers = raw.get("abstract_triggers", {})
        if not isinstance(atriggers, dict):
            raise ValueError("'abstract_triggers' must be an object")

        out: dict[str, dict] = {}
        for name, cfg in atriggers.items():
            if not isinstance(name, str) or not name:
                raise ValueError(f"abstract trigger name must be a non-empty string (got {name!r})")
            if not isinstance(cfg, dict):
                raise ValueError(f"abstract trigger {name!r} must be an object")

            assoc = cfg.get("associated_device_id")
            if assoc is not None and not isinstance(assoc, str):
                raise ValueError(f"{name}.associated_device_id must be a string or null")

            mappings_raw = cfg.get("mappings", [])
            if not isinstance(mappings_raw, list):
                raise ValueError(f"{name}.mappings must be a list")

            mappings: dict[str, str] = {}
            for i, m in enumerate(mappings_raw):
                if not isinstance(m, dict):
                    raise ValueError(f"{name}.mappings[{i}] must be an object")
                upstream = m.get("upstream")
                file_path = m.get("file_path")
                if not isinstance(upstream, str) or not upstream:
                    raise ValueError(f"{name}.mappings[{i}].upstream must be a non-empty string")
                if not isinstance(file_path, str) or not file_path:
                    raise ValueError(f"{name}.mappings[{i}].file_path must be a non-empty string")
                if upstream in mappings:
                    raise ValueError(f"{name}.mappings has duplicate upstream {upstream!r}")
                mappings[upstream] = file_path

            out[name] = {
                "associated_device_id": assoc,
                "mappings": mappings,
            }
        return out

    def _rebuild_reverse_index(self) -> None:
        rev: dict[str, set[str]] = {}
        for abstract_name, cfg in self.triggers.items():
            for upstream in cfg["mappings"]:
                rev.setdefault(upstream, set()).add(abstract_name)
        self.reverse_index = rev

    def to_json_dict(self) -> dict:
        """Serializable form returned by GET /api/abstract-triggers."""
        return {
            "version": 1,
            "abstract_triggers": {
                name: {
                    "associated_device_id": cfg["associated_device_id"],
                    "mappings": [
                        {"upstream": u, "file_path": fp}
                        for u, fp in cfg["mappings"].items()
                    ],
                }
                for name, cfg in self.triggers.items()
            },
        }


class FilePathCache:
    """Per-device track-file_path cache with a TTL.

    The cache lets us skip the POST /api/scenes call when we already set the
    same file on the same device's track recently. The user explicitly OK'd
    the assumption that if mur-config-server hasn't been used and the device
    hasn't rebooted within the TTL, our last-set value is still correct.
    """

    def __init__(self, ttl_s: float):
        self.ttl_s = float(ttl_s)
        self._store: dict[tuple[str, int], tuple[str, float]] = {}

    def get(self, device_id: str, track_index: int) -> Optional[str]:
        entry = self._store.get((device_id, track_index))
        if entry is None:
            return None
        file_path, set_at = entry
        if time.monotonic() - set_at > self.ttl_s:
            return None
        return file_path

    def set(self, device_id: str, track_index: int, file_path: str) -> None:
        self._store[(device_id, track_index)] = (file_path, time.monotonic())

    def invalidate_device(self, device_id: str) -> None:
        for key in [k for k in self._store if k[0] == device_id]:
            self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()


class EventRingBuffer:
    """Last-N events plus per-subscriber asyncio.Queues for SSE streaming.

    Each SSE client gets a dedicated queue (bounded). On overflow we drop
    oldest — the live log is best-effort, never a back-pressure source.
    """

    def __init__(self, size: int = LOG_BUFFER_SIZE):
        self._buf: deque[dict] = deque(maxlen=size)
        self._subscribers: list[asyncio.Queue] = []

    def push(self, entry: dict) -> None:
        self._buf.append(entry)
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                pass

    def recent(self, n: int) -> list[dict]:
        if n <= 0 or n >= len(self._buf):
            return list(self._buf)
        return list(self._buf)[-n:]

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass


# ---------------------------------------------------------------------------
#  Main gateway
# ---------------------------------------------------------------------------

class MurAbsGateway:
    """mur_gateway + abstract trigger layer + UI."""

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

        # Abstract-gateway config.
        self.abstract_track_index: int = int(sync_cfg["abstract_track_index"])
        self.ui_port: int = int(sync_cfg["ui_port"])
        self.file_cache_ttl_s: float = float(sync_cfg["file_cache_ttl_s"])

        # Connected devices keyed by id(writer).
        self.devices: dict[int, DeviceConnection] = {}
        # Trigger name → set of device connection ids.
        self.subscriptions: dict[str, set[int]] = {}
        # Upstream connection from Trigger Server.
        self.upstream_reader: Optional[asyncio.StreamReader] = None
        self.upstream_writer: Optional[asyncio.StreamWriter] = None
        self._register_task: Optional[asyncio.Task] = None
        self._prime_task: Optional[asyncio.Task] = None
        self._upstream_task: Optional[asyncio.Task] = None
        self._tsf_query_task: Optional[asyncio.Task] = None
        self._running = True

        # Scene cache.
        self.cached_scene: Optional[str] = None
        self.cached_scene_at: float = 0.0
        self._scene_refresh_lock: Optional[asyncio.Lock] = None

        # Abstract-trigger state.
        self.abstract_registry = AbstractTriggerRegistry(DEFAULT_ABSTRACT_PATH)
        self.file_cache = FilePathCache(self.file_cache_ttl_s)
        self.event_log = EventRingBuffer(LOG_BUFFER_SIZE)
        # Shared aiohttp session for outbound POSTs to devices, lazily
        # created (must be inside the running loop).
        self._http_session: Optional[aiohttp.ClientSession] = None

    # -------------------------------------------------------------------
    #  Upstream: Trigger Server connection
    # -------------------------------------------------------------------

    async def _register_with_trigger_server(self):
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
        while self._running:
            if self.upstream_writer is None or self.upstream_writer.is_closing():
                await self._register_with_trigger_server()
            await asyncio.sleep(REREGISTER_INTERVAL)

    async def _handle_upstream_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        logger.info("Trigger Server connected from %s", peer)

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

        ===== CRITICAL DESIGN FEATURE — DO NOT "FIX" =====
        The 1-subscriber non-SceneChange passthrough is INTENTIONAL. A single
        MUR receiving a regular trigger has no one to sync with, so it fires
        on receipt for snappy local response. The fanout_delay_ms only kicks
        in when there's actual work for it to do (>1 subscriber) or when the
        sync-scene path requires it (SceneChange). Do not remove this branch
        thinking the delay should be "applied everywhere" — that change has
        been proposed and rejected. See SYNC_DESIGN.md.
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

        return None, None

    async def _handle_trigger_event(self, line: str):
        """Parse a trigger event from upstream, fire any abstract triggers
        that map this name, then fan out to direct subscribers as before.
        """
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Bad JSON from Trigger Server: %.100s", line)
            return

        trigger_name = event.get("name")
        if not trigger_name:
            logger.warning("Trigger event missing 'name': %.100s", line)
            return

        # Capture the raw upstream value once for log entries; later conversion
        # may strip it from `event` before any per-path logging runs.
        raw_value = event.get("value")
        log_value = raw_value if raw_value is not None else None

        trigger_value = raw_value if raw_value is not None else "?"
        logger.info("Trigger event: %s value=%s", trigger_name, trigger_value)

        if trigger_name == SCENE_TRIGGER_NAME:
            new_value = event.get("value")
            new_scene = new_value if isinstance(new_value, str) and new_value else None
            self.cached_scene = new_scene
            self.cached_scene_at = time.monotonic()
            logger.info("Scene cache updated from trigger: %s", new_scene)

        # On/Off → OneShot conversion (cloned verbatim from mur_gateway).
        v_norm = raw_value.strip().lower() if isinstance(raw_value, str) else raw_value
        if v_norm in ("off", "0", 0, False):
            logger.info("Dropping Off-event for '%s' (gateway converts On/Off to OneShot)",
                        trigger_name)
            self._log_event({
                "ts": time.time(),
                "kind": "fire",
                "upstream": trigger_name,
                "abstract": None,
                "file_path": None,
                "devices": [],
                "status": "dropped_off",
                "value": log_value,
            })
            return
        if v_norm in ("on", "1", 1, True):
            event.pop("value", None)
            logger.debug("Converted On-event for '%s' to OneShot dispatch", trigger_name)

        # Abstract trigger interception: fire each abstract trigger whose
        # mapping list contains this upstream name. Runs additively — the
        # raw fan-out below still happens for any device subscribed to the
        # raw upstream name.
        abstract_names = self.abstract_registry.reverse_index.get(trigger_name, set())
        for abstract_name in list(abstract_names):
            await self._fire_abstract_trigger(abstract_name, trigger_name, event, log_value)

        # Direct fan-out to raw-name subscribers (preserved mur_gateway semantics).
        conn_ids = self.subscriptions.get(trigger_name, set())
        if not conn_ids:
            if not abstract_names:
                logger.info("No subscribers for trigger '%s'", trigger_name)
                # Surface unmapped/no-subscriber events to the UI log too —
                # operators want to see every upstream event, not just the
                # ones we forwarded.
                self._log_event({
                    "ts": time.time(),
                    "kind": "fire",
                    "upstream": trigger_name,
                    "abstract": None,
                    "file_path": None,
                    "devices": [],
                    "status": "no_subscribers",
                    "value": log_value,
                })
            return

        target_tsf_us, err = self._resolve_target_tsf(event, len(conn_ids))
        if err is not None:
            logger.warning("Dropping trigger '%s': %s", trigger_name, err)
            self._log_event({
                "ts": time.time(),
                "kind": "fire",
                "upstream": trigger_name,
                "abstract": None,
                "file_path": None,
                "devices": [],
                "status": f"dropped: {err}",
                "value": log_value,
            })
            return

        out_event = dict(event)
        if target_tsf_us is not None:
            out_event["target_tsf_us"] = int(target_tsf_us)
        out_event.pop("iso_time", None)
        out_event.pop("delta_ms", None)
        out_line = json.dumps(out_event)

        failed = []
        sent_count = 0
        sent_ids: list[str] = []
        for conn_id in list(conn_ids):
            device = self.devices.get(conn_id)
            if device:
                ok = await device.send_line(out_line)
                if ok:
                    logger.info("  → forwarded to %s%s", device.device_id,
                                f" target_tsf_us={target_tsf_us}" if target_tsf_us else "")
                    sent_count += 1
                    sent_ids.append(device.device_id)
                else:
                    failed.append(conn_id)

        logger.info("Trigger '%s' sent to %d device(s)", trigger_name, sent_count)
        if not abstract_names:
            self._log_event({
                "ts": time.time(),
                "kind": "fire",
                "upstream": trigger_name,
                "abstract": None,
                "file_path": None,
                "devices": sent_ids,
                "status": "ok" if sent_count else "no_subscribers",
                "value": log_value,
            })

        for conn_id in failed:
            self._remove_device(conn_id)

    # -------------------------------------------------------------------
    #  Abstract-trigger dispatch (NEW)
    # -------------------------------------------------------------------

    async def _fire_abstract_trigger(self, abstract_name: str,
                                     upstream_name: str, event: dict,
                                     log_value=None) -> None:
        """Resolve the file_path for this abstract trigger, POST it to each
        subscribed device's track, then dispatch a OneShot-shaped event under
        the abstract name to those same devices.

        log_value is the raw upstream value before On/Off conversion stripped
        it; passed in for the live log so operators see what came in.
        """
        cfg = self.abstract_registry.triggers.get(abstract_name)
        if cfg is None:
            logger.warning("Abstract trigger '%s' vanished mid-dispatch", abstract_name)
            return
        file_path = cfg["mappings"].get(upstream_name)
        if file_path is None:
            logger.warning("Abstract trigger '%s' has no mapping for upstream '%s'",
                           abstract_name, upstream_name)
            return

        conn_ids = self.subscriptions.get(abstract_name, set())
        if not conn_ids:
            logger.info("Abstract '%s' fired but no devices subscribed", abstract_name)
            self._log_event({
                "ts": time.time(),
                "kind": "fire",
                "upstream": upstream_name,
                "abstract": abstract_name,
                "file_path": file_path,
                "devices": [],
                "status": "no_subscribers",
                "value": log_value,
            })
            return

        scene_name = self.cached_scene or DEFAULT_SCENE_FALLBACK
        # If the gateway has no cached scene yet (Scene Service unreachable
        # at boot, or no SceneChange has fired), we patch the firmware's
        # first-boot default scene name. If the device's active scene differs,
        # the patch lands on a dormant scene and the file change has no
        # audible effect — the WARNING here is the operator's only clue.
        if self.cached_scene is None:
            logger.warning("Abstract '%s': no cached scene — patching scene '%s' "
                           "(may not be the device's active scene)",
                           abstract_name, scene_name)

        # POST file_path to each device that doesn't already have it cached.
        # POSTs run in parallel — the device handles concurrent HTTP fine.
        post_tasks: list[asyncio.Task] = []
        post_targets: list[tuple[int, DeviceConnection]] = []
        post_was_cache_hit: list[bool] = []
        for conn_id in list(conn_ids):
            device = self.devices.get(conn_id)
            if device is None:
                continue
            cached = self.file_cache.get(device.device_id, self.abstract_track_index)
            if cached == file_path:
                logger.info("File change SKIPPED (cache hit): device=%s scene=%s track=%d file=%s",
                            device.device_id, scene_name,
                            self.abstract_track_index, file_path)
                post_targets.append((conn_id, device))
                post_tasks.append(asyncio.create_task(asyncio.sleep(0, result=True)))
                post_was_cache_hit.append(True)
                continue
            post_targets.append((conn_id, device))
            post_tasks.append(asyncio.create_task(
                self._post_file_path(device, scene_name, file_path)
            ))
            post_was_cache_hit.append(False)

        results = await asyncio.gather(*post_tasks, return_exceptions=False)

        # Per-device file_change events — separate from the trigger fire so
        # operators can see in the website log which scene we patched.
        for (conn_id, device), ok, hit in zip(post_targets, results, post_was_cache_hit):
            if hit:
                fc_status = "skipped_cache"
            elif ok:
                fc_status = "ok"
            else:
                fc_status = "post_failed"
            self._log_event({
                "ts": time.time(),
                "kind": "file_change",
                "device_id": device.device_id,
                "scene": scene_name,
                "track_index": self.abstract_track_index,
                "file_path": file_path,
                "abstract": abstract_name,
                "status": fc_status,
            })

        # Filter to devices whose POST succeeded (or whose cache was warm).
        deliverable: list[tuple[int, DeviceConnection]] = []
        for (conn_id, device), ok in zip(post_targets, results):
            if ok:
                deliverable.append((conn_id, device))

        if not deliverable:
            logger.warning("Abstract '%s': all file_path POSTs failed; dropping event",
                           abstract_name)
            self._log_event({
                "ts": time.time(),
                "kind": "fire",
                "upstream": upstream_name,
                "abstract": abstract_name,
                "file_path": file_path,
                "scene": scene_name,
                "devices": [d.device_id for _, d in post_targets],
                "status": "post_failed",
                "value": log_value,
            })
            return

        # Build the synthetic event under the abstract name.
        synthetic = dict(event)
        synthetic["name"] = abstract_name
        target_tsf_us, err = self._resolve_target_tsf(synthetic, len(deliverable))
        if err is not None:
            logger.warning("Dropping abstract '%s': %s", abstract_name, err)
            return
        if target_tsf_us is not None:
            synthetic["target_tsf_us"] = int(target_tsf_us)
        synthetic.pop("iso_time", None)
        synthetic.pop("delta_ms", None)
        out_line = json.dumps(synthetic)

        sent_ids: list[str] = []
        failed: list[int] = []
        for conn_id, device in deliverable:
            ok = await device.send_line(out_line)
            if ok:
                sent_ids.append(device.device_id)
                # Trigger send is logged separately from the file change so
                # the two events are easy to correlate (or notice when one
                # ran without the other).
                logger.info("Trigger send → device=%s trigger=%s%s",
                            device.device_id, abstract_name,
                            f" target_tsf_us={target_tsf_us}" if target_tsf_us else "")
            else:
                failed.append(conn_id)

        for conn_id in failed:
            self._remove_device(conn_id)

        self._log_event({
            "ts": time.time(),
            "kind": "fire",
            "upstream": upstream_name,
            "abstract": abstract_name,
            "file_path": file_path,
            "scene": scene_name,
            "devices": sent_ids,
            "status": "ok" if sent_ids else "send_failed",
            "value": log_value,
        })

    async def _post_file_path(self, device: DeviceConnection,
                              scene_name: str, file_path: str) -> bool:
        """POST /api/scenes patch to the device to set track <abstract_track_index>'s
        file_path. Returns True on success, False on any failure.

        NOTE: This patch causes the device firmware to fire a full
        mur_listener_resubscribe() (see main/http_server.c:262). The device
        will re-send its subscribe message immediately. That's harmless here
        because subscriptions are name-only and idempotent.
        """
        if not device.peer_ip:
            logger.warning("Cannot POST file_path to %s: no peer_ip", device.device_id)
            return False
        # Pre-log so an operator can see WHICH scene we're patching even if
        # the POST then fails or hangs. The scene name comes from the
        # cached_scene; if the device's active scene differs, the patch will
        # land on a dormant scene and have no audible effect.
        logger.info("File change → device=%s scene=%s track=%d file=%s",
                    device.device_id, scene_name, self.abstract_track_index, file_path)
        url = f"http://{device.peer_ip}/api/scenes"
        body = {
            scene_name: {
                "tracks": [
                    {"track": self.abstract_track_index, "file_path": file_path}
                ]
            }
        }
        session = await self._get_http_session()
        try:
            async with session.post(url, json=body,
                                    timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if 200 <= resp.status < 300:
                    self.file_cache.set(device.device_id, self.abstract_track_index, file_path)
                    logger.info("File change OK: device=%s scene=%s (HTTP %d)",
                                device.device_id, scene_name, resp.status)
                    return True
                text = await resp.text()
                logger.warning("File change FAILED: device=%s scene=%s HTTP %d: %.200s",
                               device.device_id, scene_name, resp.status, text)
                return False
        except Exception as e:
            logger.warning("File change EXCEPTION: device=%s scene=%s err=%s",
                           device.device_id, scene_name, e)
            return False

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    def _log_event(self, entry: dict) -> None:
        self.event_log.push(entry)

    # -------------------------------------------------------------------
    #  Downstream: Mur device connections
    # -------------------------------------------------------------------

    async def _handle_device_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        conn_id = id(writer)
        peer_ip = peer[0] if peer else None
        logger.info("Device connection from %s", peer)

        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "TCP_KEEPIDLE"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 5)
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
            if hasattr(socket, "TCP_KEEPCNT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)

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
            tsf_us = msg.get("tsf_us")
            if isinstance(tsf_us, (int, float)) and tsf_us > 0:
                self.tsf_map.update(device_id, time.time(), int(tsf_us))
                logger.info("TSF map: seeded from %s announce (tsf_us=%d)",
                            device_id, int(tsf_us))
            else:
                logger.info("TSF map: %s announced without tsf_us (firmware predates sync feature?)",
                            device_id)
            welcome = json.dumps({"type": "welcome", "gateway": "mur-abs-gateway", "version": VERSION})
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
                self._log_event({
                    "ts": time.time(),
                    "kind": "subscribe",
                    "device_id": device.device_id,
                    "peer_ip": device.peer_ip,
                    "triggers": list(triggers),
                })

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
            asyncio.ensure_future(self._answer_get_scene(conn_id))

        else:
            logger.warning("Unknown message type '%s' from device %s", msg_type, device.device_id)

    # -------------------------------------------------------------------
    #  Scene cache
    # -------------------------------------------------------------------

    async def _answer_get_scene(self, conn_id: int):
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
        if self._scene_refresh_lock is None:
            self._scene_refresh_lock = asyncio.Lock()
        async with self._scene_refresh_lock:
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
        device = self.devices.pop(conn_id, None)
        if not device:
            return
        for t in device.triggers:
            if t in self.subscriptions:
                self.subscriptions[t].discard(conn_id)
                if not self.subscriptions[t]:
                    del self.subscriptions[t]
        # Don't invalidate file_cache on disconnect — when the device
        # reconnects within the TTL we trust our last-set value (per spec).
        logger.info("Removed device %s (%s)", device.device_id, device.peer)

    # -------------------------------------------------------------------
    #  HTTP status endpoint (port 4001) — same shape as mur_gateway plus
    #  abstract-trigger summary
    # -------------------------------------------------------------------

    async def _handle_status(self, request: web.Request) -> web.Response:
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
            "abstract_triggers": list(sorted(self.abstract_registry.triggers.keys())),
            "abstract_track_index": self.abstract_track_index,
            "sync": {
                "fanout_delay_ms": self.fanout_delay_ms,
                "tsf_query_interval_s": self.tsf_query_interval_s,
                "tsf_query_devices_count": self.tsf_query_devices_count,
                "tsf_map": self.tsf_map.status(),
            },
        }
        return web.json_response(body)

    async def _fetch_upstream_triggers(self) -> tuple[Optional[list[dict]], Optional[str]]:
        """Fetch the upstream Trigger Server's trigger list with On/Off → OneShot
        relabeling applied. Returns (typed_list, error_text). On any failure
        returns (None, error_text)."""
        url = f"http://{self.trigger_host}:{self.trigger_port}/api/triggers"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        return None, f"Trigger Server returned HTTP {resp.status}"
                    data = await resp.json()
        except Exception as e:
            logger.warning("Failed to fetch triggers from %s: %s", url, e)
            return None, f"Cannot reach Trigger Server: {e}"

        triggers = data.get("triggers", [])
        typed: list[dict] = []
        for t in triggers:
            name = t.get("name")
            if not name:
                continue
            ttype = t.get("type", "")
            if ttype == "On/Off":
                typed.append({"name": name, "type": "OneShot"})
            else:
                entry = {"name": name, "type": ttype}
                if "range" in t:
                    entry["range"] = t["range"]
                typed.append(entry)
        return typed, None

    async def _handle_triggers(self, request: web.Request) -> web.Response:
        """GET /triggers (port 4001) — merged list shown to clients
        (e.g. mur-config-server's track-trigger dropdown).

        Upstream On/Off triggers are relabeled OneShot, then abstract trigger
        names are added (typed OneShot) so that devices can be configured to
        subscribe to abstract names directly. On name collision the abstract
        wins — the gateway's local concept supersedes upstream.
        """
        upstream, err = await self._fetch_upstream_triggers()
        if upstream is None:
            return web.json_response({"error": err}, status=502)

        by_name: dict[str, dict] = {t["name"]: t for t in upstream}
        for abstract_name in self.abstract_registry.triggers:
            by_name[abstract_name] = {"name": abstract_name, "type": "OneShot"}

        typed = sorted(by_name.values(), key=lambda x: x["name"])
        names = [t["name"] for t in typed]
        return web.json_response({"trigger_names": names, "triggers": typed})

    # -------------------------------------------------------------------
    #  UI / management API (port 5101) — NEW
    # -------------------------------------------------------------------

    async def _ui_index(self, request: web.Request) -> web.Response:
        index = Path(__file__).resolve().parent / "templates" / "index.html"
        try:
            with open(index, "r", encoding="utf-8") as f:
                html = f.read()
        except OSError as e:
            return web.Response(text=f"Template missing: {e}", status=500)
        return web.Response(text=html, content_type="text/html")

    async def _api_get_abstract(self, request: web.Request) -> web.Response:
        return web.json_response(self.abstract_registry.to_json_dict())

    async def _api_post_abstract(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception as e:
            return web.json_response({"error": f"bad JSON body: {e}"}, status=400)
        try:
            self.abstract_registry.save(body)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        # File set changed — drop the file_cache so any obsolete entries
        # don't suppress the next POST. Re-derived on next fire.
        self.file_cache.clear()
        return web.json_response({"ok": True, "abstract_triggers":
                                  list(sorted(self.abstract_registry.triggers.keys()))})

    async def _api_gateway_info(self, request: web.Request) -> web.Response:
        """GET /api/gateway-info — small summary surfaced in the UI header."""
        upstream_connected = (
            self.upstream_writer is not None
            and not self.upstream_writer.is_closing()
        )
        scene_age = (
            round(time.monotonic() - self.cached_scene_at, 1)
            if self.cached_scene_at > 0 else None
        )
        return web.json_response({
            "trigger_server": f"{self.trigger_host}:{self.trigger_port}",
            "trigger_server_connected": upstream_connected,
            "scene_service_url": self.scene_service_url,
            "cached_scene": self.cached_scene,
            "cached_scene_age_seconds": scene_age,
            "abstract_track_index": self.abstract_track_index,
            "device_count": len(self.devices),
        })

    async def _api_get_devices(self, request: web.Request) -> web.Response:
        out = []
        for conn_id, dev in self.devices.items():
            out.append({
                "id": dev.device_id,
                "peer_ip": dev.peer_ip,
                "subscriptions": sorted(dev.triggers),
                "connected_at": dev.connected_at,
                "uptime_seconds": round(time.time() - dev.connected_at, 1),
            })
        out.sort(key=lambda d: d["id"])
        return web.json_response({"devices": out})

    async def _api_device_files(self, request: web.Request) -> web.Response:
        device_id = request.match_info["device_id"]
        device = next((d for d in self.devices.values() if d.device_id == device_id), None)
        if device is None or not device.peer_ip:
            return web.json_response(
                {"error": f"device '{device_id}' not connected"},
                status=404,
            )
        url = f"http://{device.peer_ip}/api/files"
        try:
            session = await self._get_http_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return web.json_response(data)
                return web.json_response(
                    {"error": f"device returned HTTP {resp.status}"},
                    status=502,
                )
        except Exception as e:
            return web.json_response({"error": f"device unreachable: {e}"}, status=502)

    async def _api_upstream_triggers(self, request: web.Request) -> web.Response:
        """GET /api/upstream-triggers (port 5101) — UPSTREAM ONLY (no abstracts).
        Drives the abstract-gateway UI's "map an upstream trigger" dropdown,
        so abstract names must NOT appear here (you can't map an abstract
        to itself)."""
        upstream, err = await self._fetch_upstream_triggers()
        if upstream is None:
            return web.json_response({"error": err}, status=502)
        upstream.sort(key=lambda x: x["name"])
        names = [t["name"] for t in upstream]
        return web.json_response({"trigger_names": names, "triggers": upstream})

    async def _api_log(self, request: web.Request) -> web.Response:
        try:
            n = int(request.query.get("limit", "10"))
        except ValueError:
            n = 10
        return web.json_response({"events": self.event_log.recent(n)})

    async def _api_log_stream(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await resp.prepare(request)
        # Send recent history first so the client's UI populates immediately.
        for entry in self.event_log.recent(LOG_BUFFER_SIZE):
            await resp.write(f"data: {json.dumps(entry)}\n\n".encode("utf-8"))

        q = self.event_log.subscribe()
        last_ping = time.monotonic()
        try:
            # 1s polling so this loop exits within a second of self._running
            # going False during shutdown — otherwise aiohttp's runner cleanup
            # would have to wait the full shutdown_timeout for us to notice.
            while self._running:
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=1.0)
                    await resp.write(f"data: {json.dumps(entry)}\n\n".encode("utf-8"))
                except asyncio.TimeoutError:
                    if time.monotonic() - last_ping > 15.0:
                        # Heartbeat keeps proxies / browsers from idling out.
                        await resp.write(b": ping\n\n")
                        last_ping = time.monotonic()
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.event_log.unsubscribe(q)
        return resp

    async def _api_static(self, request: web.Request) -> web.Response:
        # Simple static file shim under /static/. aiohttp.web.static
        # would also work but adds another route boilerplate.
        rel = request.match_info["filename"]
        if ".." in rel or rel.startswith("/"):
            return web.Response(status=400, text="bad path")
        base = Path(__file__).resolve().parent / "static"
        target = base / rel
        try:
            target = target.resolve()
            target.relative_to(base.resolve())
        except (OSError, ValueError):
            return web.Response(status=400, text="bad path")
        if not target.is_file():
            return web.Response(status=404, text="not found")
        ctype = "application/octet-stream"
        if target.suffix == ".css":
            ctype = "text/css"
        elif target.suffix == ".js":
            ctype = "application/javascript"
        elif target.suffix == ".html":
            ctype = "text/html"
        return web.FileResponse(target, headers={"Content-Type": ctype})

    # -------------------------------------------------------------------
    #  Utilities
    # -------------------------------------------------------------------

    def _get_local_ip(self) -> str:
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
        # Load abstract trigger registry (file-backed, source of truth).
        self.abstract_registry.load()

        # Upstream TCP server (Trigger Server connects here).
        upstream_server = await asyncio.start_server(
            self._handle_upstream_connection,
            "0.0.0.0",
            self.upstream_port,
        )
        logger.info("Upstream listener on port %d (for Trigger Server)", self.upstream_port)

        # Device TCP server.
        device_server = await asyncio.start_server(
            self._handle_device_connection,
            "0.0.0.0",
            self.device_port,
        )
        logger.info("Device listener on port %d (for Mur devices)", self.device_port)

        # Status HTTP server (4001) — preserves /status and /triggers.
        # shutdown_timeout overrides aiohttp's 60s default; we only need a
        # second or two to drain in-flight requests on Ctrl-C.
        status_app = web.Application()
        status_app.router.add_get("/status", self._handle_status)
        status_app.router.add_get("/triggers", self._handle_triggers)
        status_runner = web.AppRunner(status_app, shutdown_timeout=2.0)
        await status_runner.setup()
        status_site = web.TCPSite(status_runner, "0.0.0.0", self.status_port)
        await status_site.start()
        logger.info("Status HTTP on port %d (GET /status, GET /triggers)", self.status_port)

        # UI HTTP server (5101) — abstract trigger management + live log.
        # Same shutdown_timeout note as above; otherwise the open SSE log
        # stream from any browser tab keeps cleanup() blocked for 60s.
        ui_app = web.Application()
        ui_app.router.add_get("/", self._ui_index)
        ui_app.router.add_get("/api/gateway-info", self._api_gateway_info)
        ui_app.router.add_get("/api/abstract-triggers", self._api_get_abstract)
        ui_app.router.add_post("/api/abstract-triggers", self._api_post_abstract)
        ui_app.router.add_get("/api/devices", self._api_get_devices)
        ui_app.router.add_get("/api/device/{device_id}/files", self._api_device_files)
        ui_app.router.add_get("/api/upstream-triggers", self._api_upstream_triggers)
        ui_app.router.add_get("/api/log", self._api_log)
        ui_app.router.add_get("/api/log/stream", self._api_log_stream)
        ui_app.router.add_get("/static/{filename:.*}", self._api_static)
        ui_runner = web.AppRunner(ui_app, shutdown_timeout=2.0)
        await ui_runner.setup()
        ui_site = web.TCPSite(ui_runner, "0.0.0.0", self.ui_port)
        await ui_site.start()
        logger.info("UI on port %d (http://localhost:%d/)", self.ui_port, self.ui_port)

        # Background loops.
        self._register_task = asyncio.create_task(self._registration_loop())
        self._prime_task = asyncio.create_task(self._prime_scene_cache())
        self._tsf_query_task = asyncio.create_task(self._tsf_query_loop())

        logger.info("Mur Abs Gateway ready.")
        logger.info("  Trigger Server:    %s:%d", self.trigger_host, self.trigger_port)
        logger.info("  Upstream port:     %d", self.upstream_port)
        logger.info("  Device port:       %d", self.device_port)
        logger.info("  Status port:       %d", self.status_port)
        logger.info("  UI port:           %d", self.ui_port)
        logger.info("  Scene Service:     %s (cache TTL %.0fs)",
                    self.scene_service_url, self.scene_cache_ttl)
        logger.info("  Abstract triggers: %d (track index %d, file cache TTL %.0fs)",
                    len(self.abstract_registry.triggers),
                    self.abstract_track_index, self.file_cache_ttl_s)
        logger.info("  Sync:              fanout %d ms, tsf_query every %.0fs (sample %d)",
                    self.fanout_delay_ms, self.tsf_query_interval_s, self.tsf_query_devices_count)

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
                pass

        try:
            await stop_event.wait()
        except KeyboardInterrupt:
            pass

        logger.info("Shutting down...")
        # Flip _running so long-poll loops (SSE, _api_log_stream) wake up
        # quickly and exit. Already set by the signal handler in the SIGINT
        # path, but redundancy doesn't hurt.
        self._running = False

        # Cancel the periodic background tasks first.
        for t in (self._register_task, self._prime_task, self._tsf_query_task):
            if t:
                t.cancel()

        # Stop accepting new connections.
        upstream_server.close()
        device_server.close()

        # Close the existing upstream connection so its read loop exits and
        # we stop processing new trigger events mid-shutdown.
        if self.upstream_writer and not self.upstream_writer.is_closing():
            try:
                self.upstream_writer.close()
            except Exception:
                pass

        # Close all device sockets so their per-connection handler tasks
        # unwind in parallel with the runner cleanup below. This is what
        # produces the "Removed device ..." log lines — same as mur_gateway.
        for conn_id, dev in list(self.devices.items()):
            try:
                dev.writer.close()
            except Exception:
                pass

        # Cleanup HTTP runners — bounded by shutdown_timeout=2.0 above so
        # an idle SSE stream can't pin us for 60 seconds.
        await status_runner.cleanup()
        await ui_runner.cleanup()

        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

        self.devices.clear()
        self.subscriptions.clear()


# ---------------------------------------------------------------------------
#  CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Mur Abs Gateway — abstract-trigger gateway between Haven Trigger Server and Murmura devices"
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
    parser.add_argument("--name", default="mur-abs-gateway",
                        help="Gateway name used in Trigger Server registration (default: mur-abs-gateway)")
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
    gateway = MurAbsGateway(args, sync_cfg)

    if sys.platform == "win32":
        try:
            asyncio.run(gateway.run())
        except KeyboardInterrupt:
            logger.info("Interrupted")
    else:
        asyncio.run(gateway.run())


if __name__ == "__main__":
    main()
