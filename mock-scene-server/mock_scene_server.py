#!/usr/bin/env python3
"""
Mock Scene Service for end-to-end Murmura testing.

Replaces the real Haven Scene Service so you can test the gateway↔MUR path
without depending on the production scene/flame/OSC stack. Provides only
what mur_gateway and the SYNC_DESIGN test flow actually need:

  - GET    /api/scenes/active     (gateway prime + lazy refresh)
  - POST   /api/scenes/active     (testing: change the active scene)
  - GET    /api/scenes            (testing visibility)
  - POST   /api/scenes            (testing: create a scene)
  - DELETE /api/scenes/<name>     (testing: delete a scene)
  - GET    /health

State is in-memory only — restart wipes everything. A small set of seed
scenes ("default", "night", "day") is pre-populated so the gateway's prime
cache succeeds immediately without manual setup.

Usage:
    python mock_scene_server.py
    python mock_scene_server.py --port 5003

The trigger_server_url (where SceneChange events are pushed when the
active scene changes) is configured in config.json alongside this file.
Default is http://127.0.0.1:5002 (mock-trigger-server's default port).
Set it to null or empty string in config.json to disable the push.
"""

import argparse
import json
import logging
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

DEFAULT_PORT = 5003
SCENE_TRIGGER_NAME = "SceneChange"

