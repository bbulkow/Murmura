# Murmura HTTP API

## Overview

The ESP32 Murmura device provides a JSON-based HTTP API for remote control of audio tracks. Once connected to WiFi, the device exposes a web server on port 80.

Each device has three tracks (0, 1, 2). Each track has:
- **mode**: `"loop"` (continuously repeats) or `"trigger"` (plays when a trigger event arrives)
- **active**: whether the track is enabled/playing
- **file**: the audio file assigned to the track
- **volume**: per-track volume (0–100%)
- **trigger_name**: name of the trigger event to listen for (empty string = no trigger)
- **trigger_mode**: `"momentary"` (start on keyDown "On", stop on keyUp "Off") or `"oneshot"` (start on keyDown, plays to completion, ignore keyUp)

There is also a global/master volume that scales all tracks.

The device can connect to a Haven Trigger Gateway to receive trigger events:
- **trigger_server_ip**: IP address of the Trigger Gateway (empty = disabled)
- **trigger_server_port**: port of the Trigger Gateway (default 5002)
- The device listens for incoming TCP connections from the gateway on port 5100

## API Endpoints

### List Audio Files

**GET** `/api/files`

Lists all audio files (WAV) in the SD card root directory.

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

---

### Get Track Status

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
      "active": false,
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
- `active`: true if the track is playing (loop) or was last started by a trigger
- `file`: full path, or empty string if none assigned
- `volume`: per-track volume 0–100%
- `trigger_name`: trigger event name to match; empty string = no trigger assigned
- `trigger_mode`: `"momentary"` or `"oneshot"`

---

### Set Track Configuration

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
- `active` *(optional)*: `true` to start/arm, `false` to stop
- `file` *(optional)*: filename (e.g. `"ambient.wav"`) or full path (e.g. `"/sdcard/ambient.wav"`)
- `volume` *(optional)*: 0–100
- `trigger_name` *(optional)*: name of trigger event to bind (e.g. `"RedButton.Button_1"`); empty string clears
- `trigger_mode` *(optional)*: `"momentary"` or `"oneshot"`

**Behavior:**
- Setting `active: true` starts playback (loop mode) or arms for triggering (trigger mode). Requires a file to be configured.
- Setting `active: false` stops the track.
- Changing `file` while the track is active restarts playback with the new file.
- Changing `volume` or `mode` alone does not start/stop the track.

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

---

### Set Global Volume

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
  "volume": 75,
  "message": "Global volume adjustment command sent"
}
```

---

### Get Trigger Server Configuration

**GET** `/api/trigger-server`

Returns the current trigger gateway IP/port and the local listen port.

**Response:**
```json
{
  "trigger_server_ip": "192.168.1.10",
  "trigger_server_port": 5002,
  "trigger_listen_port": 5100
}
```

- `trigger_server_ip`: IP of the Haven Trigger Gateway; empty string if not configured
- `trigger_server_port`: gateway HTTP port (default 5002)
- `trigger_listen_port`: port this device listens on for incoming TCP events (fixed: 5100)

---

### Set Trigger Server Configuration

**POST** `/api/trigger-server`

Updates the trigger gateway connection settings. The trigger listener will use these values on its next registration attempt (every 30 s) or immediately if `trigger_listener_register_with_gateway()` is called.

**Request Body:**
```json
{
  "trigger_server_ip": "192.168.1.10",
  "trigger_server_port": 5002
}
```

Both fields are optional — only the fields present are updated.

**Response:**
```json
{
  "success": true,
  "trigger_server_ip": "192.168.1.10",
  "trigger_server_port": 5002
}
```

---

## Configuration Persistence

### Get Configuration Status

**GET** `/api/config/status`

Returns current running state and whether a saved config exists on SD card.

**Response:**
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
  }
}
```

### Save Configuration

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

### Load Configuration

**POST** `/api/config/load`

Loads and applies saved configuration from SD card.

**Response:**
```json
{
  "success": true,
  "message": "Configuration loaded and applied successfully"
}
```

### Delete Configuration

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

## WiFi Management

### Get WiFi Status

**GET** `/api/wifi/status`

**Response (connected):**
```json
{
  "connected": true,
  "ssid": "MyNetwork",
  "ip_address": "192.168.1.100",
  "rssi": -65,
  "signal_strength": 75
}
```

