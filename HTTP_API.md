# Audio Loop Controller HTTP API

## Overview

The ESP32 Audio Loop Controller provides a JSON-based HTTP API for remote control of audio loops. Once connected to WiFi, the device exposes a web server on port 80 that allows you to:

- List available audio files on the SD card
- Set files for individual tracks
- Start and stop loops on individual tracks
- Adjust volume for each track (0-100%)
- Adjust global/master volume (0-100%)
- Monitor currently playing loops

## Getting Started

1. Ensure your device is connected to WiFi (see WIFI_SETUP.md)
2. Find your device's IP address (displayed in serial logs)
3. Access the API documentation at: `http://<device-ip>/`
4. Use the API endpoints below to control loops

## API Endpoints

### List Audio Files

**GET** `/api/files`

Lists all audio files (WAV and MP3) in the SD card root directory.

**Response:**
```json
{
  "files": [
    {
      "index": 0,
      "name": "track1.wav",
      "type": "wav",
      "path": "/sdcard/track1.wav"
    },
    {
      "index": 1,
      "name": "track2.wav",
      "type": "wav",
      "path": "/sdcard/track2.wav"
    }
  ],
  "count": 2
}
```

### List Loop Status

**GET** `/api/loops`

Returns the complete state of all tracks (always returns all 3 tracks).

**Response:**
```json
{
  "loops": [
    {
      "track": 0,
      "file": "/sdcard/track1.wav",
      "volume": 100,
      "playing": true
    },
    {
      "track": 1,
      "file": "",
      "volume": 50,
      "playing": false
    },
    {
      "track": 2,
      "file": "/sdcard/track3.wav",
      "volume": 75,
      "playing": true
    }
  ],
  "active_count": 2,
  "max_tracks": 3,
  "global_volume": 75
}
```

**Note:** 
- All tracks are always returned, with `file` being an empty string if no file is set
- `volume` is track-specific volume (0-100%)
- `global_volume` is the master volume control (0-100%)

### Set Loop File

**POST** `/api/loop/file`

Sets the file for a specific track and starts playing it immediately.

**Request Body:**
```json
{
  "track": 0,
  "file_index": 0  // Use file index from /api/files
}
```

**Alternative Request Body:**
```json
{
  "track": 0,
  "file_path": "/sdcard/track1.wav"  // Specify path directly
}
```

**Response:**
```json
{
  "success": true,
  "track": 0,
  "file": "/sdcard/track1.wav",
  "message": "File set and loop started"
}
```

### Start a Loop

**POST** `/api/loop/start`

Starts or restarts playing a track with its currently configured file.

**Request Body:**
```json
{
  "track": 0
}
```

**Response:**
```json
{
  "success": true,
  "track": 0,
  "file": "/sdcard/track1.wav",
  "message": "Loop started"
}
```

**Note:** Returns an error if no file is configured for the track. Use `/api/loop/file` first.

### Stop a Loop

**POST** `/api/loop/stop`

Stops a loop on a specific track. The file assignment and volume are preserved.

**Request Body:**
```json
{
  "track": 0
}
```

**Response:**
```json
{
  "success": true,
  "track": 0,
  "message": "Loop stop command sent"
}
```

### Set Track Volume

**POST** `/api/loop/volume`

Adjusts the volume for a specific track.

**Request Body:**
```json
{
  "track": 0,
  "volume": 75  // 0-100%
}
```

**Response:**
```json
{
  "success": true,
  "track": 0,
  "volume": 75,
  "message": "Volume adjustment command sent"
}
```

### Set Global Volume

**POST** `/api/global/volume`

Adjusts the master/global volume (affects all tracks).

**Request Body:**
```json
{
  "volume": 85  // 0-100%
}
```

**Response:**
```json
{
  "success": true,
  "volume": 85,
  "message": "Global volume adjustment command sent"
}
```

## Example Usage

### Using curl

