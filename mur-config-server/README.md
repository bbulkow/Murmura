# Device Manager server

A web-based fleet management server for Brian's soundscape devicies, designed to run on a Raspberry Pi in an art installation.

## Features

- **Network Scanning**: Automatically discover ESP32 devices on your local network
- **Device Dashboard**: View all discovered devices with their status, IP addresses, and current settings
- **Individual Device Control**:
  - Enable/disable tracks (start/stop playback)
  - Adjust volume, globally and per track
  - View loaded files
  - Change track file assignments
  - Link to device web UI
- **Batch Operations**: 
  - Control multiple devices simultaneously
  - Set volume for multiple devices at once
  - Start/stop playback on selected devices
- **Real-time Updates**: WebSocket support for live device status updates
- **Auto-scan Mode**: Continuously monitor the network for new devices

## Todo - can be done through scripts for now

 - File upload and management, both batch and individually
 - Managing IDs when devices are being deployed
 - **Save-all / restore-all configs**: walk every known device, pull its scenes/wifi/identity, persist a snapshot locally; companion restore that pushes a snapshot back to the fleet. Scriptable today; would be nicer in the UI later.
 - **Cross-fleet scene visibility**: a view that lists every device and the scenes each has. We had to manually hunt down stale scenes after a deploy — this would have made it trivial.
 - **Alphabetize lists in the UI**: devices, triggers, scenes, wifi networks — wherever there's a list, sort it. Currently insertion-order, which is not what the eye wants.
 - **Per-device description field**: free-text label like "back of house" / "front of house" stored alongside the device ID. IDs are great for plumbing but unmemorable after install; the description is what humans need on the device-list page.
 - **Batch delete**: select multiple devices (or all) and delete in one operation instead of one-at-a-time. Useful for cleanup after re-imaging or test-fleet teardown.


## Installation

### Prerequisites

- Python 3.7 or higher

### Setup

```bash
cd mur-config-server
pip install -r requirements.txt
```

## Running the Server

```bash
python app.py
```

### Windows

Open PowerShell 7 as Administrator (required for network scanning), then:

```powershell
cd C:\Users\<username>\dev\esp\Murmura\mur-config-server
pip install -r requirements.txt
python app.py
```

If you encounter execution policy issues:
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### macOS / Linux / Raspberry Pi

```bash
cd ~/Murmura/mur-config-server
pip3 install -r requirements.txt
python3 app.py
```

### Accessing the Server

Once running, the server will be accessible at:
- Local machine: `http://localhost:8765`
- Network access: `http://<your-ip>:8765`

**Note**: The default port was changed from 5000 to 8765 to avoid conflicts with commonly used applications. You can override this by setting the `MUR_CONFIG_SERVER_PORT` environment variable.

To find your IP address:
- **Windows**: `ipconfig` in PowerShell
- **macOS/Linux**: `ifconfig` or `ip addr`

### Debugging Mode

The server runs in debug mode by default, which provides:
- Automatic reload on code changes
- Detailed error messages
- Console logging

To see verbose network scanning output, run the scanner directly:
```bash
# Windows PowerShell
python network_scanner.py

# macOS/Linux
python3 network_scanner.py
```

## Usage

### Initial Setup

1. Open your web browser and navigate to the server address
2. Click "Scan Network" to discover ESP32 devices on your network
3. Discovered devices will appear as cards in the dashboard

### Managing Devices

#### Individual Control
- Click on any device card to open the detailed control panel
- Adjust volume using the slider
- Control playback with Play/Pause/Stop buttons
- View files stored on the device
- Configure loop settings

#### Batch Operations
1. All online devices are selected by default; deselect to exclude
2. Use the batch controls at the top:
   - "Select All" / "Deselect All" for quick selection
   - Save Config / Reboot for device management
   - Scene: activate or create scenes across all selected devices
   - Mur Gateway: set the gateway IP/port (OK remembers locally, Set Devices pushes to devices)
   - Scene Trigger: set the discrete scene trigger name on devices (filtered to Discrete trigger types)


### Automatic Scanning

