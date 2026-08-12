#!/usr/bin/env python3
"""
Mur Scene Server — fleet-wide active-scene authority for Murmura.

TWO-LAYER MODEL (read this before changing anything):

    mur-scene-server decides WHICH scene is active fleet-wide.
    Each MUR decides WHAT THAT SCENE SOUNDS LIKE.

This service stores scene *names* and which one is active. It never stores or
touches per-track content — files, volumes, modes and triggers live on each
device in /sdcard/scenes.json and are edited through mur-config-server.

It is the missing piece of a contract the rest of the system already speaks:

  - mur-gateway primes its scene cache from GET /api/scenes/active at startup
    (30 x 2 s retry) and lazily refreshes it on TTL expiry.
    See mur_gateway.py DEFAULT_SCENE_SERVICE_URL / _prime_scene_cache().
  - SYNC_DESIGN.md: "never fire SceneChange from anywhere except the scene
    server." This process is the only sanctioned source of that trigger.

Endpoints:
  GET    /                        Web UI
  GET    /api/scenes/active       {"active_scene": str|null}   <- FROZEN CONTRACT
  POST   /api/scenes/active       Set active scene, fires SceneChange
  GET    /api/scenes              List scenes + device-limit info
  POST   /api/scenes              Create a scene
  DELETE /api/scenes/<name>       Delete a scene (not the active one)
  GET    /api/schedules           List schedules
  POST   /api/schedules           Create a schedule
  PUT    /api/schedules/<id>      Update a schedule
  DELETE /api/schedules/<id>      Delete a schedule
  GET    /health                  Health + integration status

Usage:
    python mur_scene_server.py
    python mur_scene_server.py --port 5013 --ephemeral

Ported from the Haven Scene Service (haven/Triggers/scene_service.py). See
README.md "Differences from the Haven original" for the defects fixed on the
way across.
"""

import argparse
import json
import logging
import os
import re
import socket
import sys
import tempfile
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import requests
from flask import Flask, jsonify, render_template, request

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DEFAULT_PORT = 5003
PORT_ENV_VAR = "MUR_SCENE_SERVER_PORT"

SCENE_TRIGGER_NAME = "SceneChange"
DEVICE_NAME = "SceneService"

SERVICE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SERVICE_DIR / "config.json"
# Runtime state lives in a subdirectory named after the package, matching
# mur-config-server's mur_config_server/device_map.json convention.
STATE_DIR = SERVICE_DIR / "mur_scene_server"
SCENES_FILE = STATE_DIR / "scenes.json"

# Device limits, enforced here so an operator can never create a scene name that
# no MUR can store. Source of truth: main/scene_manager.h.
#   MAX_SCENE_NAME_LEN 32  -> 31 usable chars (the 32nd is the NUL)
#   MAX_SCENES 16
# Character rule per HTTP_API.md: alphanumeric plus '-' and '_'.
MAX_SCENE_NAME_LEN = 31
SCENE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,31}$")
MAX_DEVICE_SCENES = 16

SCENE_NAME_RULE_MSG = (
    "Scene name must be 1-31 characters, letters, digits, hyphen and "
    "underscore only (device limit -- see HTTP_API.md)"
)

# Manager methods return (ok, reason, payload). Routes map reason -> HTTP status
# rather than sniffing substrings out of the message, which is how the Haven
# original decided between 400 and 404.
REASON_STATUS = {
    "not_found": 404,
    "active": 400,
    "exists": 400,
    "invalid_name": 400,
    "invalid_scene": 400,
    "invalid_time": 400,
    "invalid_repeat": 400,
}

