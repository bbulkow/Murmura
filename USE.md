# Using Murmura

This is the operator guide for working with deployed Murmura devices ("Murs"). It covers four main workflows: finding your devices, managing WiFi, managing SD card audio files, and controlling playback and volume.

## Access Methods

Each Mur has three ways to interact with it:

| Method | Best For | Requires |
|--------|----------|---------|
| **Device web UI** (`http://<device-ip>/`) | Status display (read-only); change device ID at `/settings` | Browser + device IP |
| **mur-config-server** (`http://localhost:8765`) | Fleet overview, batch play/stop/volume, editing what each scene plays | Python on a laptop or Pi |
| **mur-scene-server** (`http://localhost:5003`) | Switching the fleet-wide active scene; scene schedules | Python on the show host |
| **CLI scripts** (`device-manager/`) | Batch file sync, ID assignment, scripting | Python + `device_map.json` |

The device web UI is read-only except for one setting: you can change the device ID at `/settings`. Everything else — WiFi management, file transfers, playback control, volume — requires curl, the CLI scripts, or the mur-config-server.

---

## 1. Finding Your Devices

Murs connect to WiFi on boot and receive an IP address via DHCP. There is no fixed IP or required central server — devices operate independently once configured.

There are two hard coded wifi addresses, others may have been added.

As a last resort, you can hook up a serial port and use the ESP-IDF tools to listen to the serial port. That will output whether
the device attached to a wifi, and what its configuration is.

### Option A: mur-config-server (recommended for fleets)

```bash
cd mur-config-server
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

Every Mur ships with these WiFi networks pre-loaded into NVS on first boot:

| SSID | Password |
|------|----------|
| `murmura` | `flgforever` |
| `medea` | `!medea4u` |

`murmura` is the static config — stand up an AP with that SSID and password and any Mur is reachable without ever visiting its webpage. (`flg-haven` / `fuckoffanddie` was a built-in in earlier firmware; it is no longer written to new devices, but it is not scrubbed from units that already have it stored.)

These are hard-coded in the `builtin_networks[]` table in `main/murmura.c` and added automatically on first boot. Any that are missing are re-added on every subsequent boot, so flashing new firmware picks up newly-added built-ins. They are not overwritten if you later add additional networks. If none of these networks are in range, the device boots without WiFi — audio playback continues normally; the HTTP API is simply unreachable until WiFi connects.

It is also possible to add more wifi addresses. You may need to use one of the hardcoded wifis just to get into the device, but the device
manager scripts will allow you to identify then add to the wifi addresses.

The device stores up to 10 networks and auto-connects to the strongest available one. Networks that fail authentication are remembered and skipped until you clear the failure flag.

### Checking WiFi status

Via browser: the main page at `http://<device-ip>/` shows WiFi status in the device info section,
although it's a little silly. You know you're connected, otherwise you wouldn't see the page. At least
you can see some statistics.

Via curl:
```bash
curl http://<device-ip>/api/device
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

Via mur-config-server: not yet supported — use curl.

### Removing a WiFi network

```bash
curl -X POST http://<device-ip>/api/wifi/remove \
  -H "Content-Type: application/json" \
  -d '{"ssid":"OldNetwork"}'
```

---

## 3. Managing Files on the SD Card

Audio files (WAV or MP3) live on the SD card of each Mur. You can add or remove files without physically touching the SD card using the HTTP API. The mur-config-server does not yet support file operations — use the CLI or curl.

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

## 4. Scenes and Playback

All playback configuration is organized into **scenes**. A scene is a named configuration ("day", "night", "show") containing a global volume and settings for all 3 tracks. One scene is active (playing) at a time, and one can be set as the default for boot. Creating a new scene clones the active scene's config.

Each Mur has three tracks (0, 1, 2) per scene that can play simultaneously, mixed by the hardware codec. Each track has a file, volume, mode (loop or trigger), and active state.

> **Track limit note:** Three is a practical memory ceiling, not an arbitrary design choice. ESP-ADF spins up a significant number of processes per pipeline, and stereo 48 kHz 16-bit WAV files consume substantial RAM. Whether three simultaneous MP3 tracks will work is not guaranteed — the decoder pipelines add overhead on top of the per-track cost. If you hit stability issues with multiple tracks, reduce to two, or prefer WAV files if RAM is the bottleneck.

### Checking current state

```bash
# Get all scenes (curl)
curl http://<device-ip>/api/scenes

