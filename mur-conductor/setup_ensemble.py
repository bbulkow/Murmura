#!/usr/bin/env python3
"""
Configure a group of MURs for ensemble playback - the one-time device setup that
mur-conductor assumes has been done.

For every member device it creates the ensemble scene, points the chosen track at
a trigger in OneShot mode, makes the scene the boot default, and saves config to
the SD card. After this, each device boots SILENT and armed: it plays only when
the conductor's downbeat arrives.

Device addresses come from the conductor's own view of the fleet (its /status,
which reflects the gateway's live device connections), so there is no separate
device map to keep in sync. Pass --ip to target devices directly instead.

Usage:
  python setup_ensemble.py --group mainroom
  python setup_ensemble.py --group mainroom --dry-run
  python setup_ensemble.py --group mainroom --ip MUR-001=192.168.13.42 --ip MUR-002=192.168.13.43
  python setup_ensemble.py --group mainroom --verify      # just report current state
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
DEFAULT_CONDUCTOR = "http://127.0.0.1:4002"


def http_json(method: str, url: str, body=None, timeout=8):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
        return resp.status, (json.loads(raw) if raw.strip() else {})


def load_group(config_path: Path, group_name: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for g in cfg.get("groups", []):
        if g.get("name") == group_name:
            return g
    names = [g.get("name") for g in cfg.get("groups", [])]
    sys.exit(f"No group '{group_name}' in {config_path}. Groups: {names}")


def resolve_ips(group: dict, conductor_url: str, overrides: dict) -> dict:
    """device_id -> ip, from --ip overrides first, then the conductor's status."""
    ips = dict(overrides)
    missing = [d for d in group["expected_device_ids"] if d not in ips]
    if not missing:
        return ips
    try:
        _, status = http_json("GET", f"{conductor_url}/status")
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"Cannot reach the conductor at {conductor_url} ({e}).\n"
                 f"Start it, or pass --ip <id>=<addr> for each of: {missing}")
    for grp in status.get("groups", []):
        if grp.get("name") != group["name"]:
            continue
        for member in grp.get("members", []):
            if member.get("id") in missing and member.get("ip"):
                ips[member["id"]] = member["ip"]
    still = [d for d in group["expected_device_ids"] if d not in ips]
    if still:
        print(f"  WARNING: no address for {still} - they are not connected to the "
              f"gateway. Power them on, or pass --ip <id>=<addr>.")
    return ips


def first_file(group: dict) -> str:
    playlist = group.get("playlist") or []
    return playlist[0].get("file", "") if playlist else ""


def setup_device(device_id: str, ip: str, group: dict, dry_run: bool) -> bool:
    scene = group["scene_name"]
    track = group["track"]
    trigger = group["trigger_name"]
    file_path = first_file(group)

    steps = [
        ("create scene", "POST", f"http://{ip}/api/scene",
         {"action": "create", "name": scene}),
        # active:true keeps this patch on the trigger-mode branch of
        # scene_apply_patch, which never restarts a playing track. See the
        # README section "Why the prep patch always sends active: true".
        ("configure track", "POST", f"http://{ip}/api/scenes",
         {scene: {"tracks": [{
             "track": track,
             "mode": "trigger",
             "trigger_type": "OneShot",
             "trigger_name": trigger,
             "active": True,
             "file_path": file_path,
             "volume": 100,
         }]}}),
        ("set as boot default", "POST", f"http://{ip}/api/scene",
         {"action": "set_default", "name": scene}),
        ("activate scene", "POST", f"http://{ip}/api/scene",
         {"action": "activate", "name": scene}),
        ("save to SD card", "POST", f"http://{ip}/api/config/save", {}),
    ]

    print(f"\n  {device_id} ({ip})")
    ok = True
    for label, method, url, body in steps:
        if dry_run:
            print(f"    [dry-run] {label}: {method} {url}")
            print(f"              {json.dumps(body)}")
            continue
        try:
            status, resp = http_json(method, url, body)
            # "create" on an existing scene is an expected no-op on re-runs.
            if status >= 400 or resp.get("success") is False:
                err = resp.get("error", f"HTTP {status}")
                if label == "create scene" and "exist" in str(err).lower():
                    print(f"    ok       {label} (already existed)")
                    continue
                print(f"    FAILED   {label}: {err}")
                ok = False
            else:
                print(f"    ok       {label}")
        except (urllib.error.URLError, OSError) as e:
            print(f"    FAILED   {label}: {e}")
            ok = False
    return ok


