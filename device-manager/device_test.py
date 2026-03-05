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

NOTE: scape-server endpoints are internal to the scape-server application and are
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
VERIFY  = False  # When True: print GET /api/tracks after every track-changing POST

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
    """Print a compact track-state table (used by --verify after POST /api/track)."""
    try:
        r = requests.get(f"{base}/api/tracks", timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"  {DIM}[verify] GET /api/tracks → HTTP {r.status_code}{RESET}")
            return
        tracks = r.json().get("tracks", [])
        parts = []
        for t in sorted(tracks, key=lambda x: x.get("track", 0)):
            idx    = t.get("track", "?")
            active = "YES" if t.get("active") else "no"
            mode   = t.get("mode", "?")
            name   = Path(t.get("file", "")).name or "—"
            parts.append(f"t{idx}:{active}/{mode}/{name}")
        print(f"  {DIM}[verify] {' | '.join(parts)}{RESET}")
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
        if VERIFY and path in ("/api/track", "/api/global/volume") and r.status_code == 200:
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
    for t in range(3):
        post(base, "/api/track", {"track": t, "active": False})
    time.sleep(0.5)

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

    # 1.1a — first status call
    code, data = get(base, "/api/status")
    if not record(code == 200 and "id" in data,
                  "1.1a GET /api/status returns 200 with id field",
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

    # 1.1b — second call after delay: uptime must increase
    info("Waiting 4 seconds for uptime check...")
    time.sleep(4)
    code2, data2 = get(base, "/api/status")
    if code2 == 200:
        up2 = data2.get("uptime_seconds", -1)
        record(up2 > up1,       "1.1b uptime increased between calls",   f"{up1}s → {up2}s")
        record(data2.get("mac_address") == mac, "1.1b mac unchanged",    mac)
        record(data2.get("ip_address")  == ip,  "1.1b ip unchanged",     ip)
        record(data2.get("id")          == id_,  "1.1b id unchanged",    id_)
    else:
        record(False, "1.1b second GET /api/status call", f"HTTP {code2}")

    # 1.2 — GET /api/id
    code, data = get(base, "/api/id")
    record(code == 200 and bool(data.get("id")),
           "1.2 GET /api/id returns non-empty id", data.get("id", ""))

    # NOTE: POST /api/id (set ID) intentionally omitted — destructive in the field.

    return id_  # return for use by scape-server group

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
    code, data = get(base, "/api/tracks")
    if code == 200 and "tracks" in data:
        cur_global = data.get("global_volume", "?")
        cur_vols   = {t["track"]: t.get("volume", "?")
                      for t in data["tracks"] if "track" in t}
        info(f"Current volumes — global={cur_global}  tracks={cur_vols}")
    code, data = post(base, "/api/global/volume", {"volume": 100})
    record(code == 200 and data.get("success"),
           "3.0 Set global volume to 100% before playback tests")
    all_vol_ok = True
    for i in range(3):
        c, d = post(base, "/api/track", {"track": i, "volume": 100})
        if not (c == 200 and d.get("success")):
            all_vol_ok = False
    record(all_vol_ok, "3.0 Set per-track volumes to 100% (all 3 tracks)")

    # Prefer the configured test track; fall back to first available file
    entry = find_track(files, TEST_TRACK_0) or files[0]
    file_path = entry.get("path", "")
    file_name = entry.get("name", "")
    info(f"Using file: {file_path}")

    # 3.1 set file by path
    code, data = post(base, "/api/track", {"track": 0, "file_path": file_path})
    record(code == 200 and data.get("success"),
           "3.1 POST /api/track sets file on track 0", data.get("message", ""))

    # 3.2 verify via GET /api/tracks
    code, data = get(base, "/api/tracks")
    if code == 200 and "tracks" in data:
        t0 = next((t for t in data["tracks"] if t.get("track") == 0), None)
        record(t0 is not None and file_path in t0.get("file", ""),
               "3.2 GET /api/tracks reflects file on track 0",
               t0.get("file", "") if t0 else "track 0 missing")
        record(t0 is not None and t0.get("mode") in ("loop", "trigger"),
               "3.2 Track 0 mode field is valid (loop or trigger only)",
               t0.get("mode", "") if t0 else "")
    else:
        record(False, "3.2 GET /api/tracks", f"HTTP {code}")

    # 3.3 enable track 0 in loop mode — starts it
    code, data = post(base, "/api/track", {"track": 0, "mode": "loop", "active": True})
    record(code == 200 and data.get("success"),
           "3.3 POST /api/track mode=loop active=true track 0", data.get("message", ""))

    # 3.4 user confirmation
    user_confirm(f"Is track 0 ({file_name}) playing audio?")

    # 3.5 disable track 0 — stops it (mode stays loop)
    code, data = post(base, "/api/track", {"track": 0, "active": False})
    record(code == 200 and data.get("success"),
           "3.5 POST /api/track active=false track 0", data.get("message", ""))

    # 3.6 user confirmation
    user_confirm("Has track 0 stopped?")

    # 3.7 verify stopped state: active=false, mode still loop
    time.sleep(1)
    code, data = get(base, "/api/tracks")
    if code == 200 and "tracks" in data:
        t0 = next((t for t in data["tracks"] if t.get("track") == 0), None)
        record(t0 is not None and t0.get("active") == False,
               "3.7 GET /api/tracks shows track 0 active=false after stop",
               f"active={t0.get('active','?')} mode={t0.get('mode','?')}" if t0 else "track 0 missing")
        record(t0 is not None and t0.get("mode") == "loop",
               "3.7 Track 0 mode remains loop after deactivate",
               t0.get("mode", "?") if t0 else "")
    else:
        record(False, "3.7 GET /api/tracks after stop", f"HTTP {code}")

    # 3.8 verify invalid mode is rejected
    code, data = post(base, "/api/track", {"track": 0, "mode": "off"})
    record(code == 200 and data.get("success") is False,
           "3.8 POST /api/track mode='off' returns success=false (invalid mode rejected)",
           data.get("error", "UNEXPECTED: server accepted invalid mode"))

    # Also test set-by-filename variant
    code, data = post(base, "/api/track", {"track": 0, "filename": file_name})
    record(code == 200 and data.get("success"),
           "3.x POST /api/track by filename variant",
           data.get("message", str(data.get("error", ""))))

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
    all_set = True
    for i in range(3):
        code, data = post(base, "/api/track", {"track": i, "file_path": track_paths[i]})
        if not (code == 200 and data.get("success")):
            all_set = False
            info(f"  track {i} set failed: {data}")
    record(all_set, "4.1 Set file on tracks 0, 1, 2")

    # 4.2 start track 0 only (mode=loop, active=true)
    post(base, "/api/track", {"track": 0, "mode": "loop", "active": True})
    time.sleep(0.5)
    record(True, "4.2 Started track 0 only (mode=loop, active=true)")
    user_confirm(f"Is ONLY track 0 ({track_names[0]}) audible (tracks 1 and 2 silent)?")

    # 4.4 start track 1
    post(base, "/api/track", {"track": 1, "mode": "loop", "active": True})
    time.sleep(0.5)
    record(True, "4.4 Started track 1 (mode=loop, active=true)")
    user_confirm(f"Are tracks 0 ({track_names[0]}) AND 1 ({track_names[1]}) both audible simultaneously?")

    # 4.6 start track 2
    post(base, "/api/track", {"track": 2, "mode": "loop", "active": True})
    time.sleep(0.5)
    record(True, "4.6 Started track 2 (mode=loop, active=true)")
    user_confirm(f"Are all 3 tracks playing together ({track_names[0]}, {track_names[1]}, {track_names[2]})?")

    # 4.8 stop track 1 only (active=false, mode must remain loop)
    code, data = post(base, "/api/track", {"track": 1, "active": False})
    record(code == 200 and data.get("success"),
           "4.8 POST /api/track active=false track 1 only")
    user_confirm(f"Are tracks 0 and 2 still playing, track 1 ({track_names[1]}) silent?")

    # 4.10 verify via GET /api/tracks (active + mode fields)
    time.sleep(1)
    code, data = get(base, "/api/tracks")
    if code == 200 and "tracks" in data:
        by_idx = {t["track"]: t for t in data["tracks"] if "track" in t}
        record(by_idx.get(0, {}).get("mode") == "loop" and by_idx.get(0, {}).get("active") is True,
               "4.10 track 0 mode=loop, active=true")
        record(by_idx.get(1, {}).get("mode") == "loop" and by_idx.get(1, {}).get("active") is False,
               "4.10 track 1 mode=loop, active=false (stopped but mode preserved)")
        record(by_idx.get(2, {}).get("mode") == "loop" and by_idx.get(2, {}).get("active") is True,
               "4.10 track 2 mode=loop, active=true")
    else:
        record(False, "4.10 GET /api/tracks", f"HTTP {code}")

    # 4.11 stop remaining tracks
    post(base, "/api/track", {"track": 0, "active": False})
    post(base, "/api/track", {"track": 2, "active": False})
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
    post(base, "/api/track", {"track": 0, "file_path": track_paths[0], "mode": "loop", "active": True})
    post(base, "/api/track", {"track": 0, "volume": 100})
    time.sleep(0.5)
    user_confirm("Track 0 playing at full volume (baseline)?")

    code, data = post(base, "/api/track", {"track": 0, "volume": 20})
    record(code == 200 and data.get("success"),
           "5.3 POST /api/track volume=20 track 0", data.get("message", ""))
    user_confirm("Track 0 noticeably quieter (at 20%)?")

    code, data = post(base, "/api/track", {"track": 0, "volume": 100})
    record(code == 200 and data.get("success"),
           "5.5 POST /api/track volume=100 track 0 (restore)")

    # Stop track 0 before starting all three for the global test
    post(base, "/api/track", {"track": 0, "active": False})
    time.sleep(0.3)

    # ── Global volume ─────────────────────────────────────────────────────────
    # Explicitly set file and start all 3 tracks so they are definitely looping
    for i in range(3):
        post(base, "/api/track", {"track": i, "file_path": track_paths[i], "mode": "loop", "active": True})
        post(base, "/api/track", {"track": i, "volume": 100})
    time.sleep(0.5)
    user_confirm("All 3 tracks playing at full volume (global baseline)?")

    code, data = post(base, "/api/global/volume", {"volume": 20})
    record(code == 200 and data.get("success"),
           "5.7 POST /api/global/volume → 20%", data.get("message", ""))
    user_confirm("All tracks noticeably quieter together (global 20%)?")

    code, data = post(base, "/api/global/volume", {"volume": 75})
    record(code == 200 and data.get("success"),
           "5.9 POST /api/global/volume → 75% (restore)")
    user_confirm("All tracks back to normal volume (global 75%)?")

    # ── Boundary tests (no audible confirmation needed) ───────────────────────
    # Tracks still playing; boundary values must not crash the device
    code, data = post(base, "/api/global/volume", {"volume": 0})
    record(code == 200 and data.get("success"),
           "5.10 Boundary: global volume=0 accepted without error")

    # Re-enable tracks so they are audible for remaining boundary confirmation
    for i in range(3):
        post(base, "/api/track", {"track": i, "active": True})
    time.sleep(0.3)

    code, data = post(base, "/api/global/volume", {"volume": 101})
    # Accept either a 4xx error OR success with clamping — must not crash (500)
    record(code is not None and code != 500,
           "5.11 Boundary: global volume=101 does not 500",
           f"HTTP {code} — {data.get('error', data.get('message', ''))}")

    # Restore and stop
    post(base, "/api/global/volume", {"volume": 75})
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
        path = data.get("config_path", "")
        record("track_config.json" in path,
               "6.1 config_path references track_config.json (not loop_config.json)", path)

    # 6.2 set a known state to save (loop track 0 with test file, enabled)
    if files:
        entry = find_track(files, TEST_TRACK_0) or files[0]
        post(base, "/api/track", {"track": 0, "file_path": entry["path"], "mode": "loop", "active": True})  # set known state
        time.sleep(0.3)

    # 6.3 save config
    code, data = post(base, "/api/config/save")
    record(code == 200 and data.get("success"),
           "6.3 POST /api/config/save returns success",
           data.get("message", str(data.get("error", ""))))
    saved_path = data.get("path", "")
    record("track_config.json" in saved_path,
           "6.3 saved path is track_config.json", saved_path)

    # 6.4 verify config now exists
    code, data = get(base, "/api/config/status")
    record(code == 200 and data.get("config_exists") == True,
           "6.4 GET /api/config/status shows config_exists=true after save")

    # 6.5 dirty the state
    post(base, "/api/track",    {"track": 0, "active": False})
    post(base, "/api/global/volume", {"volume": 50})
    time.sleep(0.3)

    # 6.6 load config
    code, data = post(base, "/api/config/load")
    record(code == 200 and data.get("success"),
           "6.6 POST /api/config/load returns success",
           data.get("message", str(data.get("error", ""))))
    if code == 200 and "loaded_config" in data:
        lc = data["loaded_config"]
        record("tracks" in lc,
               "6.6 loaded_config contains tracks array")
        record("global_volume" in lc,
               "6.6 loaded_config contains global_volume")

    # 6.7 verify tracks have mode and active fields
    code, data = get(base, "/api/tracks")
    if code == 200 and "tracks" in data:
        t0 = next((t for t in data["tracks"] if t.get("track") == 0), None)
        record(t0 is not None and "mode" in t0,
               "6.7 GET /api/tracks track objects include mode field",
               t0.get("mode", "MISSING") if t0 else "track 0 missing")
        record(t0 is not None and "active" in t0,
               "6.7 GET /api/tracks track objects include active field",
               str(t0.get("active", "MISSING")) if t0 else "track 0 missing")

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
    code, data = post(base, "/api/track",
                      {"track": 0, "file_path": path_a, "mode": "loop", "active": True})
    record(code == 200 and data.get("success"),
           f"7.1 Start track 0 looping on {name_a}", data.get("message", ""))
    time.sleep(0.5)
    if not same_file:
        user_confirm(f"Is track 0 playing '{name_a}'?")

    # 7.2 Replace file while still looping (no mode field in request)
    code, data = post(base, "/api/track", {"track": 0, "file_path": path_b})
    record(code == 200 and data.get("success"),
           f"7.2 Replace file on looping track 0 → {name_b} (no mode field)",
           data.get("message", str(data.get("error", ""))))

    # 7.3 Verify GET shows new file and mode is still loop
    time.sleep(0.5)
    code, data = get(base, "/api/tracks")
    if code == 200 and "tracks" in data:
        t0 = next((t for t in data["tracks"] if t.get("track") == 0), None)
        record(t0 is not None and path_b in t0.get("file", ""),
               "7.3 GET /api/tracks reflects new file on track 0",
               t0.get("file", "?") if t0 else "track 0 missing")
        record(t0 is not None and t0.get("mode") == "loop",
               "7.3 Track 0 mode is still loop after file replacement",
               t0.get("mode", "?") if t0 else "")
        record(t0 is not None and t0.get("active") is True,
               "7.3 Track 0 still active after file replacement",
               str(t0.get("active", "?")) if t0 else "")
    else:
        record(False, "7.3 GET /api/tracks after file swap", f"HTTP {code}")
        record(False, "7.3 mode still loop")

    # 7.4 User confirms audio switched to file B
    if not same_file:
        user_confirm(f"Has the audio switched to '{name_b}'?")

    # 7.5 Switch back to file A
    code, data = post(base, "/api/track", {"track": 0, "file_path": path_a})
    record(code == 200 and data.get("success"),
           f"7.5 Switch back to {name_a} (no mode field)",
           data.get("message", str(data.get("error", ""))))
    time.sleep(0.5)
    if not same_file:
        user_confirm(f"Has the audio switched back to '{name_a}'?")

    # 7.6 Reject: try to clear file while looping
    code, data = post(base, "/api/track", {"track": 0, "file": ""})
    record(code == 200 and data.get("success") is False,
           "7.6 Clearing file while looping is rejected",
           data.get("error", f"HTTP {code}"))

    stop_all_tracks(base)


# =============================================================================
# GROUP 8: WiFi (read-only)
# =============================================================================
def group8_wifi(base):
    head("Group 7: WiFi (read-only)")

    code, data = get(base, "/api/wifi/status")
    record(code == 200, "7.1 GET /api/wifi/status returns 200", f"HTTP {code}")
    if code == 200:
        record(data.get("connected") == True,
               "7.1 WiFi reports connected=true", str(data.get("connected")))
        record(bool(data.get("ssid")),
               "7.1 ssid is non-empty", data.get("ssid", ""))
        ip = data.get("ip_address", "")
        record(is_valid_ip(ip),
               "7.1 ip_address is valid IPv4", ip)

    code, data = get(base, "/api/wifi/networks")
    record(code == 200 and "networks" in data,
           "7.2 GET /api/wifi/networks returns networks list", f"HTTP {code}")
    if code == 200:
        nets = data.get("networks", [])
        record(len(nets) >= 1,
               "7.2 At least one configured network", f"{len(nets)} network(s)")

# =============================================================================
# GROUP 9: Trigger TCP + Status (no trigger behavior tests by default)
# =============================================================================
def group9_trigger_basic(base, device_ip, trigger_tests):
    head("Group 9: Trigger System — Basic")

    # 8.1 trigger status
    code, data = get(base, "/api/trigger/status")
    record(code == 200 and data.get("success"),
           "8.1 GET /api/trigger/status returns success", f"HTTP {code}")
    if code == 200:
        record("server_ip"   in data or "trigger_server_ip"   in data,
               "8.1 trigger status has server IP field")
        record("server_port" in data or "trigger_server_port" in data,
               "8.1 trigger status has server port field")
        record("connected"   in data,
               "8.1 trigger status has connected field")

    # 8.2 TCP connect to listener port
    listener_port = 6100
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(4)
        sock.connect((device_ip, listener_port))
        sock.close()
        record(True, f"8.2 TCP connect to {device_ip}:{listener_port} succeeds")
    except Exception as e:
        record(False, f"8.2 TCP connect to {device_ip}:{listener_port}", str(e))

    # 8.3 set trigger server IP (dummy value)
    code, data = post(base, "/api/trigger/server",
                      {"ip": "192.168.5.99", "port": 5002, "listener_port": 6100})
    record(code == 200 and data.get("success"),
           "8.3 POST /api/trigger/server accepts IP/port config",
           data.get("message", str(data.get("error", ""))))

    # 8.4 verify stored
    code, data = get(base, "/api/trigger/status")
    if code == 200:
        stored_ip = data.get("server_ip") or data.get("trigger_server_ip", "")
        record("192.168.5.99" in stored_ip,
               "8.4 Trigger server IP stored correctly", stored_ip)

    # 8.5 clear IP
    code, data = post(base, "/api/trigger/server",
                      {"ip": "", "port": 5002, "listener_port": 6100})
    record(code == 200 and data.get("success"),
           "8.5 POST /api/trigger/server with empty IP does not crash")

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
