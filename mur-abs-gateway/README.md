# Mur Abs Gateway

A drop-in replacement for [mur-gateway](../mur-gateway/) that adds an
**abstract trigger** layer. One logical trigger name (e.g. `AbstractTriggerFoo`)
backs N upstream triggers — when any of them fires, the gateway swaps the
device's track-2 `file_path` over HTTP and dispatches a OneShot under the
abstract name. Devices subscribe to the abstract name only.

This works around the firmware constraint of 3 tracks × 1 trigger each on
deployed (un-updateable) Mur devices, so a 5-button cluster can drive one
speaker without changing firmware.

## Architecture

```
[Trigger Sources] → [Haven Trigger Gateway :5002]
                          ↓ TCP_SOCKET, all events
                   [Mur Abs Gateway :5100 upstream / :4000 device / :4001 status / :5101 UI]
                          ├── HTTP POST /api/scenes (track 2 file_path swap)
                          └── TCP newline-JSON (OneShot under abstract name)
                          ↓
                   [Mur Devices]
```

## Quick start

```bash
cd ~/Murmura/mur-abs-gateway
pip install -r requirements.txt

python mur_abs_gateway.py --trigger-host 192.168.1.10
# UI at http://localhost:5101/
```

## Behavior

For each upstream event:

1. **On/Off → OneShot conversion** is applied first (drop `Off`, strip `On` value).
2. **Abstract dispatch**: for every abstract trigger that maps the upstream
   name, POST `/api/scenes` to each subscribed device to set track 2's
   `file_path`, then send a OneShot-shaped event with the abstract name.
3. **Direct passthrough**: any device subscribed to the raw upstream name
   still gets the raw event (preserves mur-gateway semantics; lets abstract
   and direct triggers coexist on the same gateway).

POST/dispatch order is `await`-sequenced: the device receives the new
`file_path` before the trigger event arrives, so the OneShot fires the new
file. POST failures cause the abstract event to be skipped for that device
only.

## File-path cache

To avoid hammering devices with redundant POSTs, the gateway caches the
last `file_path` set per `(device_id, track_index)` for 10 minutes (configurable
via `file_cache_ttl_s` in [config.json](config.json)). On a cache hit no
HTTP POST is issued. Cache is cleared whenever
`POST /api/abstract-triggers` rewrites the config.

## Config files

| File | Purpose |
|---|---|
| [config.json](config.json) | Gateway runtime knobs (sync, abstract track index, UI port, cache TTL) |
| [abstract_triggers.json](abstract_triggers.json) | Operator-editable abstract trigger registry — source of truth |

`abstract_triggers.json` shape:

```json
{
  "version": 1,
  "abstract_triggers": {
    "AbstractTriggerFoo": {
      "associated_device_id": "MURMURA-001",
      "mappings": [
        { "upstream": "RedButton.Button_1", "file_path": "/sdcard/sound1.wav" },
        { "upstream": "RedButton.Button_2", "file_path": "/sdcard/sound2.wav" }
      ]
    }
  }
}
```

The `associated_device_id` field is a **UI hint** only — it sources the
file dropdown via that device's `GET /api/files`. The runtime fans out to
whichever devices actually subscribed to the abstract name.

## UI (port 5101)

- **GET `/`** — dashboard with a card per abstract trigger and a live
  event log
- **GET / POST `/api/abstract-triggers`** — read or replace the registry
  (POST is atomic; full config object expected)
- **GET `/api/devices`** — connected devices and their subscriptions
- **GET `/api/device/<id>/files`** — proxy the device's `GET /api/files`
- **GET `/api/upstream-triggers`** — proxy the upstream `/triggers`
  (with On/Off → OneShot relabeling)
- **GET `/api/log?limit=N`** — recent ring-buffer events (subscribe + fire)
- **GET `/api/log/stream`** — Server-Sent Events for live log

## Status endpoint (port 4001)

Same as mur-gateway:

```bash
curl http://localhost:4001/status | python3 -m json.tool
curl http://localhost:4001/triggers
```

The `/status` payload adds an `abstract_triggers` list and `abstract_track_index`.

## Replacing mur-gateway

The two services use the same ports and have identical upstream/downstream
behavior for non-abstract triggers, so swapping is a single systemd flip
(see [SYSTEMD_INSTALL.md](SYSTEMD_INSTALL.md)).

## Requirements

- Python 3.10+
- aiohttp ≥ 3.9