def verify_device(device_id: str, ip: str, group: dict) -> bool:
    """Report whether the device is actually configured the way the group needs."""
    scene_name = group["scene_name"]
    track_idx = group["track"]
    try:
        _, scenes = http_json("GET", f"http://{ip}/api/scenes")
    except (urllib.error.URLError, OSError) as e:
        print(f"  {device_id:<12} ({ip}): UNREACHABLE ({e})")
        return False

    problems = []
    scene = (scenes.get("scenes") or {}).get(scene_name)
    if scene is None:
        print(f"  {device_id:<12} ({ip}): scene '{scene_name}' MISSING")
        return False
    if scenes.get("default_scene") != scene_name:
        problems.append(f"default_scene is '{scenes.get('default_scene')}' "
                        f"(boot will not land on the ensemble scene)")
    if scenes.get("active_scene") != scene_name:
        problems.append(f"active_scene is '{scenes.get('active_scene')}'")
    if scene.get("synchronized"):
        problems.append("synchronized=true (should be false; it blocks the "
                        "get_scene backstop and errors on every boot)")

    tracks = {t.get("track"): t for t in scene.get("tracks", [])}
    t = tracks.get(track_idx)
    if t is None:
        problems.append(f"track {track_idx} absent from the scene")
    else:
        if t.get("mode") != "trigger":
            problems.append(f"track {track_idx} mode is '{t.get('mode')}', not 'trigger' "
                            f"(it will start playing at boot, out of sync)")
        if t.get("trigger_type") != "OneShot":
            problems.append(f"track {track_idx} trigger_type is '{t.get('trigger_type')}', "
                            f"not 'OneShot'")
        if t.get("trigger_name") != group["trigger_name"]:
            problems.append(f"track {track_idx} trigger_name is "
                            f"'{t.get('trigger_name')}', not '{group['trigger_name']}'")
        if not t.get("active"):
            problems.append(f"track {track_idx} is not active (triggers will be ignored)")
        if not t.get("file_path"):
            problems.append(f"track {track_idx} has no file_path")

    if problems:
        print(f"  {device_id:<12} ({ip}): NEEDS FIXING")
        for p in problems:
            print(f"      - {p}")
        return False
    print(f"  {device_id:<12} ({ip}): ok  (track {track_idx} trigger/OneShot "
          f"'{group['trigger_name']}', file {t.get('file_path')})")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Configure a group of MURs for ensemble playback")
    parser.add_argument("--group", required=True, help="Group name from config.json")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                        help="Conductor config.json (default: alongside this script)")
    parser.add_argument("--conductor", default=DEFAULT_CONDUCTOR,
                        help=f"Conductor base URL (default: {DEFAULT_CONDUCTOR})")
    parser.add_argument("--ip", action="append", default=[], metavar="ID=ADDR",
                        help="Override a device address; repeatable")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the requests without sending them")
    parser.add_argument("--verify", action="store_true",
                        help="Only report each device's current configuration")
    args = parser.parse_args()

    overrides = {}
    for item in args.ip:
        if "=" not in item:
            sys.exit(f"--ip expects ID=ADDR, got '{item}'")
        dev_id, addr = item.split("=", 1)
        overrides[dev_id.strip()] = addr.strip()

    group = load_group(Path(args.config), args.group)
    ips = resolve_ips(group, args.conductor.rstrip("/"), overrides)

    print(f"\n  Group '{group['name']}': scene='{group['scene_name']}' "
          f"track={group['track']} trigger='{group['trigger_name']}'")
    print(f"  Members: {group['expected_device_ids']}")

    if args.verify:
        print("\n  Verifying:")
        results = [verify_device(d, ips[d], group)
                   for d in group["expected_device_ids"] if d in ips]
        good = sum(1 for r in results if r)
        print(f"\n  {good}/{len(results)} reachable device(s) correctly configured")
        sys.exit(0 if results and good == len(results) else 1)

    results = []
    for device_id in group["expected_device_ids"]:
        if device_id not in ips:
            print(f"\n  {device_id}: SKIPPED (no address)")
            results.append(False)
            continue
        results.append(setup_device(device_id, ips[device_id], group, args.dry_run))

    if args.dry_run:
        print("\n  Dry run - nothing was changed.")
        return

    good = sum(1 for r in results if r)
    print(f"\n  {good}/{len(results)} device(s) configured.")
    print("  Each device is now silent and armed; it will start at the "
          "conductor's next downbeat.")
    print(f"  Check with: python setup_ensemble.py --group {args.group} --verify")
    sys.exit(0 if good == len(results) else 1)


if __name__ == "__main__":
    main()
