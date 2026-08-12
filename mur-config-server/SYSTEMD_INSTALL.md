# Mur Config Server Systemd Installation Guide

This guide explains how to install and configure the Mur Config Server as a systemd service on Linux (tested on Ubuntu 22.04 / Jetson and Raspberry Pi Bookworm).

The checked-in service file targets `pi@/home/pi/Murmura/mur-config-server` (the `murmura` Raspberry Pi installation box), runs as **root** so it can bind **port 80**, and uses the shared virtualenv at `/home/pi/Murmura/venv`. On other deployments edit `User=`, `Group=`, `WorkingDirectory=`, `MUR_CONFIG_SERVER_PORT=`, and the `ExecStart=` interpreter/paths to match.

## Features

1. **Port is set by the unit** - `MUR_CONFIG_SERVER_PORT` in the service file; the app's own default (when run by hand) is still 8765.
2. **Easy port override** - Change that one `Environment=` line and `systemctl daemon-reload`.
3. **Systemd service file** - Automatically starts the server on boot and restarts on failure.

## Port Configuration

### Default Port
- **In service**: Port **80** (configured via `MUR_CONFIG_SERVER_PORT` in the service file).
- **Manual run**: **8765** (the `DEFAULT_PORT` in `app.py`, unless overridden).

### Binding port 80

80 is privileged, so the process needs help to bind it. Two ways:

- **`User=root`** — what the checked-in unit does. Simplest, but the whole
  Flask app (and everything it shells out to) runs as root. Consequence worth
  knowing: files the dashboard creates under `/home/pi/Murmura` — notably
  `device-manager/device_map.json`, uploads, and ensemble config rewrites —
  end up owned by `root`, so running the `device-manager/` CLI tools as `pi`
  afterwards may need `sudo`.
- **`User=pi` plus capabilities** — the narrower option, if you'd rather not
  run as root:
  ```ini
  User=pi
  Group=pi
  AmbientCapabilities=CAP_NET_BIND_SERVICE
  CapabilityBoundingSet=CAP_NET_BIND_SERVICE
  ```
  systemd grants exactly that one capability before exec, and nothing else.

### Overriding the Port

You can override the port in several ways:

1. **In app.py directly** (at the top of the file):
   ```python
   DEFAULT_PORT = 8765  # Change this value
   ```

2. **Using environment variable when running manually**:
   ```bash
   export MUR_CONFIG_SERVER_PORT=9000
   python3 app.py
   ```

3. **In the systemd service** (see systemd configuration below)

## Systemd Service Installation

### Prerequisites

1. Ensure Python 3 and required dependencies are installed. Debian 13 (trixie)
   and other PEP-668 "externally managed" distros reject `pip install --user`,
   so this deployment uses one **shared** virtualenv for every Murmura service:

   ```bash
   cd ~/Murmura
   python3 -m venv venv
   ./venv/bin/pip install -r mur-config-server/requirements.txt \
                          -r mur-gateway/requirements.txt \
                          -r mur-conductor/requirements.txt \
                          -r device-manager/requirements.txt
   ```

   It has to be *shared*, not one venv per service: `network_wrapper.py` launches
   `device-manager/device_scanner.py` with `sys.executable`, so device-manager's
   `aiohttp` must be importable from the config server's own interpreter.

   `netifaces` has no prebuilt aarch64 wheel and compiles on install — if that
   step fails, `sudo apt install python3-dev` and retry.

### Installation Steps

1. **Copy the service file to systemd directory**:
   ```bash
   sudo cp ~/Murmura/mur-config-server/mur-config-server.service /etc/systemd/system/
   ```

2. **Reload systemd to recognize the new service**:
   ```bash
   sudo systemctl daemon-reload
   ```

3. **Enable the service to start on boot**:
   ```bash
   sudo systemctl enable mur-config-server.service
   ```

4. **Start the service now**:
   ```bash
   sudo systemctl start mur-config-server.service
   ```

### Verifying the Service

Check the service status:
```bash
sudo systemctl status mur-config-server.service
```

View the logs:
```bash
sudo journalctl -u mur-config-server.service -f
```

### Changing the Port in Systemd

To change the port when running as a systemd service:

1. **Edit the service file**:
   ```bash
   sudo nano /etc/systemd/system/mur-config-server.service
   ```

2. **Modify the port environment variable**:
   ```ini
   Environment="MUR_CONFIG_SERVER_PORT=9000"  # or any port you prefer
   ```

   **Note**: For ports below 1024 (privileged ports), add these lines under `[Service]`:
   ```ini
   AmbientCapabilities=CAP_NET_BIND_SERVICE
   CapabilityBoundingSet=CAP_NET_BIND_SERVICE
   ```

3. **Reload and restart**:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl restart mur-config-server.service
   ```

## Service Management Commands

| Command | Description |
|---------|-------------|
| `sudo systemctl start mur-config-server` | Start the service |
| `sudo systemctl stop mur-config-server` | Stop the service |
| `sudo systemctl restart mur-config-server` | Restart the service |
| `sudo systemctl status mur-config-server` | Check service status |
| `sudo systemctl enable mur-config-server` | Enable auto-start on boot |
| `sudo systemctl disable mur-config-server` | Disable auto-start on boot |
| `sudo journalctl -u mur-config-server -f` | View live logs |
| `sudo journalctl -u mur-config-server --since today` | View today's logs |

## Accessing the Web Interface

Once the service is running:

- **From the host**: http://localhost:8765
- **From other devices on the network**: http://[host-ip]:8765

To find the host's IP address:
```bash
hostname -I
```

## Troubleshooting

### Service won't start
1. Check the logs: `sudo journalctl -u mur-config-server.service -n 50`
2. Verify Python dependencies are installed: `pip3 list`
3. Check file permissions: `ls -l ~/Murmura/mur-config-server/`

### Port already in use
If you get a "port already in use" error:
1. Check what's using the port: `sudo lsof -i :8765` (or your configured port)
2. Stop the conflicting service or change the port using the environment variable method above

### Can't access from network
1. Check firewall settings: `sudo ufw status`
2. If firewall is active, allow the port: `sudo ufw allow 8765/tcp`

## Uninstalling

To remove the service:

```bash
# Stop and disable the service
sudo systemctl stop mur-config-server.service
sudo systemctl disable mur-config-server.service

# Remove the service file
sudo rm /etc/systemd/system/mur-config-server.service

# Reload systemd
sudo systemctl daemon-reload
```

## Notes

- The checked-in service file runs as `brian` from `/home/brian/Murmura/mur-config-server`. For other deployments, edit `User=`, `Group=`, `WorkingDirectory=`, and the `ExecStart=` path in the service file before installing.
- The service automatically restarts on failure with a 10-second delay
- Logs are sent to the systemd journal (use `journalctl` to view)
