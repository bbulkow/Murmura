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

## Known Bugs

### Stale device cards / duplicate entries after device ID changes

`DeviceRegistry.load_registry()` in [network_wrapper.py](network_wrapper.py)
does not clear `self.devices` before reloading from `device_map.json`. If a
device's reported ID changes during a session (for example: SD card briefly
ejected so the device falls back to its default ID, then put back so it
re-announces with its real ID), the in-memory dict accumulates an entry under
each ID it has ever seen — even after `device_map.json` is rewritten with only
the current ID.

The dashboard then shows duplicate cards for the same physical device (same
IP, same scene, same tracks) and/or stale cards for IDs that no longer exist
in the file.

**Workaround**: restart the config server, or click "Delete All" and rescan.
Both flush the in-memory dict.

**Fix when there's time**: clear `self.devices = {}` at the top of
`load_registry()` so in-memory state always matches the file.

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