Enable "Start Auto Scan" to continuously monitor the network every 30 seconds for new devices or status changes.

## API Endpoints

The server provides RESTful API endpoints for programmatic access:

### Device Management
- `GET /api/devices` - List all registered devices
- `GET /api/device/<device_id>` - Get specific device information
- `POST /api/scan` - Trigger a network scan

### Device Control
- `POST /api/device/<device_id>/volume` - Set global volume (via active scene)
- `POST /api/device/<device_id>/play` - Enable/disable all tracks
- `GET /api/device/<device_id>/files` - Get file list
- `GET/POST /api/device/<device_id>/scenes` - Get/patch scene configuration
- `POST /api/device/<device_id>/scene` - Scene actions (create, delete, activate, set_default)
- `GET /api/device/<device_id>/mur-gateway` - Get Mur Gateway + scene trigger config
- `POST /api/device/<device_id>/mur-gateway` - Set Mur Gateway config
- `POST /api/device/<device_id>/device-config` - Generic proxy to device's POST /api/device
- `GET /api/triggers` - Fetch trigger names from Mur Gateway (with type info)

### Batch Operations
- `POST /api/batch/volume` - Set global volume for multiple devices
- `POST /api/batch/play` - Enable/disable tracks for multiple devices
- `POST /api/batch/scene/activate` - Activate a scene on multiple devices
- `POST /api/batch/scene/create` - Create a scene on multiple devices (idempotent)
- `POST /api/batch/scene-trigger` - Set scene trigger name on multiple devices
- `POST /api/batch/mur-gateway` - Set Mur Gateway on multiple devices
- `POST /api/batch/save-config` - Save config on multiple devices
- `POST /api/batch/reboot` - Reboot multiple devices

### Scene Manager (proxied to mur-scene-server)
- `GET /api/scene-server/scenes` - The fleet-wide scene **name** list and which one is
  active, read from mur-scene-server (`scene_server_url` in `network_config.json`,
  default `http://127.0.0.1:5003`). Degrades to HTTP 200 with
  `{error, scenes: [], active_scene: null}` so no page breaks when it is down.

  Used by the device detail page's scene-trigger check, which prefers this live list
  over the trigger server's advertised `range.values` (that copy goes stale and cannot
  be refreshed — no Murmura trigger server implements `/api/register-device`).

  **This server owns what a scene *plays*; mur-scene-server owns which scene is
  *active*.** The dashboard's "Scene Manager" button links out to it.

### Ensembles (proxied to mur-conductor)
- `GET /api/conductor/status` - Conductor + gateway + per-group state. Degrades to
  HTTP 200 with `{error, groups: []}` so `/ensembles` still renders.
- `GET /api/conductor/triggers` - Trigger names the conductor drives (datalist source)
- `POST /api/conductor/groups` - Create/delete a group: `{action, name, ...fields}`
- `POST /api/conductor/groups/<group>` - Update any group field (see mur-conductor/README.md
  for which ones restart the group)
- `POST /api/conductor/groups/<group>/playlist` - Replace a playlist

### Ensembles (this server's own work)
- `GET /api/ensemble/<group>/readiness` - Per-member setup check. Reads each member's
  `/api/scenes` directly and returns structured problems, each with a code, a plain
  sentence, why it matters, how to fix it, and a link to that device's page. Ordered
  by dependency, not by check order: a wrong active scene or a loop-mode track is
  the *cause* of "not subscribed", so those are listed first. **Contacts every
  member, so it is on-demand only** - never called from the status poll.
- `POST /api/ensemble/<group>/configure` - Provision members for this group.
  Body `{"device_id": "30"}`, `{"device_ids": [...]}`, or `{}` for all. Re-runs the
  readiness check per device and applies **only the missing steps** (create scene,
  configure track, set boot default, activate, save to SD), then re-reads the
  device and reports what is still wrong. Idempotent - safe to press twice.
  Clears `file_path`: the conductor writes it before every downbeat, and an empty
  one keeps a device silent if a file push fails, rather than playing stale
  material. On a running group this can cost that member one entry.
