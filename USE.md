# Using Murmura

This is the operator guide for working with deployed Murmura devices ("Murs"). It covers four main workflows: finding your devices, managing WiFi, managing SD card audio files, and controlling playback and volume.

## Access Methods

Each Mur has three ways to interact with it:

| Method | Best For | Requires |
|--------|----------|---------|
| **Device web UI** (`http://<device-ip>/`) | Status display (read-only); change device ID at `/settings` | Browser + device IP |
| **scape-server** (`http://localhost:8765`) | Fleet overview, batch play/stop/volume | Python on a laptop or Pi |
| **CLI scripts** (`device-manager/`) | Batch file sync, ID assignment, scripting | Python + `device_map.json` |

The device web UI is read-only except for one setting: you can change the device ID at `/settings`. Everything else — WiFi management, file transfers, playback control, volume — requires curl, the CLI scripts, or the scape-server.

---

## 1. Finding Your Devices

Murs connect to WiFi on boot and receive an IP address via DHCP. There is no fixed IP or required central server — devices operate independently once configured.

There are two hard coded wifi addresses, others may have been added.

As a last resort, you can hook up a serial port and use the ESP-IDF tools to listen to the serial port. That will output whether
the device attached to a wifi, and what its configuration is.

### Option A: scape-server (recommended for fleets)

```bash
cd scape-server
pip install -r requirements.txt
python app.py
```

Open `http://localhost:8765`, click **Scan Network**, and all Murs on the subnet appear as cards. The scanner probes port 80 across the subnet looking for devices that respond to the Murmura API.

### Option B: CLI scanner

**NOTE:** replace the IP network with the network address you wish to scan.

```bash
cd device-manager
pip install -r requirements.txt
python device_scanner.py -n [192.168.1.0]/24 -a create
```

This creates `device_map.json`, which all other CLI tools use. Re-run this whenever devices have rebooted or IP addresses may have changed. Use `-a update` to refresh existing entries without discarding offline devices.

### Option C: Direct IP access

If you know a device's IP (from your router's DHCP list, or `device_map.json`), point a browser at `http://<device-ip>/`. The status page shows device ID, MAC address, WiFi status, uptime, and all loop tracks in real time.

---

## 2. WiFi & Network Config

### Default networks (hardcoded in firmware)

Every Mur ships with two WiFi networks pre-loaded into NVS on first boot:

| SSID | Password |
|------|----------|
| `medea` | `!medea4u` |
| `flg-haven` | `fuckoffanddie` |

These are hard-coded in `main/murmura.c` and added automatically on first boot. They are not overwritten if you later add additional networks. If neither network is in range, the device boots without WiFi — audio playback continues normally; the HTTP API is simply unreachable until WiFi connects.

It is also possible to add more wifi addresses. You may need to use one of the hardcoded wifis just to get into the device, but the device
manager scripts will allow you to identify then add to the wifi addresses.

The device stores up to 10 networks and auto-connects to the strongest available one. Networks that fail authentication are remembered and skipped until you clear the failure flag.

### Checking WiFi status

Via browser: the main page at `http://<device-ip>/` shows WiFi status in the device info section,
although it's a little silly. You know you're connected, otherwise you wouldn't see the page. At least
you can see some statistics.

Via curl:
```bash
curl http://<device-ip>/api/wifi/status
```

### Adding a WiFi network

All methods call the same HTTP endpoint. After adding, reboot the device so it scans and connects.

```bash
# Add a network
curl -X POST http://<device-ip>/api/wifi/add \
  -H "Content-Type: application/json" \
  -d '{"ssid":"YourNetwork","password":"YourPassword"}'

# Reboot so it connects
curl -X POST http://<device-ip>/api/system/reboot
```

Via scape-server: not yet supported — use curl.

### Removing a WiFi network

```bash
curl -X POST http://<device-ip>/api/wifi/remove \
  -H "Content-Type: application/json" \
  -d '{"ssid":"OldNetwork"}'
```

---

## 3. Managing Files on the SD Card

Audio files (WAV or MP3) live on the SD card of each Mur. You can add or remove files without physically touching the SD card using the HTTP API. The scape-server does not yet support file operations — use the CLI or curl.

### Listing files

```bash
# All devices (CLI)
python file_manager.py -c list

# One device (CLI)
python file_manager.py -c list -i MURMURA-001

# Direct (curl)
curl http://<device-ip>/api/files
```

### Uploading files

```bash
# Upload one file to all devices (skips if already present by name and size)
python file_manager.py -c upload -f loop1.wav

# Upload to a specific device
python file_manager.py -c upload -f loop1.wav -i MURMURA-001

# Force overwrite even if file exists
python file_manager.py -c upload -f loop1.wav -F

# Sync an entire directory (WAV and MP3; uploads new, skips existing)
python file_manager.py -c sync -d ./loops
```

### Deleting files

```bash
# Delete from all devices (CLI)
python file_manager.py -c delete -f old_loop.wav

# Direct (curl)
curl -X DELETE http://<device-ip>/api/file/delete \
  -H "Content-Type: application/json" \
  -d '{"filename":"old_loop.wav"}'
```

### Editing config directly on the SD card