```bash
# List available files
curl http://192.168.1.100/api/files

# Set file for track 0 (starts playing immediately)
curl -X POST http://192.168.1.100/api/loop/file \
  -H "Content-Type: application/json" \
  -d '{"track": 0, "file_index": 0}'

# Stop loop on track 0
curl -X POST http://192.168.1.100/api/loop/stop \
  -H "Content-Type: application/json" \
  -d '{"track": 0}'

# Restart loop on track 0 (uses previously set file)
curl -X POST http://192.168.1.100/api/loop/start \
  -H "Content-Type: application/json" \
  -d '{"track": 0}'

# Set track 0 volume to 50%
curl -X POST http://192.168.1.100/api/loop/volume \
  -H "Content-Type: application/json" \
  -d '{"track": 0, "volume": 50}'

# Set global volume to 85%
curl -X POST http://192.168.1.100/api/global/volume \
  -H "Content-Type: application/json" \
  -d '{"volume": 85}'

# Check status of all loops
curl http://192.168.1.100/api/loops
```

### Using JavaScript/Fetch

```javascript
// Set file and start loop
fetch('http://192.168.1.100/api/loop/file', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    track: 0,
    file_index: 0
  })
})
.then(response => response.json())
.then(data => console.log(data));

// Stop a loop
fetch('http://192.168.1.100/api/loop/stop', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    track: 0
  })
})
.then(response => response.json())
.then(data => console.log(data));

// Restart a loop
fetch('http://192.168.1.100/api/loop/start', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    track: 0
  })
})
.then(response => response.json())
.then(data => console.log(data));

// Adjust track volume
fetch('http://192.168.1.100/api/loop/volume', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    track: 0,
    volume: 75
  })
})
.then(response => response.json())
.then(data => console.log(data));

// Adjust global volume
fetch('http://192.168.1.100/api/global/volume', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    volume: 85
  })
})
.then(response => response.json())
.then(data => console.log(data));

// Get loop status
fetch('http://192.168.1.100/api/loops')
.then(response => response.json())
.then(data => console.log(data));
```

### Using Python

```python
import requests
import json

# Device IP
base_url = "http://192.168.1.100"

# List files
response = requests.get(f"{base_url}/api/files")
files = response.json()
print(f"Available files: {files}")

# Set file and start loop
response = requests.post(
    f"{base_url}/api/loop/file",
    json={"track": 0, "file_index": 0}
)
print(f"File set response: {response.json()}")

# Stop loop
response = requests.post(
    f"{base_url}/api/loop/stop",
    json={"track": 0}
)
print(f"Stop response: {response.json()}")

# Restart loop
response = requests.post(
    f"{base_url}/api/loop/start",
    json={"track": 0}
)
print(f"Start response: {response.json()}")

# Set track volume
response = requests.post(
    f"{base_url}/api/loop/volume",
    json={"track": 0, "volume": 75}
)
print(f"Track volume response: {response.json()}")

# Set global volume
response = requests.post(
    f"{base_url}/api/global/volume",
    json={"volume": 85}
)
print(f"Global volume response: {response.json()}")

# Get loop status
response = requests.get(f"{base_url}/api/loops")
loops = response.json()
print(f"Loop status: {loops}")
```

## Track Management

- The system supports up to 3 simultaneous tracks (0, 1, 2)
- Each track can play one audio file at a time
- Files automatically loop when they reach the end
- Setting a new file on a track will stop the currently playing file and start the new one
- Stopping a track preserves the file assignment and volume - you can restart it later
- Volume adjustments are applied in real-time and persist across stop/start

## Volume Control

The system has two levels of volume control:

1. **Track Volume** (`/api/loop/volume`) - Individual volume for each track (0-100%)
2. **Global Volume** (`/api/global/volume`) - Master volume that affects all tracks (0-100%)

Volume values:
- 100% = Full volume (0dB internally)
- 50% = Half volume (-6dB internally)
- 25% = Quarter volume (-12dB internally)
- 0% = Muted (-60dB internally)

## API Design Philosophy

The API separates the concerns of file management, playback control, and volume control:

1. **`/api/loop/file`** - Sets which file a track should play (and starts it immediately)
2. **`/api/loop/start`** - Starts/restarts playback with the currently set file
3. **`/api/loop/stop`** - Stops playback but remembers the file and volume
4. **`/api/loop/volume`** - Adjusts track volume independently of playback state
5. **`/api/global/volume`** - Adjusts master volume for all tracks

This design allows for:
- Cleaner semantics - each endpoint has a single purpose
- Better state management - file assignments and volumes persist
- Easier UI development - can show all tracks consistently
- More flexible control - can restart tracks without re-specifying files or volumes

## Configuration Persistence Endpoints

### Get Configuration Status

