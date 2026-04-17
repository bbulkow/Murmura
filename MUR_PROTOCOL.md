# Mur Protocol Specification

## Overview

The Mur Protocol is a JSON-over-TCP protocol used between Murmura ESP32 devices and the Mur Gateway. It is newline-delimited: each message is a single JSON object followed by `\n`.

Devices initiate outbound TCP connections to the Mur Gateway. The connection is persistent — devices reconnect on failure.

## Connection Flow

```
Device                          Mur Gateway
  |                                  |
  |--- TCP connect ----------------->|
  |                                  |
  |--- {"type":"announce",...} ----->|   (1) Device identifies itself
  |                                  |
  |<-- {"type":"welcome",...} ------|   (2) Gateway acknowledges
  |                                  |
  |--- {"type":"subscribe",...} --->|   (3) Device subscribes to triggers
  |                                  |
  |--- {"type":"get_scene"} ------->|   (4) Device asks for current scene
  |                                  |
  |<-- {"type":"scene","value":X} --|   (5) Gateway answers from cache
  |                                  |
  |          ... time passes ...     |
  |                                  |
  |<-- {"name":"X","value":"On"} ---|   (6) Trigger event delivered
  |                                  |
  |--- {"type":"get_scene"} ------->|   (7) Periodic re-pull (every 5 s)
  |<-- {"type":"scene","value":Y} --|
  |                                  |
```

## Messages: Device → Gateway

### announce

**Must be the first message sent after connecting.** Identifies the device.

```json
{"type": "announce", "id": "MURMURA-001"}
```

| Field  | Type   | Required | Description |
|--------|--------|----------|-------------|
| `type` | string | yes      | `"announce"` |
| `id`   | string | yes      | Device identifier (e.g. unit ID from `/sdcard/unit_id.txt`) |

### subscribe

Registers the device to receive trigger events matching the listed names. Can be sent at any time after `announce`. Sending multiple `subscribe` messages is additive — triggers accumulate.

```json
{"type": "subscribe", "triggers": ["RedButton.Button_1", "Dial.Number"]}
```

| Field      | Type     | Required | Description |
|------------|----------|----------|-------------|
| `type`     | string   | yes      | `"subscribe"` |
| `triggers` | string[] | yes      | List of trigger names to subscribe to |

### unsubscribe

Removes subscriptions for the listed trigger names.

```json
{"type": "unsubscribe", "triggers": ["Dial.Number"]}
```

| Field      | Type     | Required | Description |
|------------|----------|----------|-------------|
| `type`     | string   | yes      | `"unsubscribe"` |
| `triggers` | string[] | yes      | List of trigger names to unsubscribe from |

### get_scene

Asks the gateway for the current active scene. The gateway answers with a
`scene` message (see below). Devices send this after `subscribe` on every
(re)connect and on a short periodic timer (default 5 s) as a reliability loop
against lost `SceneChange` trigger events.

```json
{"type": "get_scene"}
```

| Field  | Type   | Required | Description |
|--------|--------|----------|-------------|
| `type` | string | yes      | `"get_scene"` |

## Messages: Gateway → Device

### welcome

Sent in response to a valid `announce` message.

```json
{"type": "welcome", "gateway": "mur-gateway", "version": "1.0"}
```

| Field     | Type   | Description |
|-----------|--------|-------------|
| `type`    | string | `"welcome"` |
| `gateway` | string | Gateway identifier |
| `version` | string | Protocol version |

### scene

Response to a `get_scene` query. Carries the current active scene name (or
`null` if the gateway has no value yet).

```json
{"type": "scene", "value": "night"}
{"type": "scene", "value": null}
```

| Field   | Type            | Description |
|---------|-----------------|-------------|
| `type`  | string          | `"scene"` |
| `value` | string or null  | Active scene name, or `null` if unknown |

The gateway maintains a short-TTL cache (default 30 s) of the current scene.
The cache is:
- **Primed at startup** from the Scene Service via `GET /api/scenes/active`.
- **Updated immediately** whenever a `SceneChange` trigger event arrives from
  the upstream Trigger Server (push path).
- **Lazily refreshed** from the Scene Service on a `get_scene` query if the
  cached value is older than the TTL. Refreshes are serialized so concurrent
  device queries collapse into at most one upstream HTTP call.

Device behavior on receipt:
- `value` is a known scene on this device → activate it (idempotent — if the
  same scene is already active, the device silently ignores the message).
- `value` is an unknown scene name → fall back to the device's `default_scene`.
- `value` is `null` → keep whatever scene is currently active.

### Trigger Event

Trigger events delivered by the gateway to subscribed devices.

```json
{"name": "RedButton.Button_1", "value": "On", "id": 123, "timestamp": "2026-03-11T10:30:00"}
```

| Field       | Type   | Description |
|-------------|--------|-------------|
| `name`      | string | Trigger name (e.g. `"DeviceName.TriggerName"`) |
| `value`     | string | `"On"`, `"Off"`, `"1"`, `"0"`, or discrete/continuous value |
| `id`        | int    | Unique event ID (for deduplication) |
| `timestamp` | string | ISO 8601 timestamp (for debugging) |

## Behavior Notes

- **Connection ownership:** The device initiates the TCP connection. The gateway never connects to devices.
- **Reconnection:** If the connection drops, the device should reconnect with exponential backoff (e.g. 1s, 2s, 4s, capped at 30s). On reconnect, the device must re-send `announce` and `subscribe`.
- **Subscription scope:** The gateway only forwards events whose `name` field exactly matches a subscribed trigger name.
- **No filtering by value:** The gateway forwards all values for a subscribed trigger (On, Off, discrete values, etc.). The device decides what to act on.
- **Graceful disconnect:** Either side may close the TCP connection at any time. The gateway cleans up subscriptions on disconnect.
- **Multiple subscriptions:** If multiple devices subscribe to the same trigger, all receive the event.
- **Encoding:** UTF-8. Messages are newline-delimited (`\n`). Carriage returns (`\r`) are ignored.

## Default Ports

| Port | Purpose |
|------|---------|
| 4000 | Mur Gateway — device connections |
| 4001 | Mur Gateway — HTTP status endpoint |