# Seed scene defaults baked in; config.json (alongside this file) overrides
# any field. Mirrors the mur-gateway/config.json pattern so on-site editing
# doesn't require touching Python source.
SEED_DEFAULTS = {
    "scenes": ["default", "night", "day"],
    "active_scene": "default",
    # mock-trigger-server's default port is 5002. Empty string or null
    # disables the SceneChange push.
    "trigger_server_url": "http://127.0.0.1:5002",
}
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def load_seed_config(path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Load seed scenes, initial active scene, and trigger-server URL from
    config.json. Falls back to built-in defaults for any missing or invalid
    field. Active scene must be either null or one of the listed scenes;
    otherwise the default is used and a warning is printed."""
    cfg = {
        "scenes": list(SEED_DEFAULTS["scenes"]),
        "active_scene": SEED_DEFAULTS["active_scene"],
        "trigger_server_url": SEED_DEFAULTS["trigger_server_url"],
    }
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        if not isinstance(user_cfg, dict):
            print(f"  [CONFIG] {path} is not a JSON object — using built-in defaults")
            return cfg

        scenes = user_cfg.get("scenes")
        if isinstance(scenes, list) and scenes \
                and all(isinstance(s, str) and s.strip() for s in scenes):
            cfg["scenes"] = [s.strip() for s in scenes]
        elif scenes is not None:
            print(f"  [CONFIG] scenes must be a non-empty list of strings; using built-in defaults")

        if "active_scene" in user_cfg:
            active = user_cfg["active_scene"]
            if active is None:
                cfg["active_scene"] = None
            elif isinstance(active, str) and active in cfg["scenes"]:
                cfg["active_scene"] = active
            else:
                print(f"  [CONFIG] active_scene {active!r} not in scenes — using {cfg['active_scene']!r}")

        if "trigger_server_url" in user_cfg:
            url = user_cfg["trigger_server_url"]
            if url is None or url == "":
                cfg["trigger_server_url"] = None  # explicit disable
            elif isinstance(url, str):
                cfg["trigger_server_url"] = url.strip()
            else:
                print(f"  [CONFIG] trigger_server_url must be a string or null; using {cfg['trigger_server_url']!r}")

        print(f"  [CONFIG] Loaded seed config from {path}")
    except FileNotFoundError:
        print(f"  [CONFIG] No {path.name} found — using built-in defaults")
    except json.JSONDecodeError as e:
        print(f"  [CONFIG] Bad JSON in {path} ({e}) — using built-in defaults")
    return cfg


_seed_config = load_seed_config()
SEED_SCENES = _seed_config["scenes"]
SEED_ACTIVE = _seed_config["active_scene"]

state_lock = threading.Lock()
scenes: set[str] = set(SEED_SCENES)
active_scene: Optional[str] = SEED_ACTIVE

# Set from config.json's trigger_server_url. We POST a SceneChange event to
# this URL on every active-scene change so the full trigger fan-out is
# exercisable end-to-end. Null/empty in config.json disables the push.
_tsu = _seed_config["trigger_server_url"]
trigger_server_url: Optional[str] = _tsu.rstrip("/") if _tsu else None


def _push_scene_change(scene_name: Optional[str]) -> None:
    """Best-effort SceneChange push to a mock-trigger-server (if configured)."""
    if not trigger_server_url:
        return
    url = f"{trigger_server_url.rstrip('/')}/api/trigger-event"
    payload = {"name": SCENE_TRIGGER_NAME, "value": scene_name if scene_name else ""}
    try:
        r = requests.post(url, json=payload, timeout=2)
        if r.ok:
            print(f"  [SCENE_CHANGE_PUSH] {scene_name!r} → {url}")
        else:
            print(f"  [SCENE_CHANGE_PUSH] HTTP {r.status_code} from {url}: {r.text}")
    except requests.RequestException as e:
        print(f"  [SCENE_CHANGE_PUSH] could not reach {url}: {e}")


# --- HTTP API (subset of the real scene_service) ---

@app.route("/api/scenes/active", methods=["GET"])
def get_active_scene():
    with state_lock:
        return jsonify({"active_scene": active_scene}), 200


@app.route("/api/scenes/active", methods=["POST"])
def set_active_scene():
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    with state_lock:
        if name is None:
            cleared = active_scene is not None
            globals()["active_scene"] = None
            print(f"  [ACTIVE] cleared (was {'set' if cleared else 'already null'})")
        else:
            if not isinstance(name, str) or not name.strip():
                return jsonify({"error": "name must be a non-empty string"}), 400
            name = name.strip()
            if name not in scenes:
                return jsonify({"error": f"Scene '{name}' does not exist"}), 400
            globals()["active_scene"] = name
            print(f"  [ACTIVE] {name}")
    threading.Thread(target=_push_scene_change, args=(name,), daemon=True).start()
    return jsonify({"active_scene": name, "message": "Active scene updated"}), 200


@app.route("/api/scenes", methods=["GET"])
def list_scenes():
    with state_lock:
        return jsonify({
            "scenes": sorted(scenes),
            "active_scene": active_scene,
            "count": len(scenes),
        }), 200


@app.route("/api/scenes", methods=["POST"])
def create_scene():
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return jsonify({"error": "name must be a non-empty string"}), 400
    name = name.strip()
    with state_lock:
        if name in scenes:
            return jsonify({"error": f"Scene '{name}' already exists"}), 400
        scenes.add(name)
        print(f"  [CREATE] {name}")
    return jsonify({"scene": name, "message": f"Scene '{name}' created"}), 201


@app.route("/api/scenes/<name>", methods=["DELETE"])
def delete_scene(name):
    with state_lock:
        if name not in scenes:
            return jsonify({"error": f"Scene '{name}' does not exist"}), 404
        if active_scene == name:
            return jsonify({"error": f"Cannot delete the active scene '{name}'"}), 400
        scenes.discard(name)
        print(f"  [DELETE] {name}")
    return jsonify({"message": f"Scene '{name}' deleted"}), 200


@app.route("/health", methods=["GET"])
def health():
    with state_lock:
        return jsonify({
            "status": "healthy",
            "service": "mock_scene_server",
            "scenes_count": len(scenes),
            "active_scene": active_scene,
            "trigger_server_url": trigger_server_url,
        }), 200


# --- Interactive CLI ---

def print_help():
    print("""
  Commands:
    list                  Show all scenes + active
    create <name>         Create a scene
    delete <name>         Delete a scene
    activate <name>       Set the active scene (fires SceneChange push if configured)
    clear                 Clear the active scene
    help                  Show this help
    quit                  Exit
""")


def cli_loop():
    time.sleep(1)
    print("\n  Mock Scene Server ready.")
    print(f"  Seeded scenes: {sorted(SEED_SCENES)}, active = {SEED_ACTIVE!r}")
    if trigger_server_url:
        print(f"  SceneChange push → {trigger_server_url}/api/trigger-event")
    else:
        print("  SceneChange push: disabled (no --trigger-server-url)")
    print("  Type 'help' for commands.\n")

    while True:
        try:
            line = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Shutting down...")
            import os
            os._exit(0)
        if not line:
            continue
        parts = line.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "help":
            print_help()
        elif cmd in ("quit", "exit"):
            print("  Shutting down...")
            import os
            os._exit(0)
        elif cmd == "list":
            with state_lock:
                print(f"    active: {active_scene!r}")
                for s in sorted(scenes):
                    marker = " *" if s == active_scene else "  "
                    print(f"    {marker} {s}")
        elif cmd == "create" and arg:
            with state_lock:
                if arg in scenes:
                    print(f"    already exists: {arg}")
                else:
                    scenes.add(arg)
                    print(f"    created: {arg}")
        elif cmd == "delete" and arg:
            with state_lock:
                if arg not in scenes:
                    print(f"    no such scene: {arg}")
                elif active_scene == arg:
                    print(f"    cannot delete active scene: {arg}")
                else:
                    scenes.discard(arg)
                    print(f"    deleted: {arg}")
        elif cmd == "activate" and arg:
            with state_lock:
                if arg not in scenes:
                    print(f"    no such scene: {arg}")
                    continue
                globals()["active_scene"] = arg
                print(f"    active: {arg}")
            threading.Thread(target=_push_scene_change, args=(arg,), daemon=True).start()
        elif cmd == "clear":
            with state_lock:
                globals()["active_scene"] = None
                print("    active cleared")
            threading.Thread(target=_push_scene_change, args=(None,), daemon=True).start()
        else:
            print(f"    Unknown command: {line}")
            print_help()


def preflight_bind_check(port: int) -> None:
    """Fail fast if the port is already taken. Werkzeug's dev server sets
    SO_REUSEADDR=True for fast restart, which on Windows lets a second
    process bind silently — masking duplicate-instance bugs. A fresh socket
    with default options reports EADDRINUSE the way it should."""
    test = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        test.bind(("0.0.0.0", port))
    except OSError as e:
        print(f"\n  [FATAL] Cannot bind 0.0.0.0:{port}: {e}")
        print(f"          Another mock-scene-server (or other service) is already using this port.")
        print(f"          Check with: netstat -ano | findstr :{port}    (Windows)")
        print(f"                  or: ss -lntp 'sport = :{port}'         (Linux)")
        sys.exit(1)
    finally:
        test.close()


def main():
    parser = argparse.ArgumentParser(
        description="Mock Scene Service for end-to-end Murmura testing"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"HTTP API port (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    preflight_bind_check(args.port)

    print(f"\n  Mock Scene Server")
    print(f"  HTTP API on port {args.port}")
    print(f"  Seeded scenes: {sorted(SEED_SCENES)}")
    print(f"  Initial active scene: {SEED_ACTIVE!r}")
    if trigger_server_url:
        print(f"  SceneChange push: {trigger_server_url}/api/trigger-event")
    else:
        print(f"  SceneChange push: disabled (trigger_server_url null/empty in config.json)")

    cli_thread = threading.Thread(target=cli_loop, daemon=True)
    cli_thread.start()

    log = logging.getLogger("werkzeug")
    log.setLevel(logging.WARNING)

    app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()