# Get all scenes (CLI)
python device_controller.py --id MURMURA-001 --command get-scenes
```

Via mur-config-server: click a device card to see all scenes with their full track configs.

### Managing scenes

```bash
# Create a new scene (clones the active scene)
curl -X POST http://<device-ip>/api/scene \
  -H "Content-Type: application/json" \
  -d '{"action": "create", "name": "night"}'

# Activate a scene (applies to hardware immediately)
curl -X POST http://<device-ip>/api/scene \
  -H "Content-Type: application/json" \
  -d '{"action": "activate", "name": "night"}'

# Set the default boot scene
curl -X POST http://<device-ip>/api/scene \
  -H "Content-Type: application/json" \
  -d '{"action": "set_default", "name": "day"}'

# Delete a scene (cannot delete the active scene)
curl -X POST http://<device-ip>/api/scene \
  -H "Content-Type: application/json" \
  -d '{"action": "delete", "name": "night"}'
```

### Configuring tracks within a scene

All track changes go through `POST /api/scenes` with the scene name as a key. Only stated fields change — omitted fields are untouched.

```bash
# Assign a file and enable track 0 in the "day" scene
curl -X POST http://<device-ip>/api/scenes \
  -H "Content-Type: application/json" \
  -d '{"day": {"tracks": [{"track": 0, "file_path": "loop1.wav", "active": true}]}}'

# Set per-track volume
curl -X POST http://<device-ip>/api/scenes \
  -H "Content-Type: application/json" \
  -d '{"day": {"tracks": [{"track": 0, "volume": 75}]}}'

# Set global volume for a scene
curl -X POST http://<device-ip>/api/scenes \
  -H "Content-Type: application/json" \
  -d '{"day": {"global_volume": 80}}'

# Disable a track
curl -X POST http://<device-ip>/api/scenes \
  -H "Content-Type: application/json" \
  -d '{"day": {"tracks": [{"track": 0, "active": false}]}}'

# CLI — set track 0 volume in "night" scene
python device_controller.py --id MURMURA-001 --command set-scene --scene night --track 0 --volume 75
```

If the scene being edited is the active scene, changes take effect on the hardware immediately.

### Trigger-based scene switching

Scenes can be switched automatically via the Haven trigger system. Two mechanisms:

**Discrete scene trigger** — a single trigger (Discrete type) whose value is treated as a scene name. Defaults to `"SceneChange"` (matches the system-wide constant in `scene_service` and `mur_gateway`); rarely needs to be changed. Editable at the device level:

```bash
# Override the discrete scene trigger name (defaults to "SceneChange")
curl -X POST http://<device-ip>/api/device \
  -H "Content-Type: application/json" \
  -d '{"scene_trigger_name": "SceneChange"}'
```

When an event arrives with `name=SceneChange` and `value=night`, the device activates scene "night". If the value doesn't match any scene, the device activates the default scene.

**Per-scene button trigger** — each scene can have a trigger (On/Off type) that activates it when an "On" event arrives:

```bash
# Set a button trigger on the "night" scene
curl -X POST http://<device-ip>/api/scenes \
  -H "Content-Type: application/json" \
  -d '{"night": {"button_trigger": "ButtonB"}}'
```

When an event arrives with `name=ButtonB` and `value=On`, the device activates scene "night".

Via mur-config-server: the device detail page shows Scene Trigger and per-scene Trigger fields. The batch panel allows setting the scene trigger name across all devices at once.

### Fleet-wide scene switching (mur-scene-server)

Everything above changes scenes on **one device at a time**. To move the whole fleet
at once, use **mur-scene-server** at `http://<host>:5003` — the service that owns which
scene is active across the installation.

The split matters, and it is the thing to keep straight:

> **mur-scene-server decides _which_ scene is active. Each Mur decides _what that scene
> sounds like_.**

mur-scene-server stores only scene **names** plus which one is active. When you activate
`night`, it fires the `SceneChange` trigger; the Mur Gateway fans that out and every Mur
looks up `night` in its own `/sdcard/scenes.json` and plays whatever it finds there. A Mur
that has no `night` scene falls back to its default scene. Editing what `night` *plays* is
mur-config-server's job, per device.

Because of that, **a scene name only does something on a Mur that already has a scene with
that name.** Create the scene on the devices first (or clone it across the fleet), then
switch to it here.

