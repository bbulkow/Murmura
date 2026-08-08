# Mur Conductor

Makes several MURs play the same material **together**: starting together from a
cold boot, staying together for hours, and letting a device that reboots alone
rejoin cleanly. Requires no firmware changes to the audio path.

The playlist lives here, on the Pi — not on the devices. That is deliberate:
device-local playlist advance is driven by end-of-file detection, which happens
at a slightly different moment on each device and cannot stay aligned across a
fleet. See "Relationship to device-local playlists" below.

## The problem this solves

Three separate things break "just make them all loop the same file":

1. **Loops drift.** Looping in the firmware is not a seamless pipeline loop —
   each iteration is a full teardown and rebuild with a variable software gap
   (`murmura.c`). Four devices started in perfect sync random-walk apart.
2. **Boot is unsynchronized.** Devices come up whenever they come up. On a cold
   start the MURs typically reach the gateway before the Pi has finished booting
   its services.
3. **A solo reboot cannot be fixed by the device.** A rebooted device can learn
   *what* to play (the gateway's `get_scene` backstop) but has no way to know
   *when* the rest of the group is in its material.

All three have the same fix: something with a fleet-wide view emits the beat.

## How it works

```
[mur-conductor]  :5002 registration + ingest, :4002 status/admin
      | (TCP to the gateway's upstream port, newline-delimited JSON events)
[mur-gateway | mur-abs-gateway]  :4000 devices, :4001 status
      | (devices connect outbound, announce + subscribe)
[MUR devices]                    [mur-config-server :8765 - UI, file ops]
```

The conductor is a **metronome**. At every playlist entry boundary (a
"downbeat") it emits one trigger event. The gateway stamps that event with an
absolute WiFi-TSF deadline (`target_tsf_us`) and fans the **same** deadline out
to every subscribed device; each device defers execution to that deadline via
its on-device scheduler. Devices therefore restart in lockstep to within the
scheduler's precision (~5-10 ms) — and the conductor never needs an accurate
clock of its own. See `SYNC_DESIGN.md` for the full rationale.

Three properties fall out of this for free:

- **Silent until the downbeat.** The ensemble track is in `trigger` mode, and
  `config_apply` never starts audio for trigger-mode tracks — it only enables
  them. So a device boots silent and armed. A device that reboots alone makes no
  sound at all until it rejoins the ensemble; it never plays out of phase.
- **Drift correction.** Every entry boundary re-aligns the whole fleet, so drift
  cannot accumulate beyond one entry.
- **Fast cold start.** The conductor waits for the fleet (the *readiness gate*)
  and then fires immediately, rather than making everyone wait out an entry.
  When everything including the Pi power-cycles together, the first unified
  downbeat lands within seconds of the Pi settling.

Note that because every beat is deferred by the same gateway-side fanout delay,
that delay cancels out of the *spacing* between beats. Conductor-to-gateway
network jitter shifts the whole grid slightly; it never affects device-to-device
alignment, which is the thing you can actually hear.

## Two invariants this service respects

Both are documented in `SYNC_DESIGN.md` and both are enforced or observed here:

- **Never fire `SceneChange`.** That name is reserved for the scene service and
  is special-cased inside the gateway. Config validation rejects it outright.
- **Never give the gateway an inbound injection endpoint.** The conductor
  reaches devices by *being* the upstream trigger source, not by poking the
  gateway. In this Murmura-only deployment there is no Haven Trigger Server, so
  the conductor implements that role itself (`POST /api/register`, then it
  connects back to the gateway and pushes events).

## Configuration

Everything lives in `config.json` next to `mur_conductor.py`. `SIGHUP`
(`systemctl reload mur-conductor`) reloads it; port and `gateway_status_url`
changes need a real restart.

```json
{
  "listen_port": 5002,
  "status_port": 4002,
  "gateway_status_url": "http://127.0.0.1:4001/status",
  "health_poll_interval_s": 10,
  "device_http_timeout_s": 3.0,
  "groups": [
    {
      "name": "mainroom",
      "enabled": true,
      "trigger_name": "EnsembleMain",
      "scene_name": "ensembleA",
      "track": 0,
      "expected_device_ids": ["MUR-001", "MUR-002", "MUR-003", "MUR-004"],
      "readiness_timeout_s": 120,
      "prep_lead_ms": 8000,
      "loop_playlist": true,
      "playlist": [
        {"file": "/sdcard/dawn.wav", "duration_ms": 212000, "gap_ms": 0},
        {"file": "/sdcard/tide.wav", "duration_ms": 900000, "gap_ms": 2000}
      ]
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `trigger_name` | The trigger every member subscribes to. Must be unique per group and must not be `SceneChange`. |
| `scene_name` | The ensemble scene on each device — the conductor patches this scene's track. |
| `track` | Which track (0-2) carries the ensemble material. |
| `expected_device_ids` | Group membership. Drives the readiness gate and health reporting. |
| `readiness_timeout_s` | How long to wait for the full fleet before starting anyway (with a warning). |
| `prep_lead_ms` | How far ahead of a downbeat to push the next entry's filename to the devices. |
| `duration_ms` | How long the entry's audio runs. The next downbeat is `duration_ms + gap_ms` later. |
| `gap_ms` | Deliberate silence after this entry before the next downbeat. |

`file` names are logical: each device plays its own `/sdcard/<name>` copy, so
per-device stems work by giving each device a different file under the same name.

**Choosing `duration_ms`.** The next downbeat cuts in at its deadline whatever
the audio is doing: set it too long and the entry gets truncated, too short and
you get silence before the next entry. Aim to match the real file duration. The
config-server UI derives it from the WAV header for you.

## Endpoints

Ingest / upstream role (`listen_port`, default 5002):

| Route | Purpose |
|---|---|
| `POST /api/register` | The gateway registers here; the conductor connects back to it. |
| `GET /api/registrations` | Which gateways are registered and connected. |
| `GET /api/triggers` | The trigger names this conductor drives (keeps the config-server dropdowns working). |
| `POST /api/trigger-event` | Manual injection for testing: `{"name": "...", "value": "..."}`. Does not disturb any group's timeline. |

Status / admin (`status_port`, default 4002):

| Route | Purpose |
|---|---|
| `GET /status` | Everything: gateway link, TSF map state, and per-group readiness, members, current entry, beat count, next-downbeat ETA. |
| `POST /api/groups/<name>/playlist` | Replace a playlist. Applied at the next boundary, so no entry is cut off mid-file. Persisted to `config.json`. |
| `POST /api/groups/<name>` | Set `enabled`, `expected_device_ids`, or `loop_playlist`. |

## Device setup (once per device)

Create the ensemble scene, make it the default so boot lands on it, and save:

```bash
DEV=192.168.13.42
curl -X POST http://$DEV/api/scene  -H 'Content-Type: application/json' \
     -d '{"action":"create","name":"ensembleA"}'
curl -X POST http://$DEV/api/scenes -H 'Content-Type: application/json' -d '{
  "ensembleA": {"global_volume": 75, "tracks": [
    {"track":0,"mode":"trigger","trigger_type":"OneShot","trigger_name":"EnsembleMain",
     "active":true,"file_path":"/sdcard/dawn.wav","volume":100},
    {"track":1,"active":false},
    {"track":2,"active":false}]}}'
