# Mur Protocol Specification

## Overview

The Mur Protocol is a JSON-over-TCP protocol used between Murmura ESP32 devices and the Mur Gateway. It is newline-delimited: each message is a single JSON object followed by `\n`.

Devices initiate outbound TCP connections to the Mur Gateway. The connection is persistent — devices reconnect on failure.

The protocol carries optional time fields used for synchronized multi-device playback. See [SYNC_DESIGN.md](SYNC_DESIGN.md) for the design rationale, prior art, and validation procedure; this document is the canonical wire-format spec.

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

**Must be the first message sent after connecting.** Identifies the device and seeds the gateway's TSF map.

```json
{"type": "announce", "id": "MURMURA-001", "tsf_us": 12320420544427881}
```

| Field    | Type   | Required | Description |
|----------|--------|----------|-------------|
| `type`   | string | yes      | `"announce"` |
| `id`     | string | yes      | Device identifier (e.g. unit ID from `/sdcard/unit_id.txt`) |
| `tsf_us` | uint64 | no       | Current WiFi TSF reading in microseconds. Seeds the gateway's `TsfMap`; gateway also pulls TSF on its own cadence via `tsf_query`. See [SYNC_DESIGN.md](SYNC_DESIGN.md). |

### subscribe

Registers the device to receive trigger events matching the listed names. Can be sent at any time after `announce`.

**Authoritative replacement:** Each `subscribe` message is treated as the device's complete current set of trigger subscriptions. The gateway clears any prior subscriptions for the connection, then installs the new list. Devices should always send their full current list — to remove a trigger, omit it from the next `subscribe`. (`unsubscribe` remains supported for explicit removals but is not required when the device's config changes.)

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

### tsf_reply

Sent in response to a `tsf_query` from the gateway. Carries the device's current TSF reading in microseconds. The gateway uses these samples to keep its ISO ↔ TSF map fresh. See [SYNC_DESIGN.md](SYNC_DESIGN.md).

```json
{"type": "tsf_reply", "tsf_us": 12320420544427881}
```

| Field    | Type   | Required | Description |
|----------|--------|----------|-------------|
| `type`   | string | yes      | `"tsf_reply"` |
| `tsf_us` | uint64 | yes      | Current WiFi TSF reading in microseconds. `0` if WiFi is not associated; gateway treats `0` as "no sample". |

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

### tsf_query

Solicits a `tsf_reply` from a device. The gateway sends this periodically (cadence is gateway-side config, see [SYNC_DESIGN.md](SYNC_DESIGN.md)).

```json
{"type": "tsf_query"}
```

| Field  | Type   | Required | Description |
|--------|--------|----------|-------------|
| `type` | string | yes      | `"tsf_query"` |

### Trigger Event

Trigger events delivered by the gateway to subscribed devices.

```json
{"name": "RedButton.Button_1", "value": "On", "id": 123, "timestamp": "2026-03-11T10:30:00", "target_tsf_us": 12320420545000000, "volume": 80}
```

| Field           | Type   | Required | Description |
|-----------------|--------|----------|-------------|
| `name`          | string | yes      | Trigger name (e.g. `"DeviceName.TriggerName"`) |
| `value`         | string | yes      | `"On"`, `"Off"`, `"1"`, `"0"`, or discrete/continuous value |
| `id`            | int    | no       | Unique event ID (for deduplication) |
| `timestamp`     | string | no       | ISO 8601 timestamp (event creation time, for debugging — **not** used for scheduling) |
| `target_tsf_us` | uint64 | no       | Absolute TSF deadline in microseconds. If present, the device schedules the action via `mur_scheduler`. If absent, the device fires immediately. See [SYNC_DESIGN.md](SYNC_DESIGN.md). |
| `volume`        | int    | no       | 0-100 level for the track(s) this event **starts**, applied at the same deadline as the start. Absent means "leave the current level alone". Clamped to 0-100 on the device; 0 is mute (-60 dB). See "Per-event volume" below. |

**Outer-protocol time fields (Trigger Server → gateway only):** the upstream Trigger Server may send `delta_ms` (int) or `iso_time` (ISO 8601 string) instead of `target_tsf_us`. The gateway translates these into `target_tsf_us` before forwarding. The three time fields are mutually exclusive — events with multiple are dropped with a logged warning. Events with no time field default to "now" semantics; the gateway adds an implicit fanout delay (default 100 ms, configurable) when more than one subscriber would receive it.

## Behavior Notes