**GET** `/api/config/status`

Returns both the current running configuration and the saved configuration from the SD card (if it exists).

**Response (with saved config):**
```json
{
  "current": {
    "global_volume": 85,
    "loops": [
      {
        "track": 0,
        "is_playing": true,
        "file_path": "/sdcard/track1.wav",
        "volume": 100
      },
      {
        "track": 1,
        "is_playing": false,
        "file_path": "/sdcard/track2.wav",
        "volume": 75
      },
      {
        "track": 2,
        "is_playing": true,
        "file_path": "/sdcard/track3.wav",
        "volume": 50
      }
    ]
  },
  "saved": {
    "global_volume": 75,
    "loops": [
      {
        "track": 0,
        "is_playing": true,
        "file_path": "/sdcard/track1.wav",
        "volume": 100
      },
      {
        "track": 1,
        "is_playing": true,
        "file_path": "/sdcard/track2.wav",
        "volume": 100
      },
      {
        "track": 2,
        "is_playing": true,
        "file_path": "/sdcard/track3.wav",
        "volume": 100
      }
    ]
  },
  "saved_exists": true
}
```

**Response (no saved config):**
```json
{
  "current": {
    "global_volume": 85,
    "loops": [...]
  },
  "saved": null,
  "saved_exists": false
}
```

### Save Configuration

**POST** `/api/config/save`

Saves the current configuration (loops, volumes, playing states) to `/sdcard/loop_config.json`. This configuration will be automatically loaded on device startup.

**Request Body:** None required

**Response (success):**
```json
{
  "success": true,
  "message": "Configuration saved to SD card",
  "path": "/sdcard/loop_config.json"
}
```

**Response (error):**
```json
{
  "success": false,
  "error": "Failed to save configuration: SD card not mounted"
}
```

### Load Configuration

**POST** `/api/config/load`

Loads and applies the saved configuration from `/sdcard/loop_config.json`. This allows you to restore a previously saved configuration without restarting the device.

**Request Body:** None required

**Response (success):**
```json
{
  "success": true,
  "message": "Configuration loaded and applied",
  "tracks_configured": 3
}
```

**Response (error - file not found):**
```json
{
  "success": false,
  "error": "No saved configuration found"
}
```

**Response (error - invalid config):**
```json
{
  "success": false,
  "error": "Invalid configuration format"
}
```

### Delete Configuration

**DELETE** `/api/config/delete`

Deletes the saved configuration file from the SD card. After deletion, the device will use the default configuration on next startup.

**Request Body:** None required

**Response (success):**
```json
{
  "success": true,
  "message": "Configuration file deleted"
}
```

**Response (error):**
```json
{
  "success": false,
  "error": "Configuration file not found"
}
```

## WiFi Management Endpoints

### Get WiFi Status

**GET** `/api/wifi/status`

Returns the current WiFi connection status including SSID, IP address, and signal strength.

**Response (when connected):**
```json
{
  "connected": true,
  "ssid": "MyNetwork",
  "ip_address": "192.168.1.100",
  "rssi": -65,
  "signal_strength": 70
}
```

**Response (when disconnected):**
```json
{
  "connected": false,
  "state": "scanning"  // or "connecting", "connection_failed", "disconnected"
}
```

### List Configured Networks

**GET** `/api/wifi/networks`

Returns a list of all WiFi networks configured in the device.

**Response:**
```json
{
  "networks": [
    {
      "index": 0,
      "ssid": "HomeNetwork",
      "has_password": true,
      "auth_failed": false,
      "available": true,
      "rssi": -65
    },
    {
      "index": 1,
      "ssid": "OfficeNetwork",
      "has_password": true,
      "auth_failed": false,
      "available": false,
      "rssi": -127
    }
  ],
  "count": 2,
  "max_networks": 5
}
```

**Note:** Passwords are never exposed in the response for security reasons.

### Add WiFi Network

**POST** `/api/wifi/add`

Adds a new WiFi network to the device configuration. The device will immediately attempt to connect to the new network.

**Request Body:**
```json
{
  "ssid": "NetworkName",
  "password": "NetworkPassword"
}
```

**Response (success):**
```json
{
  "success": true,
  "message": "Network added successfully",
  "ssid": "NetworkName"
}
```

**Response (error):**
```json
{
  "success": false,
  "error": "Maximum number of networks reached"
}
```

