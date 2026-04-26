# Mur Gateway Systemd Installation Guide

This guide explains how to install and configure the Mur Gateway as a systemd service on Linux.

The checked-in service file targets `brian@/home/brian/Murmura/mur-gateway` and points the gateway at a Haven Trigger Server on `localhost:5002`. On other deployments, edit `User=`, `Group=`, `WorkingDirectory=`, the `ExecStart=` path, and the `--trigger-host`/`--trigger-port` arguments to match.

## Ports

All ports are unprivileged — no special capabilities needed.

| Port | Direction | Purpose |
|------|-----------|---------|
| `4000` | inbound | Mur device connections (devices connect outbound here) |
| `4001` | inbound | HTTP `/status` endpoint |
| `5100` | inbound | Trigger Server connects here as the registered TCP_SOCKET service |
| `5002` | outbound | Connection to Haven Trigger Server (HTTP registration) |

## Installation Steps

### Prerequisites

```bash
cd ~/Murmura/mur-gateway
pip3 install --user -r requirements.txt
```

### Install the service

1. **Copy the service file**:
   ```bash
   sudo cp ~/Murmura/mur-gateway/mur-gateway.service /etc/systemd/system/
   ```

2. **Reload systemd**:
   ```bash
   sudo systemctl daemon-reload
   ```

3. **Enable and start**:
   ```bash
   sudo systemctl enable --now mur-gateway.service
   ```

### Verify

```bash
sudo systemctl status mur-gateway.service
sudo journalctl -u mur-gateway.service -f
curl -s http://localhost:4001/status | python3 -m json.tool
```

## Changing the Trigger Server target

Edit `/etc/systemd/system/mur-gateway.service` and update the `--trigger-host` / `--trigger-port` arguments in `ExecStart=`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart mur-gateway.service
```

## Service Management

| Command | Description |
|---------|-------------|
| `sudo systemctl start mur-gateway` | Start |
| `sudo systemctl stop mur-gateway` | Stop |
| `sudo systemctl restart mur-gateway` | Restart |
| `sudo systemctl status mur-gateway` | Status |
| `sudo systemctl enable mur-gateway` | Enable on boot |
| `sudo systemctl disable mur-gateway` | Disable on boot |
| `sudo journalctl -u mur-gateway -f` | Follow logs |

## Uninstalling

```bash
sudo systemctl disable --now mur-gateway.service
sudo rm /etc/systemd/system/mur-gateway.service
sudo systemctl daemon-reload
```