- **Connection ownership:** The device initiates the TCP connection. The gateway never connects to devices.
- **Reconnection:** If the connection drops, the device should reconnect with exponential backoff (e.g. 1s, 2s, 4s, capped at 30s). On reconnect, the device must re-send `announce` and `subscribe`.
- **Subscription scope:** The gateway only forwards events whose `name` field exactly matches a subscribed trigger name.
- **Value filtering for On/Off → OneShot conversion:** The gateway drops events with falsy values (`Off`, `off`, `0`, `false`) and strips truthy values (`On`, `on`, `1`, `true`) from forwarded events so the dispatch is shaped like a real OneShot. Discrete and Continuous values (scene names, integers, floats) pass through unchanged. See "Trigger type translation" below.
- **Graceful disconnect:** Either side may close the TCP connection at any time. The gateway cleans up subscriptions on disconnect.
- **Multiple subscriptions:** If multiple devices subscribe to the same trigger, all receive the event.
- **Encoding:** UTF-8. Messages are newline-delimited (`\n`). Carriage returns (`\r`) are ignored.

## Per-event volume

`volume` exists so a conducted playlist can carry a per-entry level trim
(see [ENSEMBLES.md](ENSEMBLES.md)). Semantics:

- **Applied only on the per-track START path.** The device enqueues `SET_VOLUME`
  immediately before `START_TRACK` on the same FIFO audio queue, so both take effect at
  `target_tsf_us`. It is never applied on a STOP, nor to a track the event does not start
  (disabled, no file, or value mismatch).
- **Ignored by the scene trigger and the per-scene button trigger.** Both consume the
  event before per-track matching. A scene change is not a track start.
- **Composes as the `track` term** of `device_volume x scene.global_volume x track.volume`
  (see [HTTP_API.md](HTTP_API.md)), so per-device trim stays with `device_volume`. Note
  the terms have different slopes: a track volume of 50 is -6 dB, while a global or device
  volume of 50 is about -12 dB.
- **Runtime state only.** It is not written to `scenes.json`. `GET /api/device` reports the
  live level while `GET /api/scenes` keeps reporting the scene's stored `volume`; the next
  scene activation or reboot resets the track to that stored value, after which the next
  event re-applies the trim.
- **Identical for every subscriber.** The gateway serializes one event and sends the same
  bytes to all of them, so a per-event volume cannot vary per device. That is precisely
  why per-device trim has to be `device_volume`.
- The gateway does not interpret `volume`; it rides through as an unknown field.

**Unknown fields generally.** Neither gateway validates the event schema. Both rebuild the
forwarded event as a shallow copy, removing only `iso_time` and `delta_ms`, so any other
key reaches the device verbatim. Firmware reads `name`, `value`, `target_tsf_us` and
`volume` and ignores the rest — which is what makes an additive field safe against older
firmware.

**Line length budget.** The device reassembles into a 512-byte line buffer and **silently
discards the overflow**, which then fails to parse as JSON. A realistic event is about
140-180 bytes, so there is roughly 3x headroom — but a new field plus a long trigger name
eats into it. Check this before extending the schema.

## Trigger Type Translation

Upstream triggers (Haven Trigger Server) come in four types: `On/Off`, `OneShot`, `Discrete`, `Continuous`. Mur device firmware supports two: `On/Off` (momentary play-while-On) and `OneShot` (fire-and-play-to-end).

For the current deployment, the gateway hides `On/Off` from clients and presents those triggers as `OneShot` instead. This affects two surfaces:

1. **Trigger listing** (`GET /triggers` on the gateway's status port): every upstream `On/Off` trigger is returned with `type: "OneShot"`. No `type: "On/Off"` entries appear in the response. Real upstream OneShot, Discrete, and Continuous triggers pass through unchanged. Filtering the result by `On/Off` therefore returns nothing; filtering by `OneShot` returns the merge of real-OneShot and relabeled-from-On/Off triggers.

2. **Trigger event dispatch**: at fan-out time the gateway looks at the event's `value` field:
   - Falsy (`Off`, `off`, `0`, `false`) → drop. The event is not forwarded to any subscriber.
   - Truthy (`On`, `on`, `1`, `true`) → strip the `value` field, forward. The event arrives at devices shaped like a real OneShot (no `value`).
   - Anything else (scene names, integers, floats) → pass through unchanged.

The `subscribe` message is unaffected and remains a list of names only.

> **Future improvement (firmware change required, not field-deployable today):**
> The Murmura device philosophy is that the firmware should support `On/Off` triggers correctly and additionally allow the user to opt a given trigger into "treat the `On` event as a OneShot" behavior — so the choice belongs on the device, per track, not in the gateway. The current deployment can't accept firmware updates, so as a workaround the gateway hides `On/Off` from clients and converts events on the wire. When firmware can next be updated, the right shape is:
> - Firmware exposes both `On/Off` and `OneShot` trigger types to mur-config-server.
> - Per-track config gains an `on_as_oneshot` boolean (or equivalent) so users can pick momentary vs. fire-and-forget per track.
> - Gateway returns to pure passthrough; the relabeling and value filtering described above can be removed.

## Default Ports

| Port | Purpose |
|------|---------|
| 4000 | Mur Gateway — device connections |
| 4001 | Mur Gateway — HTTP status endpoint |