- `GET /api/ensemble/<group>/probe?file=X[&size=N]` - Duration + format of one file
  from its WAV header, for the playlist editor's auto-fill
- `GET /api/ensemble/<group>/files` - File inventory across members; `?probe=a.wav,b.wav`
  adds durations read from the WAV headers
- `POST /api/ensemble/<group>/sync` - Queue throttled copies of files missing from a member
- `GET /api/ensemble/sync-status` - Progress of the copy queue

## Audio file format

**Every WAV must be 44100 Hz, 16-bit, stereo PCM.** Nothing in this server or on
the device converts formats.

A **mono** file plays at exactly **2x speed** with no error anywhere — the device
reads its bytes as interleaved stereo. A wrong sample rate plays at the wrong
speed. The only symptom is what you hear.

```bash
# check
ffprobe -v error -show_entries stream=channels,sample_rate -of csv=p=0 f.wav
# fix (duration is preserved; file size doubles)
ffmpeg -i in.wav -ac 2 -ar 44100 -c:a pcm_s16le out.wav
```

Note the interaction with playlists: durations are read from the WAV header, so a
mono entry finishes in half its declared span and the conductor waits out the rest
in silence. Converting the file fixes both symptoms and leaves the playlist
durations correct, because mono-to-stereo does not change duration.

`main/README.md` has the full analysis and three ADF-supported firmware fixes.
The most promising is to make the device output **mono** - these speakers are not
left/right pairs - which would remove the format footgun entirely and halve file
sizes.

## Ensembles page

`/ensembles` is the whole ensemble workflow: create and delete groups, edit every
group field, pick members, build the playlist from the files members actually
share, and check what each device still needs. `mur-conductor/config.json` should
not need to be opened.

Device-side setup stays deliberately manual. The checklist tells you exactly what
is wrong and links to the device page, where the existing scene controls fix it -
rather than a single "provision" button that hides what it changed.

The order that works: **create the group (leave it disabled) → add members → Check
setup → Configure this device (or Configure all) → set the playlist → Enable.**

*Check setup* lists exactly what each member is missing, in dependency order.
**Configure this device** then applies only those steps and saves to SD, and
**Configure all** does every member that needs it. Doing it by hand on the device
page still works and the checklist links there - the button is the same five
steps without the clicking.

Two lags are worth knowing, and the page says so where it matters:

- The conductor's view of the fleet is a 10 s poll, and subscriptions are
  published by the *device*. So after you fix something, the checklist (a direct
  device read) goes green immediately while the Status column still says "not
  subscribed" for a few seconds. That is the conductor catching up, not a failed
  fix - don't undo it.
- Nothing you change on a device survives a reboot until *Save Config*, and that
  cannot be verified remotely, so the checklist always lists it as a final step.

## WebSocket Events

The server supports WebSocket connections for real-time updates:

- `connect` - Client connection established
- `disconnect` - Client disconnected
- `scan_progress` - Network scan progress updates
- `scan_complete` - Scan finished with results
- `devices_update` - Device status changed
- `request_scan` - Request a network scan
- `start_auto_scan` - Enable automatic scanning
- `stop_auto_scan` - Disable automatic scanning

## Configuration

### Network Scanner Settings

Edit `network_scanner.py` to adjust:
- `timeout`: HTTP request timeout (default: 0.5 seconds for scanning, 1.0 for app)
- `max_workers`: Thread pool size for concurrent scanning (default: 50)
- `scan_interval`: Auto-scan interval in seconds (default: 30)

### Server Settings

Edit `app.py` to modify:
- `DEFAULT_PORT`: Server port (default: 8765)
- `SERVER_PORT`: Override using `MUR_CONFIG_SERVER_PORT` environment variable
- `host`: Server host (default: 0.0.0.0 for network access)
- `debug`: Flask debug mode (default: True)

#### Changing the Port

You can override the default port in several ways:

1. **Environment variable** (recommended):
   ```bash
   export MUR_CONFIG_SERVER_PORT=9000
   python3 app.py
   ```

2. **Edit DEFAULT_PORT in app.py** (at the top of the file):
   ```python
   DEFAULT_PORT = 9000  # Change this value
   ```

