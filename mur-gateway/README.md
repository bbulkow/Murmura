# Mur Gateway

Protocol gateway between the Haven Trigger Server and Murmura ESP32 audio devices.

## Architecture

```
[Trigger Sources] → [Haven Trigger Gateway :5002]
                           ↓ (TCP_SOCKET, all events)
                    [Mur Gateway :5100]
                     ↓         ↓        ↓
               [Mur Dev 1] [Mur Dev 2] [Mur Dev N]
               (outbound TCP connections on port 4000)
```

The Mur Gateway registers with the Haven Trigger Server as a TCP_SOCKET service. When trigger events arrive, it forwards them only to devices that have subscribed to matching trigger names.

Devices connect outbound to the Mur Gateway (no inbound connections to devices needed), announce their ID, and subscribe to the triggers they care about.

## Quick Start

```bash
pip install -r requirements.txt

# Connect to a Trigger Server at 192.168.1.10:5002
python mur_gateway.py --trigger-host 192.168.1.10

# With all options
python mur_gateway.py \
  --trigger-host 192.168.1.10 \
  --trigger-port 5002 \
  --device-port 4000 \
  --upstream-port 5100 \
  --status-port 4001 \
  --name mur-gateway \
  --verbose
```

## Command-Line Options

| Option | Default | Description |
|--------|---------|-------------|
| `--trigger-host` | `localhost` | Haven Trigger Server IP/hostname |
| `--trigger-port` | `5002` | Haven Trigger Server port |
| `--device-port` | `4000` | TCP port for Mur device connections |
| `--upstream-port` | `5100` | TCP port for Trigger Server to connect to |
| `--status-port` | `4001` | HTTP port for status endpoint |
| `--name` | `mur-gateway` | Name used in Trigger Server registration |
| `-v, --verbose` | off | Enable debug logging |

## Status Endpoint

`GET http://localhost:4001/status`

Returns JSON with connected devices, their subscriptions, and upstream connection status:

```json
{
  "gateway": "mur-gateway",
  "version": "1.0",
  "trigger_server": "192.168.1.10:5002",
  "upstream_connected": true,
  "device_port": 4000,
  "devices": [
    {
      "id": "MURMURA-001",
      "peer": "('192.168.1.50', 52341)",
      "triggers": ["RedButton.Button_1"],
      "connected_at": 1710150000.0,
      "uptime_seconds": 120.5
    }
  ],
  "device_count": 1,
  "subscriptions": {
    "RedButton.Button_1": 1
  }
}
```

## Protocol

See [MUR_PROTOCOL.md](MUR_PROTOCOL.md) for the full protocol specification.

**Summary:** Devices connect via TCP, send newline-delimited JSON messages to announce their ID and subscribe to trigger names. The gateway forwards matching trigger events (also newline-delimited JSON, same format as Haven protocol).

## Trigger type handling

The gateway converts upstream `On/Off` triggers to `OneShot` for Mur devices:

- **Listing** (`GET /triggers`): upstream `On/Off` entries are relabeled `OneShot`. The original `On/Off` type is hidden — the OneShot list returned to clients merges real-OneShot triggers and relabeled-from-On/Off triggers.
- **Dispatch**: the gateway drops events with falsy values (`Off`/`off`/`0`/`false`) and strips truthy values (`On`/`on`/`1`/`true`) from forwarded events. Discrete/Continuous values pass through unchanged.

**Why this lives in the gateway:** firmware on the deployed devices can't currently choose to treat an On/Off trigger as a OneShot. Until firmware can be updated in the field, the gateway compensates so users can use upstream On/Off triggers for full-clip playback. When firmware can be updated, this conversion should move back into the device per-track config (see "Future improvement" in [MUR_PROTOCOL.md](../MUR_PROTOCOL.md#trigger-type-translation)).

## Requirements

- Python 3.10+
- `aiohttp` (for HTTP registration with Trigger Server and status endpoint)
