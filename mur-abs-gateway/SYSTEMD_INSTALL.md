# Mur Abs Gateway Systemd Installation Guide

This service replaces [mur-gateway](../mur-gateway/SYSTEMD_INSTALL.md) on the
same host using the same ports. Only one of the two services should run at a
time. The unit file declares `Conflicts=mur-gateway.service` to enforce that.

## Ports

| Port | Direction | Purpose |
|------|-----------|---------|
| `4000` | inbound | Mur device connections |
| `4001` | inbound | HTTP `/status` and `/triggers` endpoints |
| `5100` | inbound | Trigger Server connects here as the registered TCP_SOCKET service |
| `5101` | inbound | Web UI for abstract trigger config + live log |
| `5002` | outbound | Connection to Haven Trigger Server (HTTP registration) |

## Migrating from mur-gateway

### Prerequisites

```bash
# Uses the shared virtualenv at /home/pi/Murmura/venv (see mur-config-server/SYSTEMD_INSTALL.md)
cd ~/Murmura
./venv/bin/pip install -r mur-abs-gateway/requirements.txt
# NOTE: mur-abs-gateway is a drop-in *replacement* for mur-gateway, not an
# addition — its unit declares Conflicts=mur-gateway.service. Do not enable both.
```

### Disable mur-gateway, install mur-abs-gateway

```bash
sudo systemctl disable --now mur-gateway.service
sudo cp ~/Murmura/mur-abs-gateway/mur-abs-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mur-abs-gateway.service
```

### Verify

```bash
sudo systemctl status mur-abs-gateway.service
sudo journalctl -u mur-abs-gateway.service -f
curl -s http://localhost:4001/status | python3 -m json.tool
# Open the UI in a browser:
xdg-open http://localhost:5101/
```

## Rollback to mur-gateway

```bash
sudo systemctl disable --now mur-abs-gateway.service
sudo systemctl enable --now mur-gateway.service
```

The two services have identical wire behavior for non-abstract triggers, so
deployed devices need no reconfiguration when swapping back.

## Changing the Trigger Server target

Edit `/etc/systemd/system/mur-abs-gateway.service` and update the
`--trigger-host` / `--trigger-port` arguments in `ExecStart=`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart mur-abs-gateway.service
```

## Service Management

| Command | Description |
|---------|-------------|
| `sudo systemctl start mur-abs-gateway`   | Start |
| `sudo systemctl stop mur-abs-gateway`    | Stop |
| `sudo systemctl restart mur-abs-gateway` | Restart |
| `sudo systemctl status mur-abs-gateway`  | Status |
| `sudo journalctl -u mur-abs-gateway -f`  | Follow logs |

## Uninstalling

```bash
sudo systemctl disable --now mur-abs-gateway.service
sudo rm /etc/systemd/system/mur-abs-gateway.service
sudo systemctl daemon-reload
```