### Remove WiFi Network

**POST** `/api/wifi/remove`

Removes a WiFi network from the device configuration.

**Request Body:**
```json
{
  "ssid": "NetworkName"
}
```

**Response (success):**
```json
{
  "success": true,
  "message": "Network removed successfully",
  "ssid": "NetworkName"
}
```

**Response (error):**
```json
{
  "success": false,
  "error": "Network not found"
}
```

## Error Handling

All endpoints return appropriate HTTP status codes:
- `200 OK` - Request successful
- `400 Bad Request` - Invalid request format or parameters
- `500 Internal Server Error` - Server error

Error responses include a JSON body with details:
```json
{
  "success": false,
  "error": "Track index out of range"
}
```

## Configuration Management Examples

### Using curl

```bash
# Get configuration status (current vs saved)
curl http://192.168.1.100/api/config/status

# Save current configuration to SD card
curl -X POST http://192.168.1.100/api/config/save

# Load saved configuration from SD card
curl -X POST http://192.168.1.100/api/config/load

# Delete saved configuration
curl -X DELETE http://192.168.1.100/api/config/delete
```

### Using JavaScript/Fetch

```javascript
// Get configuration status
fetch('http://192.168.1.100/api/config/status')
  .then(response => response.json())
  .then(data => {
    console.log('Current config:', data.current);
    console.log('Saved config:', data.saved);
    console.log('Config exists:', data.saved_exists);
  });

// Save current configuration
fetch('http://192.168.1.100/api/config/save', {
  method: 'POST'
})
.then(response => response.json())
.then(data => console.log('Save result:', data));

// Load saved configuration
fetch('http://192.168.1.100/api/config/load', {
  method: 'POST'
})
.then(response => response.json())
.then(data => console.log('Load result:', data));

// Delete saved configuration
fetch('http://192.168.1.100/api/config/delete', {
  method: 'DELETE'
})
.then(response => response.json())
.then(data => console.log('Delete result:', data));
```

### Using Python

```python
import requests

# Device IP
base_url = "http://192.168.1.100"

# Get configuration status
response = requests.get(f"{base_url}/api/config/status")
config_status = response.json()
print(f"Current config: {config_status['current']}")
print(f"Saved exists: {config_status['saved_exists']}")

# Save current configuration
response = requests.post(f"{base_url}/api/config/save")
print(f"Save result: {response.json()}")

# Load saved configuration
response = requests.post(f"{base_url}/api/config/load")
print(f"Load result: {response.json()}")

# Delete saved configuration
response = requests.delete(f"{base_url}/api/config/delete")
print(f"Delete result: {response.json()}")
```

## WiFi Management Examples

### Using curl

```bash
# Get WiFi status
curl http://192.168.1.100/api/wifi/status

# List configured networks
curl http://192.168.1.100/api/wifi/networks

# Add a new WiFi network
curl -X POST http://192.168.1.100/api/wifi/add \
  -H "Content-Type: application/json" \
  -d '{"ssid": "MyNetwork", "password": "MyPassword"}'

# Remove a WiFi network
curl -X POST http://192.168.1.100/api/wifi/remove \
  -H "Content-Type: application/json" \
  -d '{"ssid": "OldNetwork"}'
```

### Using JavaScript/Fetch

```javascript
// Get WiFi status
fetch('http://192.168.1.100/api/wifi/status')
  .then(response => response.json())
  .then(data => console.log(data));

// Add a new network
fetch('http://192.168.1.100/api/wifi/add', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    ssid: 'MyNetwork',
    password: 'MyPassword'
  })
})
.then(response => response.json())
.then(data => console.log(data));
```

## Notes

- CORS headers are included to allow browser-based access
- The server runs on port 80 (standard HTTP)
- Maximum request body size is limited by ESP32 memory
- File paths must exist on the SD card
- Audio files must be in WAV or MP3 format
- The `/api/loops` endpoint always returns all 3 tracks for consistent UI state
- Volume is always specified as 0-100% (not in dB)
- Global volume corresponds to the same control as the physical Vol+/Vol- buttons on the device
- The device supports up to 5 WiFi networks in its configuration
- WiFi operations are performed asynchronously in the background
- Signal strength is reported as both RSSI (dBm) and percentage (0-100)
- Adding a new network triggers an immediate connection attempt
