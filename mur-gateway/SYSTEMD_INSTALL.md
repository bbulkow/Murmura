# Mur Gateway Systemd Installation Guide

This guide explains how to install and configure the Mur Gateway as a systemd service on Linux.

The checked-in service file targets `pi@/home/pi/Murmura/mur-gateway`, runs from the shared virtualenv at `/home/pi/Murmura/venv`, and points the gateway at a trigger source on `localhost:5002`. On other deployments, edit `User=`, `Group=`, `WorkingDirectory=`, the `ExecStart=` path, and the `--trigger-host`/`--trigger-port` arguments to match.

**What is on :5002 depends on the deployment.** In a Haven-style install it is the Haven Trigger Server (`trigger.service`). In an *ensemble* install it is [mur-conductor](../mur-conductor/), which speaks the same registration protocol. Either way the gateway is the client, so it must start *after* whichever one you run — the unit's `After=` lists both, and ordering against an absent unit is a harmless no-op.

**Scene service.** The gateway primes a scene cache from `--scene-service-url` (default `http://localhost:5003`) with 30 retries at 2 s. Run [mur-scene-server](../mur-scene-server/) there — it needs no gateway-side configuration, since 5003 is already the default. If you run *no* scene service, expect ~60 s of `Scene cache prime failed` warnings at every start, then a single `Gave up priming` error, and thereafter a warning on every lazy refresh (`scene_cache_ttl`, default 30 s) whenever a device asks `get_scene`. Not fatal — `cached_scene` stays `null`, devices are answered `null`, and the ensemble/downbeat path never consults the scene cache — but it is noisy in the journal on an unattended host.

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
# Uses the shared virtualenv at /home/pi/Murmura/venv (see mur-config-server/SYSTEMD_INSTALL.md)
cd ~/Murmura
./venv/bin/pip install -r mur-gateway/requirements.txt
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