Configuration (which file plays on which track, volumes) is stored as JSON on the SD card. If you have physical access, you can pull the card, edit the JSON file directly, and re-insert it. The device loads this config on next boot.

---

## 4. Playback: Loops and Volume

Each Mur has three loop tracks (0, 1, 2) that can play simultaneously, mixed by the hardware codec. You assign an audio file to each track, set per-track volume, and optionally set a global master volume. Configuration can be saved to the SD card so it survives reboots.

> **Track limit note:** Three is a practical memory ceiling, not an arbitrary design choice. ESP-ADF spins up a significant number of processes per pipeline, and stereo 48 kHz 16-bit WAV files consume substantial RAM. Whether three simultaneous MP3 tracks will work is not guaranteed — the decoder pipelines add overhead on top of the per-track cost. If you hit stability issues with multiple tracks, reduce to two, or prefer WAV files if RAM is the bottleneck.

### Checking current loop state

```bash
# All devices (CLI)
python batch_controller.py -c status

# Direct (curl)
curl http://<device-ip>/api/loops
```

Via browser: the main page at `http://<device-ip>/` shows all tracks with their current file, play state, and volume bar, and refreshes every 5 seconds.

### Assigning a file to a track

First, get the list of available files and their indices:
```bash
curl http://<device-ip>/api/files
# or
python file_manager.py -c list -i MURMURA-001
```

Then assign by index (CLI) or by path (curl):
```bash
# CLI — assign file index 2 to track 0 on one device
python device_controller.py -i MURMURA-001 -c set-file -k 0 -x 2

# Curl — assign by file path
curl -X POST http://<device-ip>/api/loop/file \
  -H "Content-Type: application/json" \
  -d '{"track":0,"file":"/loop1.wav"}'
```

### Starting and stopping playback

```bash
# Start all tracks on all devices (CLI)
python batch_controller.py -c start-all

# Stop all tracks on all devices (CLI)
python batch_controller.py -c stop-all

# Start/stop a single track on one device (curl)
curl -X POST http://<device-ip>/api/loop/start \
  -H "Content-Type: application/json" \
  -d '{"track":0}'

curl -X POST http://<device-ip>/api/loop/stop \
  -H "Content-Type: application/json" \
  -d '{"track":0}'
```

### Setting volume

Volume is 0–100. Per-track volume and global master volume are independent.

```bash
# Global master volume on all devices (CLI)
python batch_controller.py -c set-volume -g -v 80

# Per-track volume on all devices (CLI)
python batch_controller.py -c set-volume -k 0 -v 75

# Single device, per-track (CLI)
python device_controller.py -i MURMURA-001 -c set-volume -k 0 -v 75

# Global volume on one device (curl)
curl -X POST http://<device-ip>/api/global/volume \
  -H "Content-Type: application/json" \
  -d '{"volume":80}'

# Per-track volume on one device (curl)
curl -X POST http://<device-ip>/api/loop/volume \
  -H "Content-Type: application/json" \
  -d '{"track":0,"volume":75}'
```

Via scape-server: select devices and use the batch volume slider for global volume, or click a device card for per-track control.

### Saving configuration

Changes to loop assignments and volumes are live but not persisted until you explicitly save. On next boot, each Mur loads its saved config and begins playing automatically.

```bash
# Save on all devices (CLI)
python batch_controller.py -c save-config

# Save on one device (curl)
curl -X POST http://<device-ip>/api/config/save
```

---

## Quick Reference

| Task | Device web UI | scape-server | CLI / curl |
|------|--------------|--------------|------------|
| Discover devices | — | Scan Network button | `device_scanner.py -n <subnet> -a create` |
| Check device status | displays at `http://<ip>/` | Dashboard cards | `batch_controller.py -c status` |
| Add WiFi network | — | — | `curl POST /api/wifi/add` |
| Remove WiFi network | — | — | `curl POST /api/wifi/remove` |
| List SD card files | — | — | `file_manager.py -c list` |
| Upload a file | — | — | `file_manager.py -c upload -f file.wav` |
| Sync a folder of files | — | — | `file_manager.py -c sync -d ./dir` |
| Delete a file | — | — | `file_manager.py -c delete -f file.wav` |
| Assign file to a track | — | Device card | `device_controller.py -c set-file` |
| Set per-track volume | — | Device card slider | `batch_controller.py -c set-volume -k N -v N` |
| Set global volume | — | Batch volume slider | `batch_controller.py -c set-volume -g -v N` |
| Start / stop all | — | Batch play/stop buttons | `batch_controller.py -c start-all / stop-all` |
| Save config to SD card | — | — | `batch_controller.py -c save-config` |
| Change device ID | `/settings` page | — | `device_controller.py -c set-id -n NEW-ID` |
| Reboot a device | — | — | `curl POST /api/system/reboot` |

---

## Further Reading

- [device-manager/README.md](device-manager/README.md) — full CLI tool reference including network scanning, filtering, and ID management
- [scape-server/README.md](scape-server/README.md) — fleet server setup, systemd auto-start, and web UI reference
- [HTTP_API.md](HTTP_API.md) — complete HTTP API reference with request/response examples