curl -X POST http://$DEV/api/scene  -H 'Content-Type: application/json' \
     -d '{"action":"set_default","name":"ensembleA"}'
curl -X POST http://$DEV/api/config/save
```

`setup_ensemble.py` in this directory does all of that for a whole group at once.

Two deliberate choices worth knowing:

- **Leave `synchronized` false.** That flag gates *scene activation*, but this
  design never re-activates the scene after boot — the sync-critical action is
  the per-track trigger, which the flag does not touch. Setting it true on the
  default scene makes the firmware log an error on every boot and blocks the
  `get_scene` backstop from restoring the scene. No benefit, real noise.
- **Leave `late_policy` at `play`.** For an ensemble, one device restarting
  slightly late beats one device staying silent for a whole entry.

### Why the prep patch always sends `active: true`

This is load-bearing and easy to break. In `scene_apply_patch`
(`main/scene_manager.c`), changing `file_path` on the **active** scene restarts
the track immediately *if it is currently playing*. That would cut to the next
entry `prep_lead_ms` early — and at a different moment on each device, since each
device's POST lands at a slightly different time. Exactly the failure this
service exists to prevent.

Supplying `active` in the patch routes it into the trigger-mode branch instead,
which only re-enables the track (an inert flag set — see
`AUDIO_ACTION_ENABLE_TRACK` in `main/murmura.c`) and never reaches the restart.
The new `file_path` is still stored for the next trigger to pick up. So the prep
window is safe regardless of how it lines up with playback.

## Verifying without hardware

Run the real gateway with fake devices. **The thing to look for is an identical
`target_tsf_us` on every device for each downbeat** — that shared deadline is
what makes real devices start together. If the values differ, the fan-out is not
sharing a deadline and no amount of firmware tuning will align them.

```bash
# 1. Conductor first, so the gateway's startup registration lands immediately
#    instead of waiting out its 30 s re-register loop.
python mur_conductor.py

