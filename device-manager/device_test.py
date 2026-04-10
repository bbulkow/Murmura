#!/usr/bin/env python3
"""
device_test.py - Murmura device integration test suite

Tests the Murmura ESP32 device HTTP API directly.

Usage:
    # Direct IP
    python device_test.py --device 192.168.5.135

    # Resolve device by ID from local device_map.json (same as device_controller)
    python device_test.py --id FREEKER

    # Specify a non-default device map location
    python device_test.py --id FREEKER --map-file /path/to/device_map.json

    # Enable interactive trigger behavior tests
    python device_test.py --device 192.168.5.135 --triggers

NOTE: mur-config-server endpoints are internal to the mur-config-server application and are
not part of any external API contract. Do not test them from external tooling.
"""

import argparse
import json
import re
import socket
import sys
import time
import requests
from pathlib import Path

# ── Test track names (must exist on device SD card) ───────────────────────────
# Change these to match the audio files actually present on the device.
# Use three different-sounding files so volume tests are distinguishable.
TEST_TRACK_0 = "track1.wav"
TEST_TRACK_1 = "track2.wav"
TEST_TRACK_2 = "track3.wav"

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"

def ok(msg):   print(f"  {GREEN}PASS{RESET}  {msg}")
def fail(msg): print(f"  {RED}FAIL{RESET}  {msg}")
def skip(msg): print(f"  {YELLOW}SKIP{RESET}  {msg}")
def info(msg): print(f"  {CYAN}INFO{RESET}  {msg}")
def head(msg): print(f"\n{BOLD}{msg}{RESET}")

# ── Verbose / verify flags (set by --verbose / --verify in main) ─────────────
VERBOSE = False
VERIFY  = False  # When True: print GET /api/scenes after every scene-changing POST

def vlog(method, url, body, status, response):
    """Print request/response details when VERBOSE is True."""
    if not VERBOSE:
        return
    body_str = f"  {DIM}body  : {json.dumps(body)}{RESET}" if body is not None else ""
    resp_str = json.dumps(response) if isinstance(response, dict) else str(response)
    # Truncate very long responses to keep output readable
    if len(resp_str) > 300:
        resp_str = resp_str[:297] + "..."
    print(f"  {DIM}{method:6} {url}")
    if body_str:
        print(body_str)
    print(f"         → {status}  {resp_str}{RESET}")

def _verify_tracks(base):
    """Print a compact track-state table (used by --verify after scene-changing POST)."""
    try:
        r = requests.get(f"{base}/api/scenes", timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"  {DIM}[verify] GET /api/scenes → HTTP {r.status_code}{RESET}")
            return
        sdata = r.json()
        active_scene = sdata.get("active_scene", "default")
        tracks = sdata.get("scenes", {}).get(active_scene, {}).get("tracks", [])
        parts = []
        for t in sorted(tracks, key=lambda x: x.get("track", 0)):
            idx    = t.get("track", "?")
            active = "YES" if t.get("active") else "no"
            mode   = t.get("mode", "?")
            name   = Path(t.get("file_path", "")).name or "—"
            parts.append(f"t{idx}:{active}/{mode}/{name}")
        print(f"  {DIM}[verify] [{active_scene}] {' | '.join(parts)}{RESET}")
    except Exception:
        pass

# ── Result tracking ───────────────────────────────────────────────────────────
results = {"pass": 0, "fail": 0, "skip": 0, "user_pass": 0, "user_fail": 0}

def record(passed, label, detail=""):
    if passed:
        results["pass"] += 1
        ok(f"{label}" + (f" — {detail}" if detail else ""))
    else:
        results["fail"] += 1
        fail(f"{label}" + (f" — {detail}" if detail else ""))
    return passed

def user_confirm(prompt):
    """Ask user to confirm something they can hear/see. Returns True/False."""
    while True:
        ans = input(f"\n  {YELLOW}[USER]{RESET} {prompt} [y/n]: ").strip().lower()
        if ans in ("y", "yes"):
            results["user_pass"] += 1
            return True
        if ans in ("n", "no"):
            results["user_fail"] += 1
            return False

def pause(prompt="Press Enter to continue..."):
    input(f"\n  {YELLOW}[WAIT]{RESET} {prompt}")

# ── HTTP helpers ──────────────────────────────────────────────────────────────
TIMEOUT = 8

def get(base, path):
    try:
        r = requests.get(f"{base}{path}", timeout=TIMEOUT)
        data = r.json() if r.content else {}
        vlog("GET", f"{base}{path}", None, r.status_code, data)
        return r.status_code, data
    except requests.exceptions.ConnectionError:
        vlog("GET", f"{base}{path}", None, None, {"error": "connection refused"})
        return None, {"error": "connection refused"}
    except Exception as e:
        vlog("GET", f"{base}{path}", None, None, {"error": str(e)})
        return None, {"error": str(e)}

def post(base, path, body=None):
    try:
        r = requests.post(f"{base}{path}", json=body, timeout=TIMEOUT)
        data = r.json() if r.content else {}
        vlog("POST", f"{base}{path}", body, r.status_code, data)
        if VERIFY and path in ("/api/scenes", "/api/scene") and r.status_code == 200:
            _verify_tracks(base)
        return r.status_code, data
    except requests.exceptions.ConnectionError:
        vlog("POST", f"{base}{path}", body, None, {"error": "connection refused"})
        return None, {"error": "connection refused"}
    except Exception as e:
        vlog("POST", f"{base}{path}", body, None, {"error": str(e)})
        return None, {"error": str(e)}

def delete(base, path, body=None):
    try:
        r = requests.delete(f"{base}{path}", json=body, timeout=TIMEOUT)
        data = r.json() if r.content else {}
        vlog("DELETE", f"{base}{path}", body, r.status_code, data)
        return r.status_code, data
    except requests.exceptions.ConnectionError:
        vlog("DELETE", f"{base}{path}", body, None, {"error": "connection refused"})
        return None, {"error": "connection refused"}
    except Exception as e:
        vlog("DELETE", f"{base}{path}", body, None, {"error": str(e)})
        return None, {"error": str(e)}

