# Murmura HTTP API

## Overview

The ESP32 Murmura device provides a JSON-based HTTP API for remote control of audio tracks. Once connected to WiFi, the device exposes a web server on port 80.

Each device has three tracks (0, 1, 2). Each track has:
- **mode**: `"loop"` (continuously repeats) or `"trigger"` (plays when a trigger event arrives)
- **active**: whether the track is enabled — this is user intent, not playback state (see [Active vs Playing](#active-vs-playing))
- **file**: the audio file assigned to the track
- **volume**: per-track volume (0–100%)
- **trigger_name**: name of the trigger event to listen for (empty string = no trigger)
- **trigger_mode**: `"momentary"` (start on keyDown "On", stop on keyUp "Off") or `"oneshot"` (start on keyDown, plays to completion, ignore keyUp)

There is also a global/master volume that scales all tracks.

The device connects outbound to a Mur Gateway to receive trigger events:
- **mur_gateway_ip**: IP address of the Mur Gateway (empty = disabled)
- **mur_gateway_port**: port of the Mur Gateway (default 4000)
- The device connects to the gateway, announces its ID, and subscribes to triggers

### Active vs Playing

The `active` field represents **user intent** — whether the track is enabled. It does **not** indicate whether audio is currently playing:

- **Loop mode**: setting `active: true` both enables the track and starts playback. The track is active *and* playing.
- **Trigger mode**: setting `active: true` enables (arms) the track to respond to trigger events, but audio does **not** start until a trigger event arrives. The track is active but *not yet playing*.
- In both modes, `active: false` means the track is disabled and will not play.

To determine whether a track is actually producing audio, check the pipeline state via `is_track_playing()` (firmware only). The API does not currently expose a separate "playing" field.

---

## API Endpoints

### Configuration Persistence

#### Get Configuration Status

**GET** `/api/config/status`

Returns whether a saved config exists on SD card, the current running config, and (when a save file exists) the saved config and whether they match.

**Response (config exists):**
```json
{
  "config_exists": true,
  "config_path": "/sdcard/track_config.json",
  "current_config": {
    "global_volume": 75,
    "tracks": [
      { "track": 0, "mode": "loop", "active": true, "file": "/sdcard/ambient.wav", "volume": 80 },
      { "track": 1, "mode": "trigger", "active": false, "file": "", "volume": 100 },
      { "track": 2, "mode": "loop", "active": false, "file": "", "volume": 100 }
    ]
  },
  "saved_config": {
    "global_volume": 75,
    "tracks": [
      { "track": 0, "mode": "loop", "active": true, "file": "/sdcard/ambient.wav", "volume": 80 },
      { "track": 1, "mode": "trigger", "active": false, "file": "", "volume": 100 },
      { "track": 2, "mode": "loop", "active": false, "file": "", "volume": 100 }
    ]
  },
  "configs_match": true
}
```

**Response (no saved config):**
```json
{
  "config_exists": false,
  "config_path": "/sdcard/track_config.json"
}
```

#### Save Configuration

**POST** `/api/config/save`

Saves current track configuration to `/sdcard/track_config.json`. Loaded automatically on next boot.

**Response:**
```json
{
  "success": true,
  "message": "Configuration saved successfully",
  "path": "/sdcard/track_config.json"
}
```

#### Load Configuration

**POST** `/api/config/load`

Loads and applies saved configuration from SD card.

**Response (success):**
```json
{
  "success": true,
  "message": "Configuration loaded and applied successfully",
  "loaded_config": {
    "global_volume": 75,
    "tracks": [
      { "track": 0, "file": "/sdcard/ambient.wav", "volume": 80 },
      { "track": 1, "file": "", "volume": 100 },
      { "track": 2, "file": "", "volume": 100 }
    ]
  }
}
```

#### Delete Configuration

**DELETE** `/api/config/delete`

Deletes saved configuration. Device uses defaults on next boot.

**Response:**
```json
{
  "success": true,
  "message": "Configuration deleted successfully"
}
```

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
  "firmware_version": "2.1",
  "uptime_seconds": 3600,
  "mur_gateway_ip": "192.168.1.10",
  "mur_gateway_port": 4000,
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
  "mur_gateway_port": 4000
}
```

**Response (success):**
```json
{
  "success": true,
  "id": "MURMURA-STAGE-01",
  "mur_gateway_ip": "192.168.1.10",
  "mur_gateway_port": 4000
}
```

**Response (error):**
```json
{
  "success": false,
  "error": "No valid fields to update"
}
```

---

### Track Control

#### Get Track Status

**GET** `/api/tracks`

Returns the current state of all three tracks.

**Response:**
```json
{
  "tracks": [
    {
      "track": 0,
      "mode": "loop",
      "active": true,
      "file": "/sdcard/ambient.wav",
      "volume": 80,
      "trigger_name": "",
      "trigger_mode": "momentary"
    },
    {
      "track": 1,
      "mode": "trigger",
      "active": true,
      "file": "/sdcard/sting.wav",
      "volume": 100,
      "trigger_name": "RedButton.Button_1",
      "trigger_mode": "oneshot"
    },
    {
      "track": 2,
      "mode": "loop",
      "active": false,
      "file": "",
      "volume": 100,
      "trigger_name": "",
      "trigger_mode": "momentary"
    }
  ],
  "global_volume": 75
}
```

**Fields:**
- `mode`: `"loop"` or `"trigger"`
- `active`: whether the track is enabled (see [Active vs Playing](#active-vs-playing))
- `file`: full SD card path, or empty string if none assigned
- `volume`: per-track volume 0–100%
- `trigger_name`: trigger event name to match; empty string = no trigger assigned
- `trigger_mode`: `"momentary"` or `"oneshot"`

#### Set Track Configuration

**POST** `/api/track`

Updates configuration for a single track. All fields except `track` are optional. Only the fields present in the request are applied.

**Request Body:**
```json
{
  "track": 0,
  "mode": "trigger",
  "active": false,
  "file": "sting.wav",
  "volume": 100,
  "trigger_name": "RedButton.Button_1",
  "trigger_mode": "oneshot"
}
```

**Fields:**
- `track` *(required)*: 0, 1, or 2
- `mode` *(optional)*: `"loop"` or `"trigger"`
- `active` *(optional)*: `true` to enable, `false` to disable (see [Active vs Playing](#active-vs-playing))
- `file` *(optional)*: filename (e.g. `"ambient.wav"`) or full path (e.g. `"/sdcard/ambient.wav"`). The device also accepts `file_path` (full path) and `filename` (bare name) as aliases.
- `volume` *(optional)*: 0–100 (clamped)
- `trigger_name` *(optional)*: name of trigger event to bind (e.g. `"RedButton.Button_1"`); empty string clears
- `trigger_mode` *(optional)*: `"momentary"` or `"oneshot"`

**Behavior:**
- **Loop mode, `active: true`**: enables the track and starts playback immediately. Requires a file to be configured.
- **Trigger mode, `active: true`**: enables (arms) the track to respond to trigger events. Audio does not start until a matching trigger arrives.
- **`active: false`** (either mode): disables the track and stops any audio.
- Changing `file` while the track is playing restarts playback with the new file.
- Changing `volume`, `mode`, `trigger_name`, or `trigger_mode` alone does not start/stop the track.

**Response (success):**
```json
{
  "success": true,
  "track": 1,
  "mode": "trigger",
  "active": false,
  "file": "/sdcard/sting.wav",
  "volume": 100,
  "trigger_name": "RedButton.Button_1",
  "trigger_mode": "oneshot"
}
```

**Response (error):**
```json
{
  "success": false,
  "error": "No file configured for this track"
}
```

#### Set Global Volume

**POST** `/api/global/volume`

Adjusts the master volume (affects all tracks via hardware codec).

**Request Body:**
```json
{
  "volume": 75
}
```

**Response:**
```json
{
  "success": true,
  "volume": 75
}
```

---

### WiFi Management

#### Add WiFi Network

**POST** `/api/wifi/add`

**Request Body:**
```json
{ "ssid": "NetworkName", "password": "NetworkPassword" }
```

**Response (success):**
```json
{
  "success": true,
  "message": "Network added successfully",
  "ssid": "NetworkName"
}
```

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
# Get config status (check if saved config exists)
curl http://192.168.1.100/api/config/status

# Get device info (identity, gateway, wifi)
curl http://192.168.1.100/api/device

# Get all track state
curl http://192.168.1.100/api/tracks

# Enable track 0 as a loop with a file at 80% volume
curl -X POST http://192.168.1.100/api/track \
  -H "Content-Type: application/json" \
  -d '{"track": 0, "mode": "loop", "active": true, "file": "ambient.wav", "volume": 80}'

# Disable track 0
curl -X POST http://192.168.1.100/api/track \
  -H "Content-Type: application/json" \
  -d '{"track": 0, "active": false}'

# Change volume on track 1 without affecting active state
curl -X POST http://192.168.1.100/api/track \
  -H "Content-Type: application/json" \
  -d '{"track": 1, "volume": 50}'

# Arm track 2 for trigger events
curl -X POST http://192.168.1.100/api/track \
  -H "Content-Type: application/json" \
  -d '{"track": 2, "mode": "trigger", "file": "sting.wav", "active": true}'

# Set global volume
curl -X POST http://192.168.1.100/api/global/volume \
  -H "Content-Type: application/json" \
  -d '{"volume": 75}'

# Save current configuration
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

# Get config status
resp = requests.get(f"{base_url}/api/config/status")
print(resp.json())

# Get device info (identity, gateway, wifi)
resp = requests.get(f"{base_url}/api/device")
print(resp.json())

# Get all track state
resp = requests.get(f"{base_url}/api/tracks")
print(resp.json())

# Enable track 0 as a looping ambient sound
resp = requests.post(f"{base_url}/api/track", json={
    "track": 0,
    "mode": "loop",
    "active": True,
    "file": "ambient.wav",
    "volume": 80
})
print(resp.json())

# Disable track 0
resp = requests.post(f"{base_url}/api/track", json={"track": 0, "active": False})

# Arm track 1 for trigger events
resp = requests.post(f"{base_url}/api/track", json={
    "track": 1,
    "mode": "trigger",
    "file": "sting.wav",
    "active": True
})

# Update device ID and mur gateway config
resp = requests.post(f"{base_url}/api/device", json={
    "id": "MURMURA-STAGE-01",
    "mur_gateway_ip": "192.168.1.10"
})

# Save configuration to survive reboot
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
- Audio files must be WAV format on the SD card at `/sdcard/`
- Configuration is persisted to `/sdcard/track_config.json`
- Device ID is persisted to `/sdcard/unit_id.txt`