# 2. The real gateway, pointed at the conductor
cd ../mur-gateway && python mur_gateway.py --trigger-host 127.0.0.1 --trigger-port 5002

# 3. Two fake devices. Distinct source IPs are REQUIRED: the gateway evicts
#    connections that share a peer IP, so two devices on 127.0.0.1 kick each
#    other off forever. Real MURs each have their own address.
python fake_device.py --id MUR-001 --triggers EnsembleMain --source-ip 127.0.0.2
python fake_device.py --id MUR-002 --triggers EnsembleMain --source-ip 127.0.0.3

# 4. Watch
curl -s http://127.0.0.1:4002/status | python -m json.tool
```

Expect: the group sits in `waiting_readiness` until both devices subscribe, then
fires its first downbeat within about a second; both fake devices print the same
`target_tsf_us` with a lead of roughly `fanout_delay_ms` (2500 ms by default);
beat spacing matches the configured entry spans. The prep POSTs will fail
against fake devices (they serve no HTTP) — that is expected and logged.

## Verifying on hardware

- **Cold start:** power-cycle everything including the Pi. Devices should be
  silent, then all start together within seconds of the Pi settling.
- **Solo reboot:** power-cycle one device mid-entry. It should stay silent, then
  join at the next entry boundary.
- **Entry transitions:** a single attack across all speakers, no flam or chorus.
  Check by ear and with a phone recording placed between two speakers.
- **Graceful stop:** `systemctl stop mur-conductor` — devices finish the current
  entry and fall silent together.
- **Logs:** the gateway should show an identical `target_tsf_us` per beat with no
  TSF-jitter or stale-map warnings; device serial should show
  `Trigger 'EnsembleMain' matched track 0` each beat and no `late event by N us`
  warnings in steady state.
- **Soak** for several hours including your longest entry, listening at
  boundaries.

## Relationship to device-local playlists

An earlier plan (a since-removed `PLAN_playlist.md`) described device-local
playlists, where a track's `file_path` names a `.playlist.json` and the firmware
advances on end-of-file. For **ensemble** use that approach cannot work: EOF
happens at a slightly different instant on each device, so cursors drift apart
with no mechanism to re-align them. This service keeps the playlist on the Pi
and makes every entry boundary a synchronized event instead.

The two are not mutually exclusive — device-local playlists remain useful for a
standalone MUR. They should not be combined on the same track.

## Known limitations

- **No mid-entry join.** A device that reboots into a 15-minute entry waits it
  out in silence; there is no seek or playback-position API to splice it in.
- **Not sample-accurate.** The floor is the on-device scheduler (~5-10 ms) plus
  I2S buffer alignment. `playback_offset_us` per device is the existing knob for
  speaker-distance compensation.
- **No hard stop.** The gateway drops falsy trigger events, so On/Off-style
  "stop now" is unavailable. Stopping means stopping the conductor and letting
  the current entry finish.
- **Timeline is not persisted.** A conductor restart re-runs the readiness gate
  and starts the playlist from entry 0.
- **Intra-entry drift** on a long entry: independent crystals give order-10 ms by
  the end of a 15-minute file, corrected at the next boundary.
- **Gateway recovery takes up to 30 s.** After a conductor restart the gateway
  only re-registers when it notices its upstream is down, on a 30 s loop.