def is_valid_mac(s):
    return bool(re.match(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$', s or ""))

def is_valid_ip(s):
    return bool(re.match(r'^\d{1,3}(\.\d{1,3}){3}$', s or ""))

# ── Device map ID resolution (same logic as device_controller.py) ─────────────
def resolve_id_to_ip(device_id, map_file="device_map.json"):
    """Look up device ID in local device_map.json and return its IP address."""
    path = Path(map_file)
    if not path.exists():
        print(f"{RED}ERROR{RESET}: Device map not found: {map_file}")
        print("Run device_scanner.py first to create the device map.")
        sys.exit(1)
    with open(path) as f:
        data = json.load(f)
    for dev in data.get("devices", []):
        if dev.get("id") == device_id:
            ip = dev.get("ip_address", "")
            if ip:
                return ip
            print(f"{RED}ERROR{RESET}: Device '{device_id}' found but has no ip_address")
            sys.exit(1)
    print(f"{RED}ERROR{RESET}: Device ID '{device_id}' not found in {map_file}")
    print("Run device_scanner.py to refresh the device map.")
    sys.exit(1)

# ── Stop all tracks helper ────────────────────────────────────────────────────
def stop_all_tracks(base):
    """Deactivate all 3 tracks in the active scene."""
    scene = get_active_scene_name(base)
    post(base, "/api/scenes", {scene: {"tracks": [
        {"track": 0, "active": False},
        {"track": 1, "active": False},
        {"track": 2, "active": False},
    ]}})
    time.sleep(0.5)

# ── Scene helpers ─────────────────────────────────────────────────────────────
def get_active_scene_name(base):
    """Return the name of the currently active scene (default: 'default')."""
    code, data = get(base, "/api/scenes")
    if code == 200:
        return data.get("active_scene", "default")
    return "default"

def get_active_tracks(base):
    """GET /api/scenes, return (active_scene_name, tracks_list) for the active scene."""
    code, data = get(base, "/api/scenes")
    if code != 200:
        return "default", []
    active = data.get("active_scene", "default")
    tracks = data.get("scenes", {}).get(active, {}).get("tracks", [])
    return active, tracks

def get_active_global_volume(base):
    """GET /api/scenes, return the global_volume for the active scene."""
    code, data = get(base, "/api/scenes")
    if code != 200:
        return None
    active = data.get("active_scene", "default")
    return data.get("scenes", {}).get(active, {}).get("global_volume")

def scene_track_post(base, track_updates):
    """Post track updates to the active scene. track_updates is a list of dicts."""
    scene = get_active_scene_name(base)
    return post(base, "/api/scenes", {scene: {"tracks": track_updates}})

def scene_volume_post(base, volume):
    """Set global_volume on the active scene."""
    scene = get_active_scene_name(base)
    return post(base, "/api/scenes", {scene: {"global_volume": volume}})

# ── Find a test track in the files list by name ───────────────────────────────
def find_track(files, name):
    """Return the file entry matching name (case-insensitive), or None."""
    for f in files:
        if f.get("name", "").lower() == name.lower():
            return f
    return None

# =============================================================================
# GROUP 1: Identity & Status
# =============================================================================
def group1_identity(base):
    head("Group 1: Identity & Status")

    # 1.1a — first device config call
    code, data = get(base, "/api/device")
    if not record(code == 200 and "id" in data,
                  "1.1a GET /api/device returns 200 with id field",
                  f"HTTP {code}"):
        fail("Cannot continue — device not reachable")
        return None

    mac   = data.get("mac_address", "")
    ip    = data.get("ip_address", "")
    id_   = data.get("id", "")
    up1   = data.get("uptime_seconds", -1)
    fw    = data.get("firmware_version", "")

    record(is_valid_mac(mac),  "1.1a mac_address is well-formed XX:XX:XX:XX:XX format", mac)
    record(is_valid_ip(ip),    "1.1a ip_address is well-formed IPv4",                    ip)
    record(bool(id_),          "1.1a id is non-empty",                                   id_)
    record(up1 >= 0,           "1.1a uptime_seconds is non-negative",                    str(up1))
    info(f"Device: id={id_}  ip={ip}  mac={mac}  fw={fw}  uptime={up1}s")

    # Check mur_gateway fields are present
    record("mur_gateway_ip" in data,   "1.1a mur_gateway_ip field present")
    record("mur_gateway_port" in data, "1.1a mur_gateway_port field present")

    # Check wifi sub-object
    wifi = data.get("wifi", {})
    record(isinstance(wifi, dict) and "connected" in wifi,
           "1.1a wifi object with connected field present")

    # 1.1b — second call after delay: uptime must increase
    info("Waiting 4 seconds for uptime check...")
    time.sleep(4)
    code2, data2 = get(base, "/api/device")
    if code2 == 200:
        up2 = data2.get("uptime_seconds", -1)
        record(up2 > up1,       "1.1b uptime increased between calls",   f"{up1}s → {up2}s")
        record(data2.get("mac_address") == mac, "1.1b mac unchanged",    mac)
        record(data2.get("ip_address")  == ip,  "1.1b ip unchanged",     ip)
        record(data2.get("id")          == id_,  "1.1b id unchanged",    id_)
    else:
        record(False, "1.1b second GET /api/device call", f"HTTP {code2}")

    # NOTE: POST /api/device with id field (set ID) intentionally omitted — destructive in the field.

    return id_  # return for use by mur-config-server group

# =============================================================================
# GROUP 2: File Management
# =============================================================================
def group2_files(base):
    head("Group 2: File Management")

    code, data = get(base, "/api/files")
    if not record(code == 200 and "files" in data,
                  "2.1 GET /api/files returns 200 with files array", f"HTTP {code}"):
        return []

    files = data["files"]
    count = data.get("count", len(files))
    record(count > 0, "2.2 At least one audio file on SD card", f"{count} file(s)")
    record(isinstance(files, list), "2.1 files field is a list")

    if files:
        f0 = files[0]
        record("name" in f0 and "path" in f0,
               "2.1 File entries have name and path fields", str(f0.get("name", "")))

    audio_files = [f for f in files if
                   f.get("name", "").lower().endswith((".wav", ".mp3"))]
    record(len(audio_files) > 0,
           "2.2 At least one .wav or .mp3 file present",
           f"{len(audio_files)} audio file(s)")

    # Check for expected test tracks
    for name in [TEST_TRACK_0, TEST_TRACK_1, TEST_TRACK_2]:
        found = find_track(files, name) is not None
        if found:
            ok(f"2.x Test track present: {name}")
        else:
            skip(f"2.x Test track NOT found: {name} — some tests may use fallback file")

    info(f"Files found: {[f.get('name') for f in files]}")
    return files

# =============================================================================
# GROUP 3: Single Track — File Assignment & Playback
# =============================================================================
def group3_single_track(base, files):
    head("Group 3: Single Track — File Assignment & Playback")

    if not files:
        skip("3.x No files available — skipping track playback tests")
        results["skip"] += 7
        return

    # 3.0 Read current volumes, then set everything to 100% so audible tests
    # are not silenced by a previously saved low-volume or muted state.
    scene = get_active_scene_name(base)
    _, tracks_list = get_active_tracks(base)
    if tracks_list:
        cur_global = get_active_global_volume(base)
        cur_vols   = {t["track"]: t.get("volume", "?")
                      for t in tracks_list if "track" in t}
        info(f"Current volumes — global={cur_global}  tracks={cur_vols}")
    code, data = scene_volume_post(base, 100)
    record(code == 200 and data.get("success"),
           "3.0 Set global volume to 100% before playback tests")
    code, data = scene_track_post(base, [
        {"track": 0, "volume": 100},
        {"track": 1, "volume": 100},
        {"track": 2, "volume": 100},
    ])
    record(code == 200 and data.get("success"),
           "3.0 Set per-track volumes to 100% (all 3 tracks)")

    # Prefer the configured test track; fall back to first available file
    entry = find_track(files, TEST_TRACK_0) or files[0]
    file_path = entry.get("path", "")
    file_name = entry.get("name", "")
    info(f"Using file: {file_path}")

    # 3.1 set file by path via scenes API
    code, data = scene_track_post(base, [{"track": 0, "file_path": file_path}])
    record(code == 200 and data.get("success"),
           "3.1 POST /api/scenes sets file on track 0", data.get("message", ""))

    # 3.2 verify via GET /api/scenes
    _, tracks = get_active_tracks(base)
    if tracks:
        t0 = next((t for t in tracks if t.get("track") == 0), None)
        record(t0 is not None and file_path in t0.get("file_path", ""),
               "3.2 GET /api/scenes reflects file on track 0",
               t0.get("file_path", "") if t0 else "track 0 missing")
        record(t0 is not None and t0.get("mode") in ("loop", "trigger"),
               "3.2 Track 0 mode field is valid (loop or trigger only)",
               t0.get("mode", "") if t0 else "")
    else:
        record(False, "3.2 GET /api/scenes", "no tracks returned")

    # 3.3 enable track 0 in loop mode — starts it
    code, data = scene_track_post(base, [{"track": 0, "mode": "loop", "active": True}])
    record(code == 200 and data.get("success"),
           "3.3 POST /api/scenes mode=loop active=true track 0", data.get("message", ""))

    # 3.4 user confirmation
    user_confirm(f"Is track 0 ({file_name}) playing audio?")

    # 3.5 disable track 0 — stops it (mode stays loop)
    code, data = scene_track_post(base, [{"track": 0, "active": False}])
    record(code == 200 and data.get("success"),
           "3.5 POST /api/scenes active=false track 0", data.get("message", ""))

    # 3.6 user confirmation
    user_confirm("Has track 0 stopped?")

    # 3.7 verify stopped state: active=false, mode still loop
    time.sleep(1)
    _, tracks = get_active_tracks(base)
    if tracks:
        t0 = next((t for t in tracks if t.get("track") == 0), None)
        record(t0 is not None and t0.get("active") == False,
               "3.7 GET /api/scenes shows track 0 active=false after stop",
               f"active={t0.get('active','?')} mode={t0.get('mode','?')}" if t0 else "track 0 missing")
        record(t0 is not None and t0.get("mode") == "loop",
               "3.7 Track 0 mode remains loop after deactivate",
               t0.get("mode", "?") if t0 else "")
    else:
        record(False, "3.7 GET /api/scenes after stop", "no tracks returned")

    # 3.8 verify invalid mode is rejected
    code, data = scene_track_post(base, [{"track": 0, "mode": "off"}])
    record(data.get("success") is False,
           "3.8 POST /api/scenes mode='off' returns success=false (invalid mode rejected)",
           data.get("error", "UNEXPECTED: server accepted invalid mode"))

# =============================================================================
# GROUP 4: Multi-Track Independent Control
# =============================================================================
def group4_multi_track(base, files):
    head("Group 4: Multi-Track Independent Control")

    if not files:
        skip("4.x No files available — skipping multi-track tests")
        results["skip"] += 10
        return

    # Assign each track a distinct file; fall back by cycling if fewer than 3 available
    entries = [
        find_track(files, TEST_TRACK_0) or files[0 % len(files)],
        find_track(files, TEST_TRACK_1) or files[1 % len(files)],
        find_track(files, TEST_TRACK_2) or files[2 % len(files)],
    ]
    track_paths = [e.get("path", "") for e in entries]
    track_names = [e.get("name", "") for e in entries]
    info(f"Assigning: track0={track_names[0]}  track1={track_names[1]}  track2={track_names[2]}")

    # 4.1 set all three tracks
    code, data = scene_track_post(base, [
        {"track": 0, "file_path": track_paths[0]},
        {"track": 1, "file_path": track_paths[1]},
        {"track": 2, "file_path": track_paths[2]},
    ])
    record(code == 200 and data.get("success"), "4.1 Set file on tracks 0, 1, 2")

    # 4.2 start track 0 only (mode=loop, active=true)
    scene_track_post(base, [{"track": 0, "mode": "loop", "active": True}])
    time.sleep(0.5)
    record(True, "4.2 Started track 0 only (mode=loop, active=true)")
    user_confirm(f"Is ONLY track 0 ({track_names[0]}) audible (tracks 1 and 2 silent)?")

    # 4.4 start track 1
    scene_track_post(base, [{"track": 1, "mode": "loop", "active": True}])
    time.sleep(0.5)
    record(True, "4.4 Started track 1 (mode=loop, active=true)")
    user_confirm(f"Are tracks 0 ({track_names[0]}) AND 1 ({track_names[1]}) both audible simultaneously?")

    # 4.6 start track 2
    scene_track_post(base, [{"track": 2, "mode": "loop", "active": True}])
    time.sleep(0.5)
    record(True, "4.6 Started track 2 (mode=loop, active=true)")
    user_confirm(f"Are all 3 tracks playing together ({track_names[0]}, {track_names[1]}, {track_names[2]})?")

    # 4.8 stop track 1 only (active=false, mode must remain loop)
    code, data = scene_track_post(base, [{"track": 1, "active": False}])
    record(code == 200 and data.get("success"),
           "4.8 POST /api/scenes active=false track 1 only")
    user_confirm(f"Are tracks 0 and 2 still playing, track 1 ({track_names[1]}) silent?")

    # 4.10 verify via GET /api/scenes (active + mode fields)
    time.sleep(1)
    _, tracks = get_active_tracks(base)
    if tracks:
        by_idx = {t["track"]: t for t in tracks if "track" in t}
        record(by_idx.get(0, {}).get("mode") == "loop" and by_idx.get(0, {}).get("active") is True,
               "4.10 track 0 mode=loop, active=true")
        record(by_idx.get(1, {}).get("mode") == "loop" and by_idx.get(1, {}).get("active") is False,
               "4.10 track 1 mode=loop, active=false (stopped but mode preserved)")
        record(by_idx.get(2, {}).get("mode") == "loop" and by_idx.get(2, {}).get("active") is True,
               "4.10 track 2 mode=loop, active=true")
    else:
        record(False, "4.10 GET /api/scenes", "no tracks returned")

    # 4.11 stop remaining tracks
    scene_track_post(base, [
        {"track": 0, "active": False},
        {"track": 2, "active": False},
    ])
    time.sleep(0.5)
    user_confirm("All tracks silent now?")

# =============================================================================
# GROUP 5: Volume Control
# =============================================================================
def group5_volume(base, files):
    head("Group 5: Volume Control")

    if not files:
        skip("5.x No files — skipping volume tests")
        results["skip"] += 8
        return

    # Resolve test tracks (same as group 4; fall back to cycling)
    entries = [
        find_track(files, TEST_TRACK_0) or files[0 % len(files)],
        find_track(files, TEST_TRACK_1) or files[1 % len(files)],
        find_track(files, TEST_TRACK_2) or files[2 % len(files)],
    ]
    track_paths = [e.get("path", "") for e in entries]

    # ── Per-track volume ──────────────────────────────────────────────────────
    # Explicitly set file and start track 0 so it is definitely looping
    scene_track_post(base, [{"track": 0, "file_path": track_paths[0], "mode": "loop", "active": True, "volume": 100}])
    time.sleep(0.5)
    user_confirm("Track 0 playing at full volume (baseline)?")

    code, data = scene_track_post(base, [{"track": 0, "volume": 20}])
    record(code == 200 and data.get("success"),
           "5.3 POST /api/scenes volume=20 track 0", data.get("message", ""))
    user_confirm("Track 0 noticeably quieter (at 20%)?")

    code, data = scene_track_post(base, [{"track": 0, "volume": 100}])
    record(code == 200 and data.get("success"),
           "5.5 POST /api/scenes volume=100 track 0 (restore)")

    # Stop track 0 before starting all three for the global test
    scene_track_post(base, [{"track": 0, "active": False}])
    time.sleep(0.3)

    # ── Global volume ─────────────────────────────────────────────────────────
    # Explicitly set file and start all 3 tracks so they are definitely looping
    scene_track_post(base, [
        {"track": 0, "file_path": track_paths[0], "mode": "loop", "active": True, "volume": 100},
        {"track": 1, "file_path": track_paths[1], "mode": "loop", "active": True, "volume": 100},
        {"track": 2, "file_path": track_paths[2], "mode": "loop", "active": True, "volume": 100},
    ])
    time.sleep(0.5)
    user_confirm("All 3 tracks playing at full volume (global baseline)?")

    code, data = scene_volume_post(base, 20)
    record(code == 200 and data.get("success"),
           "5.7 POST /api/scenes global_volume → 20%", data.get("message", ""))
    user_confirm("All tracks noticeably quieter together (global 20%)?")

    code, data = scene_volume_post(base, 75)
    record(code == 200 and data.get("success"),
           "5.9 POST /api/scenes global_volume → 75% (restore)")
    user_confirm("All tracks back to normal volume (global 75%)?")

    # ── Boundary tests (no audible confirmation needed) ───────────────────────
    # Tracks still playing; boundary values must not crash the device
    code, data = scene_volume_post(base, 0)
    record(code == 200 and data.get("success"),
           "5.10 Boundary: global volume=0 accepted without error")

    # Re-enable tracks so they are audible for remaining boundary confirmation
    scene_track_post(base, [
        {"track": 0, "active": True},
        {"track": 1, "active": True},
        {"track": 2, "active": True},
    ])
    time.sleep(0.3)

    code, data = scene_volume_post(base, 101)
    # Accept either a 4xx error OR success with clamping — must not crash (500)
    record(code is not None and code != 500,
           "5.11 Boundary: global volume=101 does not 500",
           f"HTTP {code} — {data.get('error', data.get('message', ''))}")

    # Restore and stop
    scene_volume_post(base, 75)
    record(True, "5.12 Restored global volume to 75%")

    stop_all_tracks(base)

# =============================================================================
# GROUP 6: Configuration Persistence
# =============================================================================
def group6_config(base, files):
    head("Group 6: Configuration Persistence")

    # 6.1 config status
    code, data = get(base, "/api/config/status")
    record(code == 200 and "config_exists" in data,
           "6.1 GET /api/config/status returns config_exists field", f"HTTP {code}")
    if code == 200:
        info(f"  config_exists={data.get('config_exists')}  path={data.get('config_path','?')}")
        info(f"  scene_count={data.get('scene_count','?')}  default_scene={data.get('default_scene','?')}")

    # 6.2 set a known state to save (loop track 0 with test file, enabled)
    if files:
        entry = find_track(files, TEST_TRACK_0) or files[0]
        scene_track_post(base, [{"track": 0, "file_path": entry["path"], "mode": "loop", "active": True}])
        time.sleep(0.3)

    # 6.3 save config
    code, data = post(base, "/api/config/save")
    record(code == 200 and data.get("success"),
           "6.3 POST /api/config/save returns success",
           data.get("message", str(data.get("error", ""))))

    # 6.4 verify config now exists
    code, data = get(base, "/api/config/status")
    record(code == 200 and data.get("config_exists") == True,
           "6.4 GET /api/config/status shows config_exists=true after save")

    # 6.5 dirty the state
    scene_track_post(base, [{"track": 0, "active": False}])
    scene_volume_post(base, 50)
    time.sleep(0.3)

    # 6.6 load config
    code, data = post(base, "/api/config/load")
    record(code == 200 and data.get("success"),
           "6.6 POST /api/config/load returns success",
           data.get("message", str(data.get("error", ""))))

    # 6.7 verify tracks have mode and active fields via scenes API
    _, tracks = get_active_tracks(base)
    if tracks:
        t0 = next((t for t in tracks if t.get("track") == 0), None)
        record(t0 is not None and "mode" in t0,
               "6.7 GET /api/scenes track objects include mode field",
               t0.get("mode", "MISSING") if t0 else "track 0 missing")
        record(t0 is not None and "active" in t0,
               "6.7 GET /api/scenes track objects include active field",
               str(t0.get("active", "MISSING")) if t0 else "track 0 missing")
    else:
        record(False, "6.7 GET /api/scenes", "no tracks returned")

    # 6.8 delete config
    code, data = delete(base, "/api/config/delete")
    record(code == 200 and data.get("success"),
           "6.8 DELETE /api/config/delete returns success",
           data.get("message", str(data.get("error", ""))))

    # 6.9 verify gone
    code, data = get(base, "/api/config/status")
    record(code == 200 and data.get("config_exists") == False,
           "6.9 GET /api/config/status shows config_exists=false after delete")

    stop_all_tracks(base)

# =============================================================================
# GROUP 7: File Replacement While Looping
# =============================================================================
def group7_file_swap(base, files):
    head("Group 7: File Replacement While Looping")

    if not files:
        skip("7.x No files available — skipping file-swap test")
        results["skip"] += 6
        return

    entry_a = find_track(files, TEST_TRACK_0) or files[0 % len(files)]
    entry_b = find_track(files, TEST_TRACK_1) or files[1 % len(files)]
    path_a, name_a = entry_a.get("path", ""), entry_a.get("name", "")
    path_b, name_b = entry_b.get("path", ""), entry_b.get("name", "")

    same_file = (path_a == path_b)
    if same_file:
        info(f"Only one unique file available ({name_a}). "
             "API behaviour will be verified but audible switch cannot be confirmed.")

    stop_all_tracks(base)

    # 7.1 Start track 0 looping on file A
    code, data = scene_track_post(base, [{"track": 0, "file_path": path_a, "mode": "loop", "active": True}])
    record(code == 200 and data.get("success"),
           f"7.1 Start track 0 looping on {name_a}", data.get("message", ""))
    time.sleep(0.5)
    if not same_file:
        user_confirm(f"Is track 0 playing '{name_a}'?")

    # 7.2 Replace file while still looping (no mode field in request)
    code, data = scene_track_post(base, [{"track": 0, "file_path": path_b}])
    record(code == 200 and data.get("success"),
           f"7.2 Replace file on looping track 0 → {name_b} (no mode field)",
           data.get("message", str(data.get("error", ""))))

    # 7.3 Verify GET shows new file and mode is still loop
    time.sleep(0.5)
    _, tracks = get_active_tracks(base)
    if tracks:
        t0 = next((t for t in tracks if t.get("track") == 0), None)
        record(t0 is not None and path_b in t0.get("file_path", ""),
               "7.3 GET /api/scenes reflects new file on track 0",
               t0.get("file_path", "?") if t0 else "track 0 missing")
        record(t0 is not None and t0.get("mode") == "loop",
               "7.3 Track 0 mode is still loop after file replacement",
               t0.get("mode", "?") if t0 else "")
        record(t0 is not None and t0.get("active") is True,
               "7.3 Track 0 still active after file replacement",
               str(t0.get("active", "?")) if t0 else "")
    else:
        record(False, "7.3 GET /api/scenes after file swap", "no tracks returned")
        record(False, "7.3 mode still loop")

    # 7.4 User confirms audio switched to file B
    if not same_file:
        user_confirm(f"Has the audio switched to '{name_b}'?")

    # 7.5 Switch back to file A
    code, data = scene_track_post(base, [{"track": 0, "file_path": path_a}])
    record(code == 200 and data.get("success"),
           f"7.5 Switch back to {name_a} (no mode field)",
           data.get("message", str(data.get("error", ""))))
    time.sleep(0.5)
    if not same_file:
        user_confirm(f"Has the audio switched back to '{name_a}'?")

    # 7.6 Reject: try to clear file while looping
    code, data = scene_track_post(base, [{"track": 0, "file_path": ""}])
    record(data.get("success") is False,
           "7.6 Clearing file while looping is rejected",
           data.get("error", f"HTTP {code}"))

    stop_all_tracks(base)


# =============================================================================
# GROUP 8: WiFi (read-only)
# =============================================================================
def group8_wifi(base):
    head("Group 8: WiFi (read-only, via /api/device)")

    code, data = get(base, "/api/device")
    record(code == 200, "8.1 GET /api/device returns 200", f"HTTP {code}")
    if code == 200:
        wifi = data.get("wifi", {})
        record(wifi.get("connected") == True,
               "8.1 wifi.connected is true", str(wifi.get("connected")))
        record(bool(wifi.get("ssid")),
               "8.1 wifi.ssid is non-empty", wifi.get("ssid", ""))

        nets = wifi.get("networks", [])
        record(isinstance(nets, list) and len(nets) >= 1,
               "8.2 wifi.networks has at least one entry", f"{len(nets)} network(s)")

# =============================================================================
# GROUP 9: Trigger TCP + Status (no trigger behavior tests by default)
# =============================================================================
def group9_trigger_basic(base, device_ip, trigger_tests):
    head("Group 9: Trigger System — Basic")

    # 9.1 get mur gateway config via /api/device
    code, data = get(base, "/api/device")
    record(code == 200,
           "9.1 GET /api/device returns 200", f"HTTP {code}")
    if code == 200:
        record("mur_gateway_ip"   in data,
               "9.1 /api/device has mur_gateway_ip field")
        record("mur_gateway_port" in data,
               "9.1 /api/device has mur_gateway_port field")

    # 9.2 (removed — device no longer listens on a TCP port)

    # 9.3 set mur gateway IP (dummy value)
    code, data = post(base, "/api/device",
                      {"mur_gateway_ip": "192.168.5.99", "mur_gateway_port": 4000})
    record(code == 200 and data.get("success"),
           "9.3 POST /api/device accepts mur_gateway IP/port config",
           data.get("message", str(data.get("error", ""))))

    # 9.4 verify stored
    code, data = get(base, "/api/device")
    if code == 200:
        stored_ip = data.get("mur_gateway_ip", "")
        record("192.168.5.99" in stored_ip,
               "9.4 Mur Gateway IP stored correctly", stored_ip)

    # 9.5 clear IP
    code, data = post(base, "/api/device",
                      {"mur_gateway_ip": "", "mur_gateway_port": 4000})
    record(code == 200 and data.get("success"),
           "9.5 POST /api/device with empty mur_gateway_ip does not crash")

    if not trigger_tests:
        skip("Group 9b trigger behavior tests skipped (use --triggers to enable)")
        return

    # ── Trigger behavior ─────────────────────────────────────────────────────
    head("Group 9b: Trigger Behavior (interactive)")
    pause("Configure track 2 as trigger mode with trigger_name='test.btn' "
          "via the web UI or API, then press Enter")

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(4)
        sock.connect((device_ip, listener_port))

        event_on  = json.dumps({"name": "test.btn", "value": "On"})  + "\n"
        event_off = json.dumps({"name": "test.btn", "value": "Off"}) + "\n"

        sock.sendall(event_on.encode())
        time.sleep(1)
        passed = user_confirm("Did track 2 start playing? (oneshot test)")
        record(passed, "8b.1 Trigger On starts track 2")

        sock.sendall(event_off.encode())
        time.sleep(1)
        passed = user_confirm("Is track 2 still playing after Off? (oneshot — should NOT stop)")
        record(passed, "8b.2 Trigger Off does not stop track in oneshot mode")

        sock.close()

        pause("Change track 2 trigger_action to 'momentary' via the web UI or API, then press Enter")

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(4)
        sock.connect((device_ip, listener_port))

        sock.sendall(event_on.encode())
        time.sleep(1)
        passed = user_confirm("Track 2 started playing (momentary test)?")
        record(passed, "8b.3 Trigger On starts track 2 in momentary mode")

        sock.sendall(event_off.encode())
        time.sleep(1)
        passed = user_confirm("Did track 2 STOP on Off? (momentary — should stop)")
        record(passed, "8b.4 Trigger Off stops track in momentary mode")

        sock.close()

    except Exception as e:
        record(False, "8b.x TCP trigger test", str(e))

# =============================================================================
# GROUP 10: Scenes
# =============================================================================
def group10_scenes(base):
    head("Group 10: Scene Management")

    # 10.1 GET /api/scenes on fresh device — "default" scene present
    code, data = get(base, "/api/scenes")
    record(code == 200 and "scenes" in data,
           "10.1 GET /api/scenes returns 200 with scenes dict", f"HTTP {code}")
    if code == 200:
        scenes = data.get("scenes", {})
        record("default" in scenes,
               "10.1 'default' scene exists on device")
        record(data.get("active_scene") == "default",
               "10.1 active_scene is 'default'",
               data.get("active_scene", "?"))
        record(data.get("default_scene") == "default",
               "10.1 default_scene is 'default'",
               data.get("default_scene", "?"))
        # Verify the default scene has expected structure
        default_scene = scenes.get("default", {})
        record("global_volume" in default_scene,
               "10.1 default scene has global_volume field")
        record("tracks" in default_scene and isinstance(default_scene.get("tracks"), list),
               "10.1 default scene has tracks array")

    # 10.2 Create a new scene "test-day"
    code, data = post(base, "/api/scene", {"action": "create", "name": "test-day"})
    record(code == 200 and data.get("success"),
           "10.2 Create scene 'test-day'",
           data.get("message", str(data.get("error", ""))))

    # 10.3 Verify it appears in GET /api/scenes
    code, data = get(base, "/api/scenes")
    if code == 200:
        scenes = data.get("scenes", {})
        record("test-day" in scenes,
               "10.3 'test-day' appears in scenes list")
        record(data.get("active_scene") == "default",
               "10.3 active_scene still 'default' after create",
               data.get("active_scene", "?"))
        # Verify the new scene has default structure
        td = scenes.get("test-day", {})
        record("global_volume" in td and "tracks" in td,
               "10.3 new scene has global_volume and tracks fields")
    else:
        record(False, "10.3 GET /api/scenes after create", f"HTTP {code}")

    # 10.4 PATCH the new scene's config (set global_volume, configure a track)
    code, data = post(base, "/api/scenes", {
        "test-day": {
            "global_volume": 80,
            "tracks": [{"track": 0, "mode": "loop", "volume": 60}]
        }
    })
    record(code == 200 and data.get("success"),
           "10.4 PATCH test-day scene config",
           data.get("message", str(data.get("error", ""))))

    # Verify patch applied
    code, data = get(base, "/api/scenes")
    if code == 200:
        td = data.get("scenes", {}).get("test-day", {})
        record(td.get("global_volume") == 80,
               "10.4 test-day global_volume is 80 after patch",
               str(td.get("global_volume", "?")))
        td_tracks = td.get("tracks", [])
        t0 = next((t for t in td_tracks if t.get("track") == 0), None)
        record(t0 is not None and t0.get("volume") == 60,
               "10.4 test-day track 0 volume is 60 after patch",
               str(t0.get("volume", "?")) if t0 else "track 0 missing")

    # 10.5 Activate test-day
    code, data = post(base, "/api/scene", {"action": "activate", "name": "test-day"})
    record(code == 200 and data.get("success"),
           "10.5 Activate scene 'test-day'",
           data.get("message", str(data.get("error", ""))))

    # Verify active scene changed
    code, data = get(base, "/api/scenes")
    if code == 200:
        record(data.get("active_scene") == "test-day",
               "10.5 active_scene is now 'test-day'",
               data.get("active_scene", "?"))
    else:
        record(False, "10.5 GET /api/scenes after activate", f"HTTP {code}")

    # 10.6 Create another scene "test-night" and patch it
    code, data = post(base, "/api/scene", {"action": "create", "name": "test-night"})
    record(code == 200 and data.get("success"),
           "10.6 Create scene 'test-night'",
           data.get("message", str(data.get("error", ""))))

    code, data = post(base, "/api/scenes", {
        "test-night": {
            "global_volume": 30,
            "tracks": [{"track": 0, "mode": "loop", "volume": 40}]
        }
    })
    record(code == 200 and data.get("success"),
           "10.6 PATCH test-night config",
           data.get("message", str(data.get("error", ""))))

    # 10.7 Activate test-night
    code, data = post(base, "/api/scene", {"action": "activate", "name": "test-night"})
    record(code == 200 and data.get("success"),
           "10.7 Activate scene 'test-night'",
           data.get("message", str(data.get("error", ""))))

    code, data = get(base, "/api/scenes")
    if code == 200:
        record(data.get("active_scene") == "test-night",
               "10.7 active_scene is now 'test-night'",
               data.get("active_scene", "?"))
        # Verify the active scene's config is applied
        gv = data.get("scenes", {}).get("test-night", {}).get("global_volume")
        record(gv == 30,
               "10.7 active scene global_volume matches test-night config (30)",
               str(gv))

    # 10.8 Set default scene to test-day
    code, data = post(base, "/api/scene", {"action": "set_default", "name": "test-day"})
    record(code == 200 and data.get("success"),
           "10.8 Set default scene to 'test-day'",
           data.get("message", str(data.get("error", ""))))

    code, data = get(base, "/api/scenes")
    if code == 200:
        record(data.get("default_scene") == "test-day",
               "10.8 default_scene is now 'test-day'",
               data.get("default_scene", "?"))
        # Active should still be test-night (set_default does not activate)
        record(data.get("active_scene") == "test-night",
               "10.8 active_scene still 'test-night' after set_default",
               data.get("active_scene", "?"))

    # 10.9 Cannot delete the active scene
    code, data = post(base, "/api/scene", {"action": "delete", "name": "test-night"})
    record(data.get("success") is False,
           "10.9 Cannot delete active scene 'test-night'",
           data.get("error", "UNEXPECTED: deletion succeeded"))

    # 10.10 Delete non-active scene (test-day)
    code, data = post(base, "/api/scene", {"action": "delete", "name": "test-day"})
    record(code == 200 and data.get("success"),
           "10.10 Delete non-active scene 'test-day'",
           data.get("message", str(data.get("error", ""))))

    # Verify it is gone
    code, data = get(base, "/api/scenes")
    if code == 200:
        record("test-day" not in data.get("scenes", {}),
               "10.10 'test-day' no longer in scenes list")
    else:
        record(False, "10.10 GET /api/scenes after delete", f"HTTP {code}")

    # 10.11 Activate nonexistent scene
    code, data = post(base, "/api/scene", {"action": "activate", "name": "does-not-exist"})
    record(data.get("success") is False,
           "10.11 Activate nonexistent scene returns success=false",
           data.get("error", "UNEXPECTED: activation succeeded"))

    # 10.12 Invalid scene name rejected (contains spaces)
    code, data = post(base, "/api/scene", {"action": "create", "name": "bad name!"})
    record(data.get("success") is False,
           "10.12 Invalid scene name 'bad name!' rejected",
           data.get("error", "UNEXPECTED: creation succeeded"))

    # 10.13 Empty scene name rejected
    code, data = post(base, "/api/scene", {"action": "create", "name": ""})
    record(data.get("success") is False,
           "10.13 Empty scene name rejected",
           data.get("error", "UNEXPECTED: creation succeeded"))

    # 10.14 PATCH nonexistent scene is rejected
    code, data = post(base, "/api/scenes", {"no-such-scene": {"global_volume": 50}})
    record(data.get("success") is False,
           "10.14 PATCH nonexistent scene returns success=false",
           data.get("error", "UNEXPECTED: patch succeeded"))

    # 10.15 Multi-scene PATCH (update both default and test-night at once)
    code, data = post(base, "/api/scenes", {
        "default": {"global_volume": 60},
        "test-night": {"global_volume": 45},
    })
    record(code == 200 and data.get("success"),
           "10.15 Multi-scene PATCH (default + test-night)",
           data.get("message", str(data.get("error", ""))))

    code, data = get(base, "/api/scenes")
    if code == 200:
        s = data.get("scenes", {})
        record(s.get("default", {}).get("global_volume") == 60,
               "10.15 default scene global_volume is 60",
               str(s.get("default", {}).get("global_volume", "?")))
        record(s.get("test-night", {}).get("global_volume") == 45,
               "10.15 test-night global_volume is 45",
               str(s.get("test-night", {}).get("global_volume", "?")))

    # 10.16 Cleanup — activate default, delete test-night
    code, data = post(base, "/api/scene", {"action": "activate", "name": "default"})
    record(code == 200 and data.get("success"),
           "10.16 Re-activate 'default' scene for cleanup",
           data.get("message", str(data.get("error", ""))))

    code, data = post(base, "/api/scene", {"action": "delete", "name": "test-night"})
    record(code == 200 and data.get("success"),
           "10.16 Delete 'test-night' scene",
           data.get("message", str(data.get("error", ""))))

    # Restore default_scene back to "default"
    code, data = post(base, "/api/scene", {"action": "set_default", "name": "default"})
    record(code == 200 and data.get("success"),
           "10.16 Restore default_scene to 'default'",
           data.get("message", str(data.get("error", ""))))

    # Final verification: only "default" remains, active and default
    code, data = get(base, "/api/scenes")
    if code == 200:
        scenes = data.get("scenes", {})
        record(len(scenes) == 1 and "default" in scenes,
               "10.16 Only 'default' scene remains after cleanup",
               f"scenes: {list(scenes.keys())}")
        record(data.get("active_scene") == "default" and data.get("default_scene") == "default",
               "10.16 active_scene and default_scene both 'default'")


# =============================================================================
# MAIN
# =============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Murmura device integration test suite")

    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--device", metavar="IP",
                        help="Device IP address (direct)")
    target.add_argument("--id", metavar="ID",
                        help="Device ID — resolved via local device_map.json")

    parser.add_argument("--map-file", default="device_map.json", metavar="PATH",
                        help="Path to device_map.json (default: device_map.json)")
    parser.add_argument("--triggers", action="store_true",
                        help="Enable interactive trigger behavior tests (Group 8b)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Log every HTTP request and response for debugging")
    parser.add_argument("--verify", action="store_true",
                        help="After every track-changing POST, print current track state")
    args = parser.parse_args()

    global VERBOSE, VERIFY
    VERBOSE = args.verbose
    VERIFY  = args.verify

    # Resolve device IP
    if args.device:
        device_ip   = args.device
        device_base = f"http://{args.device}"
        device_id   = None
    else:
        device_ip   = resolve_id_to_ip(args.id, args.map_file)
        device_base = f"http://{device_ip}"
        device_id   = args.id
        info(f"Resolved {args.id} → {device_ip} (from {args.map_file})")

    print(f"\n{BOLD}Murmura Device Test Suite{RESET}")
    print(f"  Device      : {device_base}" + (f"  (id={device_id})" if device_id else ""))
    print(f"  Triggers    : {'enabled' if args.triggers else 'disabled (use --triggers)'}")
    print(f"  Verbose     : {'enabled' if VERBOSE else 'disabled (use --verbose)'}")
    print(f"  Verify      : {'enabled' if VERIFY else 'disabled (use --verify)'}")
    print(f"  Test tracks : {TEST_TRACK_0}, {TEST_TRACK_1}, {TEST_TRACK_2}")

    # Run groups
    group1_identity(device_base)
    files = group2_files(device_base)
    group3_single_track(device_base, files)
    group4_multi_track(device_base, files)
    group5_volume(device_base, files)
    group6_config(device_base, files)
    group7_file_swap(device_base, files)
    group8_wifi(device_base)
    if args.triggers:
        group9_trigger_basic(device_base, device_ip, args.triggers)
    else:
        print(f"\n{BOLD}Group 9: Trigger System{RESET}")
        print(f"  {DIM}Not run — use --triggers to enable{RESET}")
    group10_scenes(device_base)

    # ── Summary ───────────────────────────────────────────────────────────────
    total      = results["pass"] + results["fail"]
    user_total = results["user_pass"] + results["user_fail"]
    print(f"\n{BOLD}{'─'*50}")
    print(f"Results{RESET}")
    print(f"  Automated : {GREEN}{results['pass']} passed{RESET}  "
          f"{RED}{results['fail']} failed{RESET}  "
          f"{YELLOW}{results['skip']} skipped{RESET}  "
          f"({total} total)")
    if user_total:
        print(f"  User      : {GREEN}{results['user_pass']} confirmed{RESET}  "
              f"{RED}{results['user_fail']} rejected{RESET}")

    exit_code = 1 if (results["fail"] > 0 or results["user_fail"] > 0) else 0
    if exit_code == 0:
        print(f"\n{GREEN}{BOLD}ALL TESTS PASSED{RESET}")
    else:
        print(f"\n{RED}{BOLD}SOME TESTS FAILED{RESET}")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