### List Configured Networks

**GET** `/api/wifi/networks`

```json
{
  "networks": [
    { "index": 0, "ssid": "HomeNetwork", "has_password": true, "available": true, "rssi": -65 }
  ],
  "count": 1,
  "max_networks": 5
}
```

### Add WiFi Network

**POST** `/api/wifi/add`

```json
{ "ssid": "NetworkName", "password": "NetworkPassword" }
```

### Remove WiFi Network

**POST** `/api/wifi/remove`

```json
{ "ssid": "NetworkName" }
```

---

## Device Status

### Get Device Status

**GET** `/api/status`

```json
{
  "mac_address": "AA:BB:CC:DD:EE:FF",
  "id": "MURMURA-001",
  "ip_address": "192.168.1.100",
  "wifi_connected": true,
  "firmware_version": "2.0",
  "uptime_seconds": 3600,
  "uptime_formatted": "00 01:00:00"
}
```

### Get Device ID

**GET** `/api/id`

```json
{ "id": "MURMURA-001", "success": true }
```

### Set Device ID

**POST** `/api/id`

```json
{ "id": "MURMURA-STAGE-01" }
```

The ID is persisted to `/sdcard/unit_id.txt`.

---

## File Management

### Upload Audio File

**POST** `/api/upload?filename=track.wav`

- Content-Type: `application/octet-stream`
- Body: binary file data

```json
{ "success": true, "filename": "track.wav", "path": "/sdcard/track.wav", "size": 1048576 }
```

### Delete Audio File

**DELETE** `/api/file/delete`

```json
{ "filename": "track.wav" }
```

---

## System

### Reboot

**POST** `/api/system/reboot`

```json
{ "delay_ms": 1000 }
```

---

## Examples

### curl

```bash
# Get track status
curl http://192.168.1.100/api/tracks

# Start track 0 as a loop with a file at 80% volume
curl -X POST http://192.168.1.100/api/track \
  -H "Content-Type: application/json" \
  -d '{"track": 0, "mode": "loop", "active": true, "file": "ambient.wav", "volume": 80}'

# Stop track 0
curl -X POST http://192.168.1.100/api/track \
  -H "Content-Type: application/json" \
  -d '{"track": 0, "active": false}'

# Change volume on track 1 without affecting play state
curl -X POST http://192.168.1.100/api/track \
  -H "Content-Type: application/json" \
  -d '{"track": 1, "volume": 50}'

# Set track 2 as trigger mode
curl -X POST http://192.168.1.100/api/track \
  -H "Content-Type: application/json" \
  -d '{"track": 2, "mode": "trigger", "file": "sting.wav", "active": true}'

# Set global volume
curl -X POST http://192.168.1.100/api/global/volume \
  -H "Content-Type: application/json" \
  -d '{"volume": 75}'

# Save current configuration
curl -X POST http://192.168.1.100/api/config/save
```

### Python

```python
import requests

base_url = "http://192.168.1.100"

# Get all track status
resp = requests.get(f"{base_url}/api/tracks")
print(resp.json())

# Configure track 0 as a looping ambient sound
resp = requests.post(f"{base_url}/api/track", json={
    "track": 0,
    "mode": "loop",
    "active": True,
    "file": "ambient.wav",
    "volume": 80
})
print(resp.json())

# Stop track 0
resp = requests.post(f"{base_url}/api/track", json={"track": 0, "active": False})

# Set track 1 as a trigger
resp = requests.post(f"{base_url}/api/track", json={
    "track": 1,
    "mode": "trigger",
    "file": "sting.wav",
    "active": True
})

# Save configuration to survive reboot
resp = requests.post(f"{base_url}/api/config/save")
```

---

## Error Handling

All endpoints return HTTP 200 with a JSON body. Errors use `"success": false`:

```json
{ "success": false, "error": "Track index out of range" }
```

HTTP status codes:
- `200 OK` — request processed (check `success` field)
- `400 Bad Request` — missing or invalid request body
- `500 Internal Server Error` — server-side failure

---

## Notes

- CORS headers included for browser access
- Server runs on port 80
- Audio files must be WAV format on the SD card at `/sdcard/`
- Configuration is persisted to `/sdcard/track_config.json`
- Device ID is persisted to `/sdcard/unit_id.txt`