## Device Registry

The server maintains a persistent registry of discovered devices in `device_registry.json`. This file stores:
- Device IDs and IP addresses
- Last known status
- First and last seen timestamps
- Device configuration

## Registry semantics (fixed: stale cards / "Delete All" doing nothing)

`device_map.json` is **authoritative**. `DeviceRegistry.load_registry()`
rebuilds `self.devices` from the file on every call, so anything absent from
the file is gone from memory.

This was previously a merge that never reset the dict, so the registry could
only grow. Two symptoms, one cause:

- **"Delete All Devices" appeared to do nothing.** It wrote an empty
  `device_map.json` and reloaded, but the reload iterated zero devices and left
  every existing entry in memory. Stale cards stayed on the dashboard until the
  process was restarted — and since the reload happens on every
  `GET /api/devices`, they never aged out.
- **Duplicate cards after a device ID change** (SD card briefly ejected so the
  device falls back to its default ID, then restored). The dict kept one entry
  per ID the device had ever announced.

Three consequences of the file being authoritative, all handled in code:

1. `update_device()` drops any stale key for the same physical device (matched
   on `ip_address`) when re-keying, so a rename cannot leave a duplicate.
2. In-memory edits that must survive have to be written through with
   `save_registry()` (temp file + atomic replace). The probe loop calls it when
   a device's **id or mac** changes — not for the `online` flag, which churns
   every slot and would otherwise rewrite the file continuously. Without this,
   an ID change would be reverted by the next reload and re-detected forever.
3. A failed/partial read **keeps the previous contents** rather than blanking
   the registry. `device_scanner.py` writes the map non-atomically, so a reload
   landing mid-write sees truncated JSON; blanking there would flicker the
   dashboard.

Clearing devices flushes all three stores that hold device state: the file, the
registry dict, and the `_device_cache` in `app.py`. Clearing only the file is
what made the original bug survive.

## Troubleshooting

### Devices Not Discovered
1. Ensure devices are on the same network subnet
2. Check that devices have HTTP API enabled
3. Verify firewall settings allow HTTP traffic
4. Try increasing the timeout in `network_scanner.py`

### Connection Issues
1. Check that port 8765 is not blocked by firewall (or your custom port if changed)
2. Verify the server is running with `ps aux | grep app.py`
3. Check server logs for error messages

### Performance Issues
1. Reduce `max_workers` if scanning causes network congestion
2. Increase scan interval for less frequent updates
3. Disable auto-scan when not needed

## Auto-start on Boot with Systemd

The mur-config-server includes a pre-configured systemd service file. The checked-in file targets `brian@/home/brian/Murmura/mur-config-server`; edit `User=` and paths for other deployments.

**For detailed installation instructions, see [SYSTEMD_INSTALL.md](SYSTEMD_INSTALL.md)**

Quick installation:

1. Copy the service file:
```bash
sudo cp ~/Murmura/mur-config-server/mur-config-server.service /etc/systemd/system/
```

2. Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mur-config-server.service
```

3. Check status:
```bash
sudo systemctl status mur-config-server.service
```

The service includes:
- Automatic restart on failure
- Proper environment configuration
- Easy port customization via environment variable
- Full logging to systemd journal

## Offline / No-Internet Operation

This server is designed to run on standalone networks at art installations **with no internet access**. All resources (JS, CSS, fonts) must be served locally — no CDN links or external URLs. When adding dependencies, always bundle them in `static/` rather than linking to external CDNs.

## Security Considerations

**Warning**: This server is designed for use in controlled environments (art installations) and does not include authentication or encryption. For production use:

1. Add authentication middleware
2. Use HTTPS with proper certificates
3. Implement rate limiting
4. Add input validation and sanitization
5. Run behind a reverse proxy (nginx/Apache)

## Future Enhancements

Planned features for future versions:
- File upload capability to devices
- Firmware update management
- Device grouping and zones
- Scheduled playback automation
- Advanced loop configuration UI
- Network topology visualization
- Device health monitoring and alerts
- Backup and restore device configurations

## Support

For issues or questions, please refer to the main Murmura project documentation.
