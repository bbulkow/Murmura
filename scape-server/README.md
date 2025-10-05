# Device Manager server

A web-based fleet management server for Brian's soundscape devicies, designed to run on a Raspberry Pi in an art installation.

## Features

- **Network Scanning**: Automatically discover ESP32 devices on your local network
- **Device Dashboard**: View all discovered devices with their status, IP addresses, and current settings
- **Individual Device Control**: 
  - Control playback (play/pause/stop)
  - Adjust volume, globally and per track
  - View loaded files
  - Change playing loops
  - Link to device website
- **Batch Operations**: 
  - Control multiple devices simultaneously
  - Set volume for multiple devices at once
  - Start/stop playback on selected devices
- **Real-time Updates**: WebSocket support for live device status updates
- **Auto-scan Mode**: Continuously monitor the network for new devices

## Todo - can be done through scripts for now

 - File upload and management, both batch and individually
 - Managing IDs when devices are being deployed


## Installation

### Prerequisites

- Python 3.7 or higher
- pip (Python package manager)

### Setup

1. Navigate to the scape_server directory:
```bash
cd scape_server
```

2. Install the required Python packages:
```bash
pip install -r requirements.txt
```

## Running the Server

### Windows (PowerShell 7)

1. Open PowerShell 7 as Administrator (for network scanning capabilities)
2. Navigate to the scape_server directory:
```powershell
cd C:\Users\<username>\dev\esp\loudframe\play_sdcard_multi\scape_server
```

3. **Option A: Using Virtual Environment (Recommended)**
   
   Virtual environments are recommended but **not required**. They provide:
   - **Isolation**: Keeps project dependencies separate from system Python packages
   - **Version Control**: Ensures specific package versions don't conflict with other projects
   - **Clean Uninstall**: Easy to remove all project dependencies by deleting the venv folder
   - **Reproducibility**: Guarantees the same environment across different machines
   
   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python app.py
   ```

4. **Option B: Direct Installation (Simpler)**
   
   If you prefer to skip the virtual environment:
   ```powershell
   pip install -r requirements.txt
   python app.py
   ```
   
   Note: This installs packages globally, which may conflict with other Python projects.

If you encounter execution policy issues:
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### macOS (Terminal)

1. Open Terminal
2. Navigate to the scape_server directory:
```bash
cd ~/dev/esp/loudframe/play_sdcard_multi/scape_server
```

3. **Option A: Using Virtual Environment (Recommended)**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   python3 app.py
   ```

4. **Option B: Direct Installation (Simpler)**
   ```bash
   pip3 install -r requirements.txt
   python3 app.py
   ```

### Linux/Raspberry Pi

1. Open terminal
2. Navigate to the scape_server directory:
```bash
cd ~/loudframe/play_sdcard_multi/scape_server
```

3. Install dependencies:
```bash
pip3 install -r requirements.txt
```

4. Run the server:
```bash
python3 app.py
```

### Accessing the Server

Once running, the server will be accessible at:
- Local machine: `http://localhost:8765`
- Network access: `http://<your-ip>:8765`

**Note**: The default port was changed from 5000 to 8765 to avoid conflicts with commonly used applications. You can override this by setting the `SCAPE_SERVER_PORT` environment variable.

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
1. Select devices using the checkboxes on each card
2. Use the batch controls at the top:
   - "Select All" / "Deselect All" for quick selection
   - "Play All Selected" / "Stop All Selected" for playback control
   - Batch volume slider to set volume for all selected devices

### Automatic Scanning

Enable "Start Auto Scan" to continuously monitor the network every 30 seconds for new devices or status changes.

## API Endpoints

The server provides RESTful API endpoints for programmatic access:

### Device Management
- `GET /api/devices` - List all registered devices
- `GET /api/device/<device_id>` - Get specific device information
- `POST /api/scan` - Trigger a network scan

### Device Control
- `POST /api/device/<device_id>/volume` - Set device volume
- `POST /api/device/<device_id>/play` - Control playback
- `GET /api/device/<device_id>/files` - Get file list
- `GET /api/device/<device_id>/loops` - Get loop configuration
- `POST /api/device/<device_id>/loops` - Set loop configuration

### Batch Operations
- `POST /api/batch/volume` - Set volume for multiple devices
- `POST /api/batch/play` - Control playback for multiple devices

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
- `SERVER_PORT`: Override using `SCAPE_SERVER_PORT` environment variable
- `host`: Server host (default: 0.0.0.0 for network access)
- `debug`: Flask debug mode (default: True)

#### Changing the Port

You can override the default port in several ways:

1. **Environment variable** (recommended):
   ```bash
   export SCAPE_SERVER_PORT=9000
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

## Running on Raspberry Pi

### Auto-start on Boot with Systemd

The scape-server includes a pre-configured systemd service file for easy installation on Raspberry Pi.

**For detailed installation instructions, see [SYSTEMD_INSTALL.md](SYSTEMD_INSTALL.md)**

Quick installation:

1. Copy the service file:
```bash
sudo cp /home/pi/loudframe/play_sdcard_multi/scape-server/scape-server.service /etc/systemd/system/
```

2. Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable scape-server.service
sudo systemctl start scape-server.service
```

3. Check status:
```bash
sudo systemctl status scape-server.service
```

The service includes:
- Automatic restart on failure
- Proper environment configuration
- Easy port customization via environment variable
- Full logging to systemd journal

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

For issues or questions, please refer to the main Loudframe project documentation.
