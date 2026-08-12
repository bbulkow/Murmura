# Murmura HTTP API

## Overview

The ESP32 Murmura device provides a JSON-based HTTP API for remote control of audio tracks. Once connected to WiFi, the device exposes a web server on port 80.

All playback configuration is organized into **scenes**. A scene is a named configuration containing a global volume and settings for all 3 tracks. Example scenes: "day", "night", "show". One scene is active (playing) at a time, and one can be set as the default for boot.

Each track within a scene has:
- **mode**: `"loop"` (continuously repeats) or `"trigger"` (plays when a trigger event arrives)
- **active**: whether the track is enabled — this is user intent, not playback state (see [Active vs Playing](#active-vs-playing))
- **file_path**: the audio file assigned to the track
- **volume**: per-track volume (0-100%)
- **trigger_name**: name of the trigger event to listen for (empty string = no trigger)
- **trigger_type**: `"On/Off"` (start on "On", stop on "Off") or `"OneShot"` (start on event, plays to completion, ignore subsequent). Mirrors upstream Haven Trigger Server type names.

Each scene also has:
- **global_volume** (master volume, 0-100%) that scales all tracks via the hardware codec
- **button_trigger** (optional): a trigger name (typically On/Off type) that activates this scene when an "On" event arrives. One button trigger per scene.
- **synchronized** (optional, default `false`): when `true`, the scene can only be activated via cross-device-synchronized paths. Direct admin activation (`POST /api/scene` `action=activate`) returns 409, and the gateway's `get_scene` reliability poll refuses to enter the scene. Activation requires a `SceneChange` trigger event. See [SYNC_DESIGN.md](SYNC_DESIGN.md).

The device connects outbound to a Mur Gateway to receive trigger events:
- **mur_gateway_ip**: IP address of the Mur Gateway (empty = disabled)
- **mur_gateway_port**: port of the Mur Gateway (default 4000)
- The device connects to the gateway, announces its ID, and subscribes to triggers

### Active vs Playing

The `active` field represents **user intent** — whether the track is enabled. It does **not** indicate whether audio is currently playing:

- **Loop mode**: setting `active: true` both enables the track and starts playback. The track is active *and* playing.
- **Trigger mode**: setting `active: true` enables (arms) the track to respond to trigger events, but audio does **not** start until a trigger event arrives. The track is active but *not yet playing*.
- In both modes, `active: false` means the track is disabled and will not play.

To determine whether a track is actually producing audio, check the `playing` field in the GET /api/scenes response (only present on the active scene's tracks).

---

## API Endpoints

### Configuration Persistence

Scenes are stored in-memory during runtime. Use these endpoints to persist to/from SD card.

#### Get Configuration Status

**GET** `/api/config/status`

```json
{
  "config_exists": true,
  "config_path": "/sdcard/scenes.json",
  "scene_count": 3,
  "default_scene": "day",
  "active_scene": "day"
}
```

#### Save Configuration

**POST** `/api/config/save`

Saves all scenes to `/sdcard/scenes.json` and gateway config to `/sdcard/track_config.json`.

**Response:** `{"success": true, "message": "Configuration saved successfully", "path": "/sdcard/scenes.json"}`

#### Load Configuration

**POST** `/api/config/load`

Loads scenes from SD card and activates the default scene.

**Response:** `{"success": true, "message": "Configuration loaded and applied", "active_scene": "day"}`

#### Delete Configuration

**DELETE** `/api/config/delete`

Deletes saved configuration files. Device creates a default scene on next boot.

**Response:** `{"success": true, "message": "Configuration deleted successfully"}`

---

### Device Configuration

#### Get Device Configuration and Status

**GET** `/api/device`

Returns device identity, network status, Mur Gateway config, and WiFi info in one response.

**Response:**
```json
{
  "id": "MURMURA-001",
  "mac_address": "AA:BB:CC:DD:EE:FF",
  "ip_address": "192.168.1.100",
  "firmware_version": "3.5",
  "uptime_seconds": 3600,
  "mur_gateway_ip": "192.168.1.10",
  "mur_gateway_port": 4000,
  "scene_trigger_name": "SceneChange",
  "device_volume": 100,
  "late_policy": "play",
  "playback_offset_us": 0,
  "wifi": {
    "connected": true,
    "ssid": "MyNetwork",
    "rssi": -65,
    "signal_strength": 75,
    "networks": [
      { "index": 0, "ssid": "HomeNetwork", "has_password": true, "available": true, "rssi": -65 }
    ]
  }
}
```

**Fields:**
- `id`: device ID (persisted to `/sdcard/unit_id.txt`)
- `mac_address`, `ip_address`, `firmware_version`, `uptime_seconds`: read-only device info
- `mur_gateway_ip`: IP of the Mur Gateway; empty string if not configured
- `mur_gateway_port`: Mur Gateway TCP port (default 4000)
- `scene_trigger_name`: trigger name for discrete scene changes — when an event with this name arrives, the `value` is used as the scene name to activate. If the value doesn't match any scene, the default scene is activated. Default is `"SceneChange"` (matches the system-wide constant in `mur-scene-server` and `mur_gateway`); empty string disables.
- `device_volume`: per-device master attenuator, 0–100. Composes multiplicatively with the active scene's `global_volume` and each track's `volume` — effective output = `device_volume × scene.global_volume × track.volume`. Persisted to `/sdcard/track_config.json`, survives scene changes and reboot. Default 100.
  - Note the terms have different slopes: a track `volume` of 50 is −6 dB (20·log10), while `global_volume` or `device_volume` of 50 is about −12 dB (the codec's analog taper). A "50" in one place is not a "50" in another.
  - A conducted ensemble drives the `track` term per playlist entry, via a `volume` field on the trigger event (see [MUR_PROTOCOL.md](MUR_PROTOCOL.md)). That change is **runtime only**: `GET /api/device` reports the live level, while `GET /api/scenes` keeps reporting the scene's stored `volume`. The divergence is expected, not a bug.
- `late_policy`: per-device policy for scheduled events that arrive past their TSF deadline. `"play"` (default) fires immediately with a `late` warning; `"drop"` discards with a `late` warning. Persisted to `/sdcard/track_config.json`. See [SYNC_DESIGN.md](SYNC_DESIGN.md).
- `playback_offset_us`: signed per-device offset in microseconds applied to every scheduled event's `target_tsf_us` before firing. Positive = fire later (e.g. compensate for a closer speaker), negative = fire earlier (compensate for a more distant one). Range int32 (~±35 min); typical values are tens of milliseconds (1 m of air path ≈ 2.9 ms). Default 0. Persisted to `/sdcard/track_config.json`. See [SYNC_DESIGN.md](SYNC_DESIGN.md).
- `wifi.connected`: whether WiFi is connected
- `wifi.ssid`, `wifi.rssi`, `wifi.signal_strength`: current connection info (only present when connected)
- `wifi.networks`: list of configured WiFi networks

#### Update Device Configuration

**POST** `/api/device`

Patch-style update of settable device fields. All fields are optional — only the fields present in the request are applied.

**Request Body:**
```json
{
  "id": "MURMURA-STAGE-01",
  "mur_gateway_ip": "192.168.1.10",
  "mur_gateway_port": 4000,
  "scene_trigger_name": "SceneChange",
  "device_volume": 80,
  "late_policy": "drop",
  "playback_offset_us": -15000
}
```

**Response (success):**
```json
{
  "success": true,
  "id": "MURMURA-STAGE-01",
  "mur_gateway_ip": "192.168.1.10",
  "mur_gateway_port": 4000,
  "scene_trigger_name": "SceneChange",
  "device_volume": 80,
  "late_policy": "drop",
  "playback_offset_us": -15000
}
```

A `device_volume` change returns HTTP 503 if the internal audio control queue is full. A `late_policy` other than `"play"` or `"drop"` returns 400 with `"late_policy must be 'play' or 'drop'"`. A `playback_offset_us` outside int32 range returns 400 with `"playback_offset_us out of int32 range"`.

**Response (error):**
```json
{
  "success": false,
  "error": "No valid fields to update"
}
```

---

### Scenes

All playback configuration lives inside **named scenes**. A scene contains a global volume and configuration for all 3 tracks. Device settings (wifi, gateway, device ID) are **not** part of scenes.

A scene may be marked `synchronized: true` to enforce that it can only be activated via the synchronized cross-device trigger path. See [SYNC_DESIGN.md](SYNC_DESIGN.md) for the rationale, the gating rules, and the handling of each activation path.

#### Get All Scenes

**GET** `/api/scenes`

Returns all scene configurations plus metadata.

**Response:**
```json
{
  "default_scene": "day",
  "active_scene": "day",
  "scenes": {
    "day": {
      "global_volume": 75,
      "button_trigger": "ButtonA",
      "tracks": [
        {"track": 0, "mode": "loop", "active": true, "file_path": "/sdcard/birds.wav", "volume": 80, "trigger_name": "", "trigger_type": "On/Off", "playing": true},
        {"track": 1, "mode": "loop", "active": true, "file_path": "/sdcard/wind.wav", "volume": 60, "trigger_name": "", "trigger_type": "On/Off", "playing": true},
        {"track": 2, "mode": "trigger", "active": false, "file_path": "", "volume": 100, "trigger_name": "", "trigger_type": "On/Off"}
      ]
    },
    "night": {
      "global_volume": 40,
      "tracks": [
        {"track": 0, "mode": "loop", "active": true, "file_path": "/sdcard/crickets.wav", "volume": 100, "trigger_name": "", "trigger_type": "On/Off"},
        {"track": 1, "mode": "loop", "active": false, "file_path": "", "volume": 100, "trigger_name": "", "trigger_type": "On/Off"},
        {"track": 2, "mode": "loop", "active": false, "file_path": "", "volume": 100, "trigger_name": "", "trigger_type": "On/Off"}
      ]
    }
  }
}
```

- `default_scene`: the scene activated on boot (empty string = none)
- `active_scene`: the scene currently applied to the hardware
- `playing`: only present on the active scene's tracks (runtime state)
- Per-track fields: same as before (`mode`, `active`, `file_path`, `volume`, `trigger_name`, `trigger_type`)

#### Update Scene Configuration

**POST** `/api/scenes`

Patch-style update. Body keys are scene names, values are partial scene configs. Only stated fields change. Unstated fields, tracks, and scenes are untouched.

**Atomic**: validates all changes before applying any. If any scene name doesn't exist or any value is invalid, the entire request is rejected.

**If the updated scene is the active scene**, changes are applied to the hardware immediately.

**Examples:**

Update global volume of one scene:
```json
{"day": {"global_volume": 50}}
```

Update a track within a scene:
```json
{"night": {"tracks": [{"track": 0, "volume": 80, "file_path": "/sdcard/newfile.wav"}]}}
```

Update multiple scenes at once:
```json
{"day": {"global_volume": 75}, "night": {"global_volume": 40}}
```

**Response:**
```json
{"success": true, "message": "Scenes updated"}
```

**Error response (nothing changed):**
```json
{"success": false, "error": "Scene 'foo' not found"}
```

#### Scene Management Actions

**POST** `/api/scene`

A single endpoint for scene lifecycle operations, dispatched by the `action` field.

**Create a scene:**
```json
{"action": "create", "name": "show"}
```
Creates by cloning the active scene's config. Optionally include initial values to override:
```json
{"action": "create", "name": "show", "global_volume": 100, "tracks": [...]}
```

**Delete a scene:**
```json
{"action": "delete", "name": "show"}
```
Cannot delete the active scene.

**Activate a scene:**
```json
{"action": "activate", "name": "night"}
```
Applies the scene's config to the hardware immediately. Returns **HTTP 409 Conflict** if the scene has `synchronized: true` — those scenes can only be activated via the cross-device synchronized path (a `SceneChange` trigger event). See [SYNC_DESIGN.md](SYNC_DESIGN.md).

**Set default boot scene:**
```json
{"action": "set_default", "name": "day"}
```
Empty name clears default.

**Response:**
```json
{"success": true, "message": "Scene 'night' activated"}
```

**Scene name rules:** 1-31 characters, alphanumeric plus hyphen and underscore.

---

### WiFi Management

Stored networks live in **NVS**, not on the SD card — they survive reboots
without `POST /api/config/save`. Up to 10 networks (`WIFI_MAX_NETWORKS`).
The device's own `/settings` page provides a UI for both endpoints below.

**Neither endpoint touches the live connection.** Both only edit the stored
list; the device stays on whatever AP it is currently associated with. The
stored list is consulted the next time the device needs to connect — on reboot,
or after losing its current AP — and the background task takes whichever saved
network it finds. Adding an entry therefore does *not* mean the device will
switch to it, and rebooting only forces the selection to run again, not to
choose the newest entry.

Two consequences worth designing around:

- There is no "connect to this network now" operation. To move a device
  deliberately, add the new network, remove the ones you do not want it to
  choose, then reboot (`POST /api/system/reboot`).
- Removing the network the device is using does not disconnect it, but the
  device will not rejoin that network after its next reboot.

An earlier firmware called `wifi_manager_reconnect()` from `/api/wifi/add`,
which dropped the association on every save. That was removed: saving
credentials should not knock a working device off the air, and it could leave
the device unreachable if the new entry was mistyped.

#### Add WiFi Network

**POST** `/api/wifi/add`

**Request Body:**
```json
{ "ssid": "NetworkName", "password": "NetworkPassword" }
```

An empty `password` is allowed (open network). Re-adding an existing SSID
updates its password rather than creating a duplicate.

**Response (success):**
```json
{
  "success": true,
  "message": "Network added successfully",
  "ssid": "NetworkName"
}
```

**Errors** (HTTP 200 with `success: false`): `"Missing or invalid SSID"`,
`"Missing or invalid password"`, `"Maximum number of networks reached"`.

#### Remove WiFi Network

**POST** `/api/wifi/remove`

**Request Body:**
```json
{ "ssid": "NetworkName" }
```

**Response (success):**
```json
{
  "success": true,
  "message": "Network removed successfully",
  "ssid": "NetworkName"
}
```

**Errors** (HTTP 200 with `success: false`): `"Missing or invalid SSID"`,
`"Network not found"`.

---

### File Management

#### List Audio Files

**GET** `/api/files`

Lists all audio files on the SD card root directory.

**Response:**
```json
{
  "files": [
    {
      "index": 0,
      "name": "track1.wav",
      "type": "wav",
      "path": "/sdcard/track1.wav",
      "size": 1048576
    }
  ],
  "count": 1
}
```

#### Upload Audio File

**POST** `/api/upload?filename=track.wav`

- Content-Type: `application/octet-stream`
- Body: raw binary file data

**Response:**
```json
{
  "success": true,
  "filename": "track.wav",
  "path": "/sdcard/track.wav",
  "size": 1048576,
  "message": "File uploaded successfully"
}
```

#### Download Audio File

**GET** `/api/file/download?filename=track.wav`

Streams a file from the SD card root. The read-back counterpart to
`/api/upload` — used to copy audio from one device to another (every member of
an ensemble group must hold the same files) and to read a WAV header to derive a
file's duration.

- `filename` must be a bare filename; path separators are rejected.
- Response is `application/octet-stream`, sent with chunked transfer encoding
  (no `Content-Length`). Use `/api/files` to learn the size beforehand.
- Only reads the first bytes if that is all you need — a ranged read is not
  supported, but a client may close the connection early.

**Errors:** `400` missing/invalid filename, `404` file not found,
`500` cannot open or allocate. Errors are always JSON-free plain httpd errors
and are only ever sent *before* streaming begins.

#### Delete Audio File

**DELETE** `/api/file/delete`

**Request Body:**
```json
{ "filename": "track.wav" }
```

**Response:**
```json
{
  "success": true,
  "filename": "track.wav",
  "message": "File deleted successfully"
}
```

---

### System

#### Reboot

**POST** `/api/system/reboot`

**Request Body (optional):**
```json
{ "delay_ms": 1000 }
```

Defaults to 1000ms delay. Clamped to 100–10000ms.

**Response:**
```json
{
  "success": true,
  "message": "System will reboot",
  "delay_ms": 1000
}
```

---

## Examples

### curl

```bash
# Get all scenes
curl http://192.168.1.100/api/scenes

# Get device info (identity, gateway, wifi)
curl http://192.168.1.100/api/device

# Create a new scene
curl -X POST http://192.168.1.100/api/scene \
  -H "Content-Type: application/json" \
  -d '{"action": "create", "name": "night"}'

# Update a track in a scene
curl -X POST http://192.168.1.100/api/scenes \
  -H "Content-Type: application/json" \
  -d '{"day": {"tracks": [{"track": 0, "mode": "loop", "active": true, "file_path": "ambient.wav", "volume": 80}]}}'

# Set global volume for a scene
curl -X POST http://192.168.1.100/api/scenes \
  -H "Content-Type: application/json" \
  -d '{"night": {"global_volume": 40}}'

# Activate a scene
curl -X POST http://192.168.1.100/api/scene \
  -H "Content-Type: application/json" \
  -d '{"action": "activate", "name": "night"}'

# Set default boot scene
curl -X POST http://192.168.1.100/api/scene \
  -H "Content-Type: application/json" \
  -d '{"action": "set_default", "name": "day"}'

# Delete a scene
curl -X POST http://192.168.1.100/api/scene \
  -H "Content-Type: application/json" \
  -d '{"action": "delete", "name": "night"}'

# Save to SD card
curl -X POST http://192.168.1.100/api/config/save

# Set device ID and mur gateway
curl -X POST http://192.168.1.100/api/device \
  -H "Content-Type: application/json" \
  -d '{"id": "MURMURA-STAGE-01", "mur_gateway_ip": "192.168.1.10"}'
```

### Python

```python
import requests

base_url = "http://192.168.1.100"

# Get all scenes
resp = requests.get(f"{base_url}/api/scenes")
print(resp.json())

# Create a new scene
resp = requests.post(f"{base_url}/api/scene", json={"action": "create", "name": "night"})

# Configure track 0 in the "day" scene
resp = requests.post(f"{base_url}/api/scenes", json={
    "day": {
        "tracks": [{"track": 0, "mode": "loop", "active": True, "file_path": "ambient.wav", "volume": 80}]
    }
})

# Activate "night" scene
resp = requests.post(f"{base_url}/api/scene", json={"action": "activate", "name": "night"})

# Save to SD card
resp = requests.post(f"{base_url}/api/config/save")
```

---

## Error Handling

Most errors return HTTP 200 with `"success": false` in the JSON body. Some cases use standard HTTP status codes:

```json
{ "success": false, "error": "Track index out of range" }
```

HTTP status codes:
- `200 OK` — request processed (check `success` field for errors)
- `400 Bad Request` — missing or invalid request body, or invalid parameters (e.g. no file configured when enabling a track)
- `500 Internal Server Error` — server-side failure (e.g. file I/O error)
- `503 Service Unavailable` — audio control queue full (command dropped, retry later)

---

## Notes

- CORS headers included for browser access
- Server runs on port 80
- Audio files must be WAV or MP3 format on the SD card at `/sdcard/`
- Scene configuration is persisted to `/sdcard/scenes.json`
- Gateway configuration (mur_gateway_ip/port) and scene trigger name are persisted to `/sdcard/track_config.json`
- Per-scene button triggers are persisted as part of each scene in `/sdcard/scenes.json`
- Device ID is persisted to `/sdcard/unit_id.txt`
