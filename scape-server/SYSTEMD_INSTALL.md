# Scape Server Systemd Installation Guide

This guide explains how to install and configure the Loudframe Scape Server as a systemd service on Raspberry Pi (Bookworm or similar).

## Features Added

1. **Runs on port 80 by default** - The service file is configured to run on port 80 for easier access (no port number needed in URLs).
2. **Easy port override** - You can change the port using the `SCAPE_SERVER_PORT` environment variable.
3. **Systemd service file** - Automatically start the server on boot and restart on failure.
4. **Privileged port support** - Uses systemd capabilities to allow binding to port 80 without running as root.

## Port Configuration

### Default Port
- **In service**: The systemd service runs on port **80** (configured in service file)
- **Manual run**: When run manually, defaults to port **8765** (unless overridden)

### Overriding the Port

You can override the port in several ways:

1. **In app.py directly** (at the top of the file):
   ```python
   DEFAULT_PORT = 8765  # Change this value
   ```

2. **Using environment variable when running manually**:
   ```bash
   export SCAPE_SERVER_PORT=9000
   python3 app.py
   ```

3. **In the systemd service** (see systemd configuration below)

## Systemd Service Installation

### Prerequisites

1. Ensure Python 3 and required dependencies are installed:
   ```bash
   cd /home/pi/loudframe/play_sdcard_multi/scape-server
   pip3 install -r requirements.txt
   ```

### Installation Steps

1. **Copy the service file to systemd directory**:
   ```bash
   sudo cp /home/pi/loudframe/play_sdcard_multi/scape-server/scape-server.service /etc/systemd/system/
   ```

2. **Reload systemd to recognize the new service**:
   ```bash
   sudo systemctl daemon-reload
   ```

3. **Enable the service to start on boot**:
   ```bash
   sudo systemctl enable scape-server.service
   ```

4. **Start the service now**:
   ```bash
   sudo systemctl start scape-server.service
   ```

### Verifying the Service

Check the service status:
```bash
sudo systemctl status scape-server.service
```

View the logs:
```bash
sudo journalctl -u scape-server.service -f
```

### Changing the Port in Systemd

To change the port when running as a systemd service:

1. **Edit the service file**:
   ```bash
   sudo nano /etc/systemd/system/scape-server.service
   ```

2. **Modify the port environment variable**:
   ```ini
   Environment="SCAPE_SERVER_PORT=8765"  # or any port you prefer
   ```

   **Note**: For ports below 1024 (privileged ports), ensure these lines are present:
   ```ini
   AmbientCapabilities=CAP_NET_BIND_SERVICE
   CapabilityBoundingSet=CAP_NET_BIND_SERVICE
   ```

3. **Reload and restart**:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl restart scape-server.service
   ```

## Service Management Commands

| Command | Description |
|---------|-------------|
| `sudo systemctl start scape-server` | Start the service |
| `sudo systemctl stop scape-server` | Stop the service |
| `sudo systemctl restart scape-server` | Restart the service |
| `sudo systemctl status scape-server` | Check service status |
| `sudo systemctl enable scape-server` | Enable auto-start on boot |
| `sudo systemctl disable scape-server` | Disable auto-start on boot |
| `sudo journalctl -u scape-server -f` | View live logs |
| `sudo journalctl -u scape-server --since today` | View today's logs |

## Accessing the Web Interface

Once the service is running:

- **From the Raspberry Pi**: http://localhost (port 80 is default)
- **From other devices on the network**: http://[raspberry-pi-ip]

No port number is needed in the URL since it runs on port 80!

To find your Raspberry Pi's IP address:
```bash
hostname -I
```

## Troubleshooting

### Service won't start
1. Check the logs: `sudo journalctl -u scape-server.service -n 50`
2. Verify Python dependencies are installed: `pip3 list`
3. Check file permissions: `ls -l /home/pi/loudframe/play_sdcard_multi/scape-server/`

### Port already in use
If you get a "port already in use" error:
1. Check what's using the port: `sudo lsof -i :80` (or your configured port)
2. For port 80, check if Apache or nginx is running: `sudo systemctl status apache2 nginx`
3. Stop conflicting services or change the port using the environment variable method above

### Can't access from network
1. Check firewall settings: `sudo ufw status`
2. If firewall is active, allow the port: `sudo ufw allow 80/tcp`

## Uninstalling

To remove the service:

```bash
# Stop and disable the service
sudo systemctl stop scape-server.service
sudo systemctl disable scape-server.service

# Remove the service file
sudo rm /etc/systemd/system/scape-server.service

# Reload systemd
sudo systemctl daemon-reload
```

## Notes

- The service runs as the `pi` user with the home directory `/home/pi`
- If you installed the repository in a different location, update the paths in the service file
- The service automatically restarts on failure with a 10-second delay
- Logs are sent to the systemd journal (use `journalctl` to view)
