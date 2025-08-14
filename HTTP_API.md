# Audio Loop Controller HTTP API

## Overview

The ESP32 Audio Loop Controller provides a JSON-based HTTP API for remote control of audio loops. Once connected to WiFi, the device exposes a web server on port 80 that allows you to:

- List available audio files on the SD card
- Start and stop loops on individual tracks
- Adjust gain for each track
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

### List Currently Playing Loops

**GET** `/api/loops`

Returns information about all currently playing loops.

**Response:**
```json
{
  "loops": [
    {
      "track": 0,
      "file": "/sdcard/track1.wav",
      "gain_db": 0.0,
      "playing": true
    }
  ],
  "active_count": 1,
  "max_tracks": 3
}
```

### Start a Loop

**POST** `/api/loop/start`

Starts playing a loop on a specific track (0-2).

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
  "message": "Loop started"
}
```

### Stop a Loop

**POST** `/api/loop/stop`

Stops a loop on a specific track.

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
  "message": "Loop stopped"
}
```

### Set Loop Gain

**POST** `/api/loop/gain`

Adjusts the gain (volume) for a specific loop.

**Request Body:**
```json
{
  "track": 0,
  "gain_db": -6.0  // Range: -60 to +12 dB
}
```

**Response:**
```json
{
  "success": true,
  "track": 0,
  "gain_db": -6.0,
  "message": "Gain updated"
}
```

## Example Usage

### Using curl

```bash
# List available files
curl http://192.168.1.100/api/files

# Start a loop on track 0
curl -X POST http://192.168.1.100/api/loop/start \
  -H "Content-Type: application/json" \
  -d '{"track": 0, "file_index": 0}'

# Set gain to -6dB on track 0
curl -X POST http://192.168.1.100/api/loop/gain \
  -H "Content-Type: application/json" \
  -d '{"track": 0, "gain_db": -6.0}'

# Stop loop on track 0
curl -X POST http://192.168.1.100/api/loop/stop \
  -H "Content-Type: application/json" \
  -d '{"track": 0}'
```

### Using JavaScript/Fetch

```javascript
// Start a loop
fetch('http://192.168.1.100/api/loop/start', {
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

// Adjust gain
fetch('http://192.168.1.100/api/loop/gain', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    track: 0,
    gain_db: -3.0
  })
})
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

# Start a loop
response = requests.post(
    f"{base_url}/api/loop/start",
    json={"track": 0, "file_index": 0}
)
print(f"Start response: {response.json()}")

# Set gain
response = requests.post(
    f"{base_url}/api/loop/gain",
    json={"track": 0, "gain_db": -6.0}
)
print(f"Gain response: {response.json()}")
```

## Track Management

- The system supports up to 3 simultaneous tracks (0, 1, 2)
- Each track can play one audio file at a time
- Files automatically loop when they reach the end
- Starting a new file on a track will stop the currently playing file
- Gain adjustments are applied in real-time

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

## Notes

- CORS headers are included to allow browser-based access
- The server runs on port 80 (standard HTTP)
- Maximum request body size is limited by ESP32 memory
- File paths must exist on the SD card
- Audio files must be in WAV or MP3 format