CONFIG_DEFAULTS = {
    # Where SceneChange events are POSTed. Null/empty disables the push.
    "trigger_server_url": "http://127.0.0.1:5002",
    # "auto" -> probe once, disable permanently on 404/405.
    # true   -> probe and keep retrying connection failures.
    # false  -> never attempt registration.
    "register_with_trigger_server": "auto",
    "scheduler_interval_s": 30,
    # Ephemeral mode only.
    "seed_scenes": ["night", "day", "show"],
    "seed_active_scene": "day",
    # Related Services links in the UI. Purely cosmetic; never polled by us.
    "config_server_url": "http://127.0.0.1:8765",
    "gateway_status_url": "http://127.0.0.1:4001/status",
    "conductor_status_url": "http://127.0.0.1:4002/status",
    "trigger_server_ui_url": "http://127.0.0.1:5002/api/triggers",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("mur-scene-server")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict:
    """Merge config.json over the built-in defaults.

    Follows mur_gateway.load_sync_config(): per-field validation, warn and fall
    back on a bad type, a missing file is not an error. Operational knobs live
    here so an on-site install can be edited without touching Python.
    """
    cfg = dict(CONFIG_DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
    except FileNotFoundError:
        logger.info("No %s found -- using built-in defaults", path.name)
        return cfg
    except json.JSONDecodeError as e:
        logger.warning("Bad JSON in %s (%s) -- using built-in defaults", path, e)
        return cfg

    if not isinstance(user_cfg, dict):
        logger.warning("%s is not a JSON object -- using built-in defaults", path)
        return cfg

    for key in ("trigger_server_url", "config_server_url", "gateway_status_url",
                "conductor_status_url", "trigger_server_ui_url"):
        if key not in user_cfg:
            continue
        val = user_cfg[key]
        if val is None or val == "":
            cfg[key] = None
        elif isinstance(val, str):
            cfg[key] = val.strip().rstrip("/") if key == "trigger_server_url" else val.strip()
        else:
            logger.warning("%s must be a string or null; keeping %r", key, cfg[key])

    if "register_with_trigger_server" in user_cfg:
        val = user_cfg["register_with_trigger_server"]
        if val in (True, False, "auto"):
            cfg["register_with_trigger_server"] = val
        else:
            logger.warning(
                "register_with_trigger_server must be true, false or \"auto\"; keeping %r",
                cfg["register_with_trigger_server"])

    if "scheduler_interval_s" in user_cfg:
        val = user_cfg["scheduler_interval_s"]
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val >= 1:
            cfg["scheduler_interval_s"] = float(val)
        else:
            logger.warning("scheduler_interval_s must be a number >= 1; keeping %r",
                           cfg["scheduler_interval_s"])

    if "seed_scenes" in user_cfg:
        val = user_cfg["seed_scenes"]
        if isinstance(val, list) and val and all(
                isinstance(s, str) and s.strip() for s in val):
            cfg["seed_scenes"] = [s.strip() for s in val]
        else:
            logger.warning("seed_scenes must be a non-empty list of strings; keeping %r",
                           cfg["seed_scenes"])

    if "seed_active_scene" in user_cfg:
        val = user_cfg["seed_active_scene"]
        if val is None:
            cfg["seed_active_scene"] = None
        elif isinstance(val, str) and val.strip() in cfg["seed_scenes"]:
            cfg["seed_active_scene"] = val.strip()
        else:
            logger.warning("seed_active_scene %r is not in seed_scenes; keeping %r",
                           val, cfg["seed_active_scene"])

    logger.info("Loaded config from %s", path)
    return cfg


def validate_scene_name(name) -> Optional[str]:
    """Return None if the name is storable on a MUR, else the reason it isn't."""
    if not isinstance(name, str):
        return SCENE_NAME_RULE_MSG
    if not SCENE_NAME_RE.match(name.strip()):
        return SCENE_NAME_RULE_MSG
    return None


# --------------------------------------------------------------------------
# Scene manager
# --------------------------------------------------------------------------

class SceneManager:
    """Scene names, the active scene, and scheduled activations.

    All check-then-modify-then-save sequences run under a single lock. Every
    mutator returns (ok, reason, payload) so the HTTP layer can pick a status
    code from a reason string instead of parsing the human-readable message.
    """

    def __init__(self, config: dict, ephemeral: bool = False,
                 scenes_file: Path = SCENES_FILE):
        self.config = config
        self.ephemeral = ephemeral
        self.filename = Path(scenes_file)
        self.scenes: set = set()
        self.active_scene: Optional[str] = None
        self.schedules: list = []
        self._lock = threading.RLock()
        self._stop = threading.Event()

        # Trigger-server integration state, surfaced by /health.
        self._registration_supported: Optional[bool] = None  # None = not yet probed
        self._registered = False
        self._register_event = threading.Event()
        self._last_push_ok: Optional[bool] = None

        self.load_scenes()
        self._start_scheduler()
        self._start_registrar()

    # -- persistence -------------------------------------------------------

    def load_scenes(self):
        if self.ephemeral:
            self.scenes = set(self.config["seed_scenes"])
            self.active_scene = self.config["seed_active_scene"]
            self.schedules = []
            logger.info("Ephemeral mode: seeded %d scenes, active=%r (nothing is persisted)",
                        len(self.scenes), self.active_scene)
            return

        self.filename.parent.mkdir(parents=True, exist_ok=True)

        if not self.filename.exists():
            logger.info("No %s yet -- starting with an empty scene list", self.filename)
            self.save_scenes()
            return

        try:
            with open(self.filename, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.scenes = set(data.get("scenes", []))
            self.active_scene = data.get("active_scene")
            self.schedules = data.get("schedules", [])
            logger.info("Loaded %d scenes, %d schedules from %s",
                        len(self.scenes), len(self.schedules), self.filename)
            if self.active_scene:
                logger.info("Active scene: %s", self.active_scene)
        except Exception as e:
            # Do not clobber a file we failed to parse -- an operator can still
            # recover it by hand. Start empty in memory and refuse to save until
            # something is explicitly changed.
            logger.error("Error loading %s: %s -- starting empty (file left intact)",
                         self.filename, e)
            self.scenes = set()
            self.active_scene = None
            self.schedules = []

    def save_scenes(self):
        """Atomically persist state. Caller must hold self._lock.

        Writes a temp file in the same directory then os.replace()-swaps it in,
        so a crash mid-write never leaves scenes.json corrupt or truncated.
        """
        if self.ephemeral:
            return
        tmpname = None
        try:
            data = {
                # sorted(), not list(): a set serializes in arbitrary order,
                # which makes the file churn on every write and useless to diff.
                "scenes": sorted(self.scenes),
                "active_scene": self.active_scene,
                "schedules": self.schedules,
                "last_updated": datetime.now().isoformat(),
            }
            self.filename.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                    "w", dir=str(self.filename.parent), suffix=".tmp",
                    delete=False, encoding="utf-8") as f:
                tmpname = f.name
                json.dump(data, f, indent=2)
            os.replace(tmpname, self.filename)
        except Exception as e:
            logger.error("Error saving %s: %s", self.filename, e)
            if tmpname:
                try:
                    os.unlink(tmpname)
                except OSError:
                    pass

    # -- scene CRUD --------------------------------------------------------

    def create_scene(self, name):
        problem = validate_scene_name(name)
        if problem:
            return False, "invalid_name", problem

        name = name.strip()
        with self._lock:
            if name in self.scenes:
                return False, "exists", f"Scene '{name}' already exists"
            self.scenes.add(name)
            self.save_scenes()
            count = len(self.scenes)

        logger.info("Created scene: %s", name)
        self._request_registration()

        warning = None
        if count > MAX_DEVICE_SCENES:
            warning = (f"{count} scenes exceeds the {MAX_DEVICE_SCENES}-scene per-device "
                       f"limit (MAX_SCENES); no MUR can hold them all")
            logger.warning(warning)
        return True, None, {"message": f"Scene '{name}' created",
                            "scene": name, "warning": warning}

    def delete_scene(self, name):
        with self._lock:
            if name not in self.scenes:
                return False, "not_found", f"Scene '{name}' does not exist"
            if self.active_scene == name:
                return False, "active", f"Cannot delete the currently active scene '{name}'"
            self.scenes.discard(name)
            # Drop any schedules that pointed at it.
            self.schedules = [s for s in self.schedules if s.get("scene") != name]
            self.save_scenes()

        logger.info("Deleted scene: %s", name)
        self._request_registration()
        return True, None, f"Scene '{name}' deleted"

    def get_scenes(self):
        # Hold the lock: sorted() iterates the set, and a concurrent create or
        # delete would raise "Set changed size during iteration".
        with self._lock:
            return sorted(self.scenes)

    def get_active_scene(self):
        with self._lock:
            return self.active_scene

    def set_active_scene(self, name):
        """Set the active scene. None clears it. Fires SceneChange on success."""
        with self._lock:
            if name is None:
                self.active_scene = None
                self.save_scenes()
                logger.info("Cleared active scene")
            else:
                if name not in self.scenes:
                    return False, "not_found", f"Scene '{name}' does not exist"
                self.active_scene = name
                self.save_scenes()
                logger.info("Set active scene: %s", name)

        self._push_scene_change_async(name)
        msg = "Active scene cleared" if name is None else f"Active scene set to '{name}'"
        return True, None, msg

    # -- schedules ---------------------------------------------------------

    @staticmethod
    def _normalize_time(time_str):
        try:
            hour, minute = map(int, str(time_str).strip().split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError()
            return f"{hour:02d}:{minute:02d}"
        except Exception:
            return None

    def create_schedule(self, scene_name, time_str, repeat):
        normalized = self._normalize_time(time_str)
        if normalized is None:
            return False, "invalid_time", "time must be in HH:MM format (24-hour)"
        if repeat not in ("daily", "once"):
            return False, "invalid_repeat", "repeat must be 'daily' or 'once'"

        with self._lock:
            # Validate inside the lock: the Haven original checked scene
            # existence outside it, so a concurrent delete could leave a
            # schedule pointing at a scene that no longer exists.
            if scene_name not in self.scenes:
                return False, "invalid_scene", f"Scene '{scene_name}' does not exist"
            schedule = {
                "id": str(uuid.uuid4()),
                "scene": scene_name,
                "time": normalized,
                "repeat": repeat,
                "created": datetime.now().isoformat(),
                "last_fired": None,
            }
            self.schedules.append(schedule)
            self.save_scenes()

        logger.info("Created schedule %s: %s @ %s (%s)",
                    schedule["id"], scene_name, normalized, repeat)
        return True, None, dict(schedule)

    def update_schedule(self, schedule_id, scene_name, time_str, repeat):
        normalized = self._normalize_time(time_str)
        if normalized is None:
            return False, "invalid_time", "time must be in HH:MM format (24-hour)"
        if repeat not in ("daily", "once"):
            return False, "invalid_repeat", "repeat must be 'daily' or 'once'"

        with self._lock:
            if scene_name not in self.scenes:
                return False, "invalid_scene", f"Scene '{scene_name}' does not exist"
            for s in self.schedules:
                if s["id"] == schedule_id:
                    s["scene"] = scene_name
                    s["time"] = normalized
                    s["repeat"] = repeat
                    s["last_fired"] = None   # reset so it can fire at the new time
                    self.save_scenes()
                    logger.info("Updated schedule %s: %s @ %s (%s)",
                                schedule_id, scene_name, normalized, repeat)
                    return True, None, dict(s)
            return False, "not_found", f"Schedule '{schedule_id}' not found"

    def delete_schedule(self, schedule_id):
        with self._lock:
            before = len(self.schedules)
            self.schedules = [s for s in self.schedules if s["id"] != schedule_id]
            if len(self.schedules) == before:
                return False, "not_found", f"Schedule '{schedule_id}' not found"
            self.save_scenes()
        logger.info("Deleted schedule %s", schedule_id)
        return True, None, f"Schedule '{schedule_id}' deleted"

    def get_schedules(self):
        with self._lock:
            return [dict(s) for s in self.schedules]

    # -- background scheduler ---------------------------------------------

    def _start_scheduler(self):
        t = threading.Thread(target=self._scheduler_loop, daemon=True,
                             name="scene-scheduler")
        t.start()
        logger.info("Scene scheduler started (interval %ss)",
                    self.config["scheduler_interval_s"])

    def _scheduler_loop(self):
        interval = self.config["scheduler_interval_s"]
        while not self._stop.is_set():
            try:
                self._check_schedules()
            except Exception:
                logger.exception("Scheduler tick failed")
            self._stop.wait(interval)

    def _check_schedules(self):
        now_str = datetime.now().strftime("%H:%M")
        today_str = date.today().isoformat()
        fired_ids = []
        fired_scene = None

        with self._lock:
            for schedule in self.schedules:
                if schedule["time"] != now_str:
                    continue
                # Don't fire twice within the same minute.
                last_fired = schedule.get("last_fired") or ""
                if last_fired.startswith(today_str):
                    continue

                scene = schedule["scene"]
                if scene in self.scenes:
                    self.active_scene = scene
                    fired_scene = scene
                    schedule["last_fired"] = datetime.now().isoformat()
                    logger.info("Scheduler: activated scene '%s' (schedule %s)",
                                scene, schedule["id"])
                else:
                    logger.warning("Scheduler: scene '%s' no longer exists, skipping", scene)

                if schedule["repeat"] == "once":
                    fired_ids.append(schedule["id"])

            if fired_ids:
                self.schedules = [s for s in self.schedules if s["id"] not in fired_ids]

            # Save only when something actually changed. The Haven original
            # tested `self.active_scene or fired_ids`, which rewrote the file
            # every tick for as long as any scene was active.
            if fired_scene or fired_ids:
                self.save_scenes()

        if fired_scene:
            self._push_scene_change_async(fired_scene)

    # -- trigger server integration ---------------------------------------

    def _push_scene_change_async(self, scene_name):
        threading.Thread(target=self._push_scene_change, args=(scene_name,),
                         daemon=True, name="scene-push").start()

    def _push_scene_change(self, scene_name):
        """Best-effort SceneChange push. Never blocks or fails the caller.

        Per SYNC_DESIGN.md this process is the only sanctioned source of the
        SceneChange trigger. The push is the fast path; mur-gateway's periodic
        pull from /api/scenes/active is the backstop when it fails.
        """
        base = self.config["trigger_server_url"]
        if not base:
            return
        url = f"{base}/api/trigger-event"
        payload = {"name": SCENE_TRIGGER_NAME,
                   "value": scene_name if scene_name else ""}
        try:
            resp = requests.post(url, json=payload, timeout=3)
            if resp.ok:
                self._last_push_ok = True
                logger.info("SceneChange dispatched -> %s (scene=%s)", url, scene_name)
            else:
                self._last_push_ok = False
                logger.warning("Trigger server returned %d for SceneChange: %s",
                               resp.status_code, resp.text[:200])
        except requests.RequestException as e:
            self._last_push_ok = False
            logger.warning("Could not reach trigger server for SceneChange: %s", e)

    def _start_registrar(self):
        setting = self.config["register_with_trigger_server"]
        if setting is False or not self.config["trigger_server_url"]:
            self._registration_supported = False
            logger.info("Trigger-server registration disabled by config")
            return
        threading.Thread(target=self._registrar_loop, daemon=True,
                         name="scene-registrar").start()

    def _registrar_loop(self):
        """One long-lived thread owns registration.

        The Haven original spawned a fresh 30-attempt thread on every scene
        create and delete, so a burst of edits produced a pile of overlapping
        retry loops. Here, startup retries once and later changes just wake this
        thread for a single attempt.

        No Murmura trigger server implements /api/register-device --
        mock-trigger-server and mur-conductor both expose /api/register instead.
        So a 404/405 is the expected answer, not an error: log it once, mark the
        capability absent, and never ask again.
        """
        if not self._attempt_registration(retry=True):
            return
        while not self._stop.is_set():
            self._register_event.wait()
            if self._stop.is_set():
                return
            self._register_event.clear()
            # Coalesce a burst of create/delete calls into one registration.
            self._stop.wait(0.5)
            self._register_event.clear()
            self._attempt_registration(retry=False)

    def _attempt_registration(self, retry: bool) -> bool:
        """Try to register. Returns False when the registrar should give up."""
        max_attempts = 30 if retry else 1
        retry_delay = 2

        for attempt in range(1, max_attempts + 1):
            # get_scenes() takes the lock, so call it outside any held lock.
            scenes = self.get_scenes()
            url = f"{self.config['trigger_server_url']}/api/register-device"
            payload = {
                "name": DEVICE_NAME,
                "ip": "localhost",
                "triggers": [{
                    "name": SCENE_TRIGGER_NAME,
                    "type": "Discrete",
                    "range": {"values": scenes},
                    "description": ("Active scene broadcast by mur-scene-server "
                                    "on every scene change"),
                }],
            }
            try:
                resp = requests.post(url, json=payload, timeout=3)
                if resp.ok:
                    self._registration_supported = True
                    self._registered = True
                    logger.info("Registered '%s' with trigger server (device=%s, scenes=%s)",
                                SCENE_TRIGGER_NAME, DEVICE_NAME, scenes)
                    return True
                if resp.status_code in (404, 405):
                    self._registration_supported = False
                    logger.info(
                        "Trigger server at %s does not implement /api/register-device "
                        "(HTTP %d). Scene list will not be advertised upstream; "
                        "mur-config-server reads it directly from GET /api/scenes "
                        "instead. This is expected with mock-trigger-server and "
                        "mur-conductor.",
                        self.config["trigger_server_url"], resp.status_code)
                    return False
                logger.warning("Trigger server registration failed (attempt %d/%d): %d %s",
                               attempt, max_attempts, resp.status_code, resp.text[:200])
            except requests.RequestException as e:
                logger.warning("Could not reach trigger server for registration "
                               "(attempt %d/%d): %s", attempt, max_attempts, e)

            if attempt < max_attempts:
                if self._stop.wait(retry_delay):
                    return False

        if retry:
            logger.warning("Gave up registering with the trigger server after %d attempts. "
                           "SceneChange push will still be attempted on every change.",
                           max_attempts)
        return retry  # keep the thread alive after a startup give-up, exit otherwise

    def _request_registration(self):
        if self._registration_supported is not False:
            self._register_event.set()

    # -- status ------------------------------------------------------------

    def health(self):
        scenes = self.get_scenes()
        return {
            "status": "healthy",
            "service": "mur-scene-server",
            "scenes_count": len(scenes),
            "active_scene": self.get_active_scene(),
            "schedules_count": len(self.get_schedules()),
            "over_device_limit": len(scenes) > MAX_DEVICE_SCENES,
            "max_device_scenes": MAX_DEVICE_SCENES,
            "ephemeral": self.ephemeral,
            "trigger_server_url": self.config["trigger_server_url"],
            "trigger_server_registered": self._registered,
            "registration_supported": self._registration_supported,
            "last_push_ok": self._last_push_ok,
            "scenes_file": None if self.ephemeral else str(self.filename),
        }


# --------------------------------------------------------------------------
# Flask app
# --------------------------------------------------------------------------

# Plain Flask(__name__): templates/ and static/ only. The Haven original used
# Flask(__name__, static_folder='.', static_url_path='') which served its entire
# working directory over HTTP -- scenes.json and all.
app = Flask(__name__)

config: dict = {}
scene_manager: Optional[SceneManager] = None


def _fail(reason, message):
    return jsonify({"error": message}), REASON_STATUS.get(reason, 400)


@app.route("/")
def index():
    return render_template(
        "index.html",
        config_server_url=config.get("config_server_url") or "",
        gateway_status_url=config.get("gateway_status_url") or "",
        conductor_status_url=config.get("conductor_status_url") or "",
        trigger_server_ui_url=config.get("trigger_server_ui_url") or "",
        max_scene_name_len=MAX_SCENE_NAME_LEN,
        max_device_scenes=MAX_DEVICE_SCENES,
    )


# -- scenes ----------------------------------------------------------------

@app.route("/api/scenes/active", methods=["GET"])
def get_active_scene():
    """FROZEN CONTRACT -- mur_gateway._refresh_scene_cache() depends on this
    exact shape. Do not add required fields or change the key."""
    return jsonify({"active_scene": scene_manager.get_active_scene()}), 200


@app.route("/api/scenes/active", methods=["POST"])
def set_active_scene():
    """Set the active scene.

    Requires a JSON object carrying a 'name' key. An explicit {"name": null}
    clears the active scene; a missing key or non-object body is a 400.

    The Haven original (and mock-scene-server) treated any unparseable body as
    "clear the active scene" and fired SceneChange with an empty value, so a
    typo'd curl silently dropped the whole fleet out of its scene.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or "name" not in data:
        return jsonify({
            "error": "JSON object with a 'name' key required. "
                     "Use {\"name\": null} to clear the active scene."
        }), 400

    name = data["name"]
    if name is not None:
        if not isinstance(name, str) or not name.strip():
            return jsonify({"error": "name must be a non-empty string or null"}), 400
        name = name.strip()

    ok, reason, message = scene_manager.set_active_scene(name)
    if not ok:
        return _fail(reason, message)
    return jsonify({"message": message, "active_scene": name}), 200


@app.route("/api/scenes", methods=["GET"])
def get_scenes():
    scenes = scene_manager.get_scenes()
    return jsonify({
        "scenes": scenes,
        "active_scene": scene_manager.get_active_scene(),
        "count": len(scenes),
        "max_device_scenes": MAX_DEVICE_SCENES,
        "over_device_limit": len(scenes) > MAX_DEVICE_SCENES,
    }), 200


@app.route("/api/scenes", methods=["POST"])
def create_scene():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or "name" not in data:
        return jsonify({"error": "Missing required field: name"}), 400

    ok, reason, result = scene_manager.create_scene(data["name"])
    if not ok:
        return _fail(reason, result)

    body = {"message": result["message"], "scene": result["scene"]}
    if result["warning"]:
        body["warning"] = result["warning"]
    return jsonify(body), 201


@app.route("/api/scenes/<name>", methods=["DELETE"])
def delete_scene(name):
    ok, reason, message = scene_manager.delete_scene(name)
    if not ok:
        return _fail(reason, message)
    return jsonify({"message": message}), 200


# -- schedules -------------------------------------------------------------

@app.route("/api/schedules", methods=["GET"])
def get_schedules():
    return jsonify({"schedules": scene_manager.get_schedules()}), 200


def _schedule_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({"error": "JSON body required"}), 400)
    for field in ("scene", "time", "repeat"):
        if field not in data:
            return None, (jsonify({"error": f"Missing required field: {field}"}), 400)
    return data, None


@app.route("/api/schedules", methods=["POST"])
def create_schedule():
    data, err = _schedule_body()
    if err:
        return err
    ok, reason, result = scene_manager.create_schedule(
        data["scene"], data["time"], data["repeat"])
    if not ok:
        return _fail(reason, result)
    return jsonify({"message": "Schedule created", "schedule": result}), 201


@app.route("/api/schedules/<schedule_id>", methods=["PUT"])
def update_schedule(schedule_id):
    data, err = _schedule_body()
    if err:
        return err
    ok, reason, result = scene_manager.update_schedule(
        schedule_id, data["scene"], data["time"], data["repeat"])
    if not ok:
        return _fail(reason, result)
    return jsonify({"message": "Schedule updated", "schedule": result}), 200


@app.route("/api/schedules/<schedule_id>", methods=["DELETE"])
def delete_schedule(schedule_id):
    ok, reason, message = scene_manager.delete_schedule(schedule_id)
    if not ok:
        return _fail(reason, message)
    return jsonify({"message": message}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify(scene_manager.health()), 200


# --------------------------------------------------------------------------
# Interactive CLI (salvaged from mock-scene-server)
# --------------------------------------------------------------------------

CLI_HELP = """
  Commands:
    list                  Show all scenes + active
    create <name>         Create a scene
    delete <name>         Delete a scene
    activate <name>       Set the active scene (fires SceneChange)
    clear                 Clear the active scene
    schedules             List schedules
    health                Show health JSON
    help                  Show this help
    quit                  Exit
"""


def cli_loop():
    time.sleep(1)
    print("\n  Mur Scene Server ready. Type 'help' for commands.\n")
    while True:
        try:
            line = input("  > ").strip()
        except EOFError:
            # stdin closed (piped, redirected, or launched detached). The HTTP
            # server is the point of this process -- drop the prompt and keep
            # serving. mock-scene-server called os._exit(0) here, so running it
            # in the background killed the whole service.
            logger.info("stdin closed -- interactive CLI disabled, continuing to serve HTTP")
            return
        except KeyboardInterrupt:
            print("\n  Shutting down...")
            os._exit(0)
        if not line:
            continue
        parts = line.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "help":
            print(CLI_HELP)
        elif cmd in ("quit", "exit"):
            print("  Shutting down...")
            os._exit(0)
        elif cmd == "list":
            active = scene_manager.get_active_scene()
            print(f"    active: {active!r}")
            for s in scene_manager.get_scenes():
                print(f"    {' *' if s == active else '  '} {s}")
        elif cmd == "create" and arg:
            ok, _reason, result = scene_manager.create_scene(arg)
            print(f"    {result['message'] if ok else result}")
            if ok and result.get("warning"):
                print(f"    WARNING: {result['warning']}")
        elif cmd == "delete" and arg:
            _ok, _reason, msg = scene_manager.delete_scene(arg)
            print(f"    {msg}")
        elif cmd == "activate" and arg:
            _ok, _reason, msg = scene_manager.set_active_scene(arg)
            print(f"    {msg}")
        elif cmd == "clear":
            _ok, _reason, msg = scene_manager.set_active_scene(None)
            print(f"    {msg}")
        elif cmd == "schedules":
            for s in scene_manager.get_schedules():
                print(f"    {s['time']}  {s['scene']:<20} {s['repeat']:<6} {s['id']}")
        elif cmd == "health":
            print("    " + json.dumps(scene_manager.health(), indent=2).replace("\n", "\n    "))
        else:
            print(f"    Unknown command: {line}")
            print(CLI_HELP)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def preflight_bind_check(port: int) -> None:
    """Fail fast if the port is already taken.

    Werkzeug's dev server sets SO_REUSEADDR for fast restart, which on Windows
    lets a second process bind silently -- masking duplicate-instance bugs. A
    fresh socket with default options reports EADDRINUSE the way it should.
    """
    test = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        test.bind(("0.0.0.0", port))
    except OSError as e:
        print(f"\n  [FATAL] Cannot bind 0.0.0.0:{port}: {e}")
        print("          Another mur-scene-server (or other service) is already using this port.")
        print(f"          Check with: netstat -ano | findstr :{port}    (Windows)")
        print(f"                  or: ss -lntp 'sport = :{port}'         (Linux)")
        sys.exit(1)
    finally:
        test.close()


def main():
    global config, scene_manager

    parser = argparse.ArgumentParser(
        description="Mur Scene Server -- fleet-wide active-scene authority")
    parser.add_argument("-p", "--port", type=int, default=None,
                        help=f"HTTP port (default: ${PORT_ENV_VAR} or {DEFAULT_PORT})")
    parser.add_argument("--ephemeral", action="store_true",
                        help="In-memory only: seed from config.json, persist nothing. "
                             "Replaces the old mock-scene-server. Implies --cli.")
    parser.add_argument("--cli", action="store_true",
                        help="Enable the interactive command prompt")
    args = parser.parse_args()

    port = args.port or int(os.environ.get(PORT_ENV_VAR, DEFAULT_PORT))
    config = load_config()
    preflight_bind_check(port)

    scene_manager = SceneManager(config, ephemeral=args.ephemeral)

    logger.info("=" * 64)
    logger.info("Mur Scene Server starting")
    logger.info("=" * 64)
    logger.info("Web UI:          http://0.0.0.0:%d/", port)
    logger.info("State:           %s", "EPHEMERAL (nothing persisted)"
                if args.ephemeral else SCENES_FILE)
    logger.info("Scenes:          %s", scene_manager.get_scenes())
    logger.info("Active scene:    %r", scene_manager.get_active_scene())
    logger.info("Schedules:       %d", len(scene_manager.get_schedules()))
    logger.info("Trigger server:  %s", config["trigger_server_url"] or "disabled")
    logger.info("=" * 64)

    # Only offer the prompt when there is a human on the other end. Under
    # systemd or a background launch stdin is not a terminal, and a CLI thread
    # there just spins on EOF.
    if (args.cli or args.ephemeral) and sys.stdin is not None and sys.stdin.isatty():
        threading.Thread(target=cli_loop, daemon=True, name="scene-cli").start()

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