```bash
# What scenes exist and which is live
curl http://<host>:5003/api/scenes

# Switch the whole fleet
curl -X POST http://<host>:5003/api/scenes/active \
  -H "Content-Type: application/json" \
  -d '{"name":"night"}'

# Just the active scene name (this is what the gateway polls)
curl http://<host>:5003/api/scenes/active

# Add a scene name to the fleet list
curl -X POST http://<host>:5003/api/scenes \
  -H "Content-Type: application/json" \
  -d '{"name":"night"}'
```

Scene names are validated against the device limits: **1–31 characters, letters, digits,
hyphen and underscore**. A name that would not fit on a Mur is rejected here rather than
failing silently later. A device holds at most 16 scenes; going past that is a warning, not
an error, since the list is fleet-wide.

**Never fire the `SceneChange` trigger by hand** from the trigger server or any other tool —
it is reserved for this service. Doing it elsewhere makes the gateway believe a scene that
mur-scene-server does not have, and it silently snaps back within ~30 s. See
[SYNC_DESIGN.md](SYNC_DESIGN.md).

#### Scheduled scene changes

The web UI at `http://<host>:5003/` also schedules activations — "go to `day` at 08:00
daily", "go to `show` at 21:30 once". One-shot schedules delete themselves after firing.

```bash
curl -X POST http://<host>:5003/api/schedules \
  -H "Content-Type: application/json" \
  -d '{"scene":"day","time":"08:00","repeat":"daily"}'
```

### Saving configuration

Changes are live but not persisted until you explicitly save. On next boot, each Mur loads its saved scenes and activates the default scene.

```bash
# Save on one device (curl)
curl -X POST http://<device-ip>/api/config/save

# Save on one device (CLI)
python device_controller.py --id MURMURA-001 --command save-config
```

Via mur-config-server: the **Download Config** / **Upload Config** buttons let you save scenes as a JSON file and clone them to other devices.

---

## Quick Reference

| Task | Device web UI | mur-config-server | CLI / curl |
|------|--------------|--------------|------------|
| Discover devices | — | Scan Network button | `device_scanner.py -n <subnet> -a create` |
| Check device status | displays at `http://<ip>/` | Dashboard cards | `device_controller.py -c status` |
| **Switch the whole fleet's scene** | — | Scene Manager button → :5003 | `curl POST :5003/api/scenes/active {"name":"..."}` |
| **Schedule a scene change** | — | Scene Manager button → :5003 | `curl POST :5003/api/schedules` |
| **List fleet scene names** | — | Scene Manager button → :5003 | `curl GET :5003/api/scenes` |
| View all scenes on a device | — | Device detail page | `curl GET /api/scenes` |
| Create a scene | — | Create Scene input | `curl POST /api/scene {"action":"create","name":"..."}` |
| Activate a scene | — | Activate button | `curl POST /api/scene {"action":"activate","name":"..."}` |
| Set default boot scene | — | Set Default button | `curl POST /api/scene {"action":"set_default","name":"..."}` |
| Edit track in a scene | — | Track controls | `curl POST /api/scenes {"scene": {"tracks":[...]}}` |
| Set scene volume | — | Scene volume slider | `curl POST /api/scenes {"scene": {"global_volume":N}}` |
| Download/upload config | — | Download/Upload buttons | `curl GET /api/scenes` (save JSON) |
| Add WiFi network | — | — | `curl POST /api/wifi/add` |
| Remove WiFi network | — | — | `curl POST /api/wifi/remove` |
| List SD card files | — | View Files button | `curl GET /api/files` |
| Upload a file | — | — | `file_manager.py -c upload -f file.wav` |
| Delete a file | — | — | `curl DELETE /api/file/delete` |
| Save config to SD card | — | Save Config button | `curl POST /api/config/save` |
| Change device ID | `/settings` page | — | `device_controller.py -c set-id --new-id NEW-ID` |
| Reboot a device | — | Reboot button | `curl POST /api/system/reboot` |

---

## Further Reading

- [device-manager/README.md](device-manager/README.md) — full CLI tool reference including network scanning, filtering, and ID management
- [mur-config-server/README.md](mur-config-server/README.md) — fleet server setup, systemd auto-start, and web UI reference
- [mur-scene-server/README.md](mur-scene-server/README.md) — fleet-wide active scene, schedules, and the scene-name rules
- [HTTP_API.md](HTTP_API.md) — complete HTTP API reference with request/response examples
