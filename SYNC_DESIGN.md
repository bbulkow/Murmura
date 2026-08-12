# Synchronized Multi-MUR Playback — Design

This doc explains how multiple MUR devices play the same sound (or change scene, or any other action) at the same instant. It covers the underlying clock, the architecture, the protocol additions, and how to validate the system on a new install.

## The problem

When a trigger fires (a button press, a sensor event, an upstream `SceneChange`), we want every subscribed MUR to act at the same wall-clock instant — close enough that a listener can't tell two devices apart.

The naive approach — each device fires immediately on receive — doesn't work. Per-device receive latency is dominated by WiFi airtime, retries, and OS scheduling, all of which vary by tens of milliseconds device-to-device. That's audible as flam, slap-back, or chorusing.

We need a shared clock that all MURs read the same value of, at the same real-world instant, with low jitter.

## Prior art and the pattern we're implementing

Using TSF (or, equivalently, MAC-layer beacon timestamps) as a shared microsecond clock for synchronized audio playback is **established prior art**, not novel:

- The Wi-Fi Alliance's **Wi-Fi TimeSync** (2017) standardizes precisely this idea, building on **IEEE 802.11mc**.
- **Sonos** has long used MAC-layer beacon timestamps as the cross-device reference for multi-room audio.

Our work is an independent parallel implementation of the same pattern in our stack (ESP32 / ESP-IDF / ESP-ADF / a Python gateway). Worth documenting not because the idea is new, but because anyone touching this code needs to know which pattern we're implementing, why we chose it over the alternatives, and the project-specific knobs and failure modes.

We considered and rejected:
- **Application-level NTP** — adds a slewing layer, an extra dependency, and tens-of-ms-class accuracy. Worse than what TSF gives us for free.
- **PTP / IEEE 1588** — designed for this problem, but requires hardware support and / or PTP-aware switches. Overkill for our installation.
- **Ad-hoc UDP broadcast sync** — would re-invent what TSF already does correctly.

We did **not** implement the full Wi-Fi TimeSync protocol. We use TSF directly, which is simpler and sufficient for our latency target (sub-millisecond, not sample-accurate).

## The measurement

`main/tsf_drift_test.c` samples `(esp_timer_get_time(), esp_wifi_get_tsf_time(WIFI_IF_STA))` once every 5 s on a connected MUR and logs the raw delta plus drift since first sample. On the test bench (LyraT board, residential AP):

```
I (60762) TSF_DRIFT: tsf=12320420544427881  mono=59728859  delta=12320420484699022  step=+233  drift_since_start=-543
I (65762) TSF_DRIFT: tsf=12320420549427551  mono=64728859  delta=12320420484698692  step=-330  drift_since_start=-873
I (70762) TSF_DRIFT: tsf=12320420554427522  mono=69728877  delta=12320420484698645  step=-47  drift_since_start=-920
I (75762) TSF_DRIFT: tsf=12320420559427732  mono=74728859  delta=12320420484698873  step=+228  drift_since_start=-692
I (80762) TSF_DRIFT: tsf=12320420564427404  mono=79728858  delta=12320420484698546  step=-327  drift_since_start=-1019
I (85762) TSF_DRIFT: tsf=12320420569427617  mono=84728859  delta=12320420484698758  step=+212  drift_since_start=-807
I (90762) TSF_DRIFT: tsf=12320420574427564  mono=89728859  delta=12320420484698705  step=-53  drift_since_start=-860
```

Headline: `delta` (raw `tsf − mono`) is bounded — oscillating in a ~500 µs band rather than trending. That's TSF being slewed to the AP's beacons; the local crystal would otherwise walk away by tens of ppm. Sub-millisecond stability over 30 s is plenty for our needs.

The test task remains in the firmware as a permanent observability tool — leave it on, watch the log on a new AP / hardware combination, confirm `delta` doesn't trend before relying on TSF for sync.

## Architecture

```
[Trigger Server] --(JSON +/- timestamp/delta_ms/iso_time)--> [mur_gateway]
                                                                  |
                                                 [TsfMap: iso ↔ tsf_us]
                                                                  |
                          +-------------+-----------------+--------+----+
                          |             |                 |             |
                    <target_tsf_us> <target_tsf_us>  <target_tsf_us>   ...
                          v             v                 v             v
                        [MUR 1]       [MUR 2]           [MUR 3]      [MUR N]
                          |             |                 |             |
            mur_scheduler picks deadline → dispatch_event → audio_control_queue
```

Three components, three responsibilities:

1. **Trigger Server (upstream)** doesn't know about TSF. It can ask for an action at "now", at a future ISO instant, or at a relative offset (`delta_ms`). If it doesn't ask for a time, the gateway picks one.

2. **mur_gateway** owns the ISO ↔ TSF translation. It maintains a `TsfMap` keyed by samples it pulls from MURs (and seeded from each MUR's `announce`). It rewrites incoming trigger events to carry an absolute `target_tsf_us` and forwards to subscribed MURs.

3. **MUR (firmware)** parses `target_tsf_us` from incoming events. If the deadline is in the future, the action goes through `mur_scheduler` (a min-heap keyed on TSF microseconds). If the deadline has passed beyond a small grace window, the per-MUR `late_policy` decides whether to fire-late-with-warning or drop.

## Wire format

Canonical schema lives in [MUR_PROTOCOL.md](MUR_PROTOCOL.md). Rationale-only summary:

**Outer (Trigger Server → mur_gateway)** — fields on the trigger event JSON, all optional, mutually exclusive:
- `delta_ms` (int) — fire this many ms after gateway receive.
- `iso_time` (string, ISO 8601) — fire at this absolute wall-clock instant.
- `target_tsf_us` (int) — fire at this absolute TSF (test/debug passthrough).
- absent — "now" (gateway picks fanout delay if >1 subscriber; see below).

**Inner (mur_gateway → MUR)** — only `target_tsf_us` (uint64) and the `tsf_query` / `tsf_reply` /  augmented `announce` exist on the wire. ISO never reaches the MUR.

## Configuration knobs

### Gateway: `mur-gateway/config.json`

Edit on-site, restart the service. Config file fields override built-in defaults; CLI args (existing) are not affected by this file.

| Field | Default | What it tunes |
|---|---|---|
| `fanout_delay_ms` | 2500 | Implicit "now" + multi-MUR scheduling — set above worst-case end-to-end delivery latency on your network. Production WiFi installs commonly see 1–2 s HTTP/TCP delays under load, so 2500 ms gives ~500 ms–1.5 s of margin. Drop it on a clean dev LAN if you want snappier "now" cues; raise it if you see late-event drops/warnings. |
| `tsf_query_interval_s` | 30 | How often to refresh the TSF map. Lower = fresher, more LAN traffic. The TSF test showed sub-ms drift over 30 s. |
| `tsf_query_devices_count` | 3 | Sample size per pull cycle. Lets the gateway compute pairwise jitter for diagnostics. |
| `tsf_jitter_warn_us` | 1000 | Pairwise TSF disagreement threshold for a logged warning. |
| `tsf_map_max_age_s` | 120 | Oldest acceptable canonical sample. Beyond this we still translate but log a warning. |

### MUR: `late_policy` on `POST /api/device`

Per-device. Persisted in `/sdcard/track_config.json` (not NVS — NVS is reserved for WiFi bootstrap creds).

- `"play"` (default) — fire late events anyway. Best when one missing device is worse than three slightly-late devices (e.g. a fleet where one MUR happens to receive a re-tx well past the deadline).
- `"drop"` — discard late events. Best when synchronization is contractual: rather have an absent unit than a flam.

The threshold is the scheduler's grace window (`LATE_GRACE_US` = 10 ms). Scheduled events that arrive within 10 ms of their deadline still go through the scheduler (it fires immediately).

### MUR: `playback_offset_us` on `POST /api/device`

Per-device tuning offset. Signed int32 microseconds, default 0. Applied to every scheduled event's `target_tsf_us` *before* the lateness check, so the on-time / late / drop decision reflects the adjusted deadline.

- **Positive** = fire later. Compensates for a speaker physically closer to the listener (where its sound would otherwise arrive too early).
- **Negative** = fire earlier. Compensates for a speaker further from the listener (where its sound would otherwise arrive too late).
- Speed of sound is ≈ 343 m/s, so 1 m of air-path difference ≈ 2.9 ms (2900 µs).

Persisted in `/sdcard/track_config.json`. Set on each MUR during install; tune by ear or by measurement (see "Validating on a new install" below for the multi-MUR sync test). Note that the offset only applies when the gateway has set a `target_tsf_us` on the event — for "now" events with a single subscriber, the gateway passes through unscheduled and the offset is not applied.

A pathologically large negative offset can push the adjusted deadline into the past and trigger the `late_policy`. That's intentional: if you've configured -2 s on a MUR, you've gone too far.

## Synchronized scenes

A scene change is the headline use case for cross-device synchronization — when the show changes scene, every MUR must change at the same instant or the mismatch is glaringly audible. Scenes therefore carry a `synchronized: bool` flag (default `false`). When set, the device enforces that the scene can only be entered through paths that *guarantee* cross-device timing.

### The four runtime activation paths

| Path | Source | `synchronized` behavior |
|---|---|---|
| 1. `SceneChange` trigger event matching `scene_trigger_name` | `dispatch_or_defer` → deferred dispatch | **Allowed** — gateway adds fanout delay, all subscribed MURs activate at the same `target_tsf_us` |
| 2. Per-scene `button_trigger` event | `dispatch_or_defer` → deferred dispatch | **Allowed** — same mechanism as path 1 |
| 3. `get_scene` reply from gateway (5 s reliability poll) | direct call | **Refused** with `ESP_ERR_INVALID_STATE` and an `ESP_LOGW` |
| 4. `POST /api/scene` `action=activate` (admin REST) | direct call | **Refused** — HTTP 409 Conflict with `"Scene is synchronized; activate via SceneChange trigger"` |
| 5. Boot in `app_main` (default_scene from scenes.json) | direct call | **Allowed but logs `ESP_LOGE`** — a synchronized default is a configuration error |
| 6. After `POST /api/config/load` | direct call | Same as boot — allowed with error log if synchronized |

### Why path 3 is *correctly* refused

The case that makes this rule earn its keep:

1. Operator updates the scene service to `nightShow`, which is a synchronized scene.
2. MUR-A polls `get_scene` and receives `nightShow` — *refuses* (logged: `Scene pull skipped 'nightShow' (synchronized; awaiting trigger)`).
3. MUR-B and MUR-C poll later and receive the same — also refuse.
4. The operator fires the `SceneChange` trigger with `value=nightShow`.
5. Gateway fanout-delays it; all three MURs activate at the same TSF.

If the rule were "path 3 wins for everyone," any MUR that polled in window (2) would activate `nightShow` early and stay desynced for up to 5 seconds. The "refuse" rule prefers a small window of "wrong but consistent" over an unbounded window of "scattered." TCP's per-connection FIFO guarantee plus the gateway fanout-delay gives us that consistent window.

### Failure mode if path 1 is dropped

If the SceneChange trigger event itself fails to reach a MUR (gateway crash, TCP reset, etc.), and the scene is synchronized, that MUR stays on the old scene until the next `SceneChange` event. The 5-second poll *won't* recover it — by design. A future feature could add a "force resync" admin endpoint, but for now the operational answer is to fire the trigger again or reboot the affected MUR.

### Boot-time gotcha

If `default_scene` in `scenes.json` is itself synchronized, the boot path (path 5) activates it anyway and logs `ESP_LOGE("Boot scene 'X' is marked synchronized — fix scenes.json default_scene")`. This is intentionally noisy: a synchronized default is almost always a configuration mistake, because boot happens *outside* the synchronized timing path.

The right configuration is: `default_scene` should be a non-synchronized "neutral" scene (e.g., a quiet fallback) that the device can boot into harmlessly. The synchronized scene is then entered via a normal `SceneChange` trigger.

### Wire format

In `scenes.json` and on the `GET /api/scenes` response, scenes carry a `"synchronized": true|false` field. Setting via `POST /api/scenes` patch is supported. Default is `false`; the field can be omitted in saved JSON for non-synchronized scenes (it's elided on save when `false` to keep the file readable).

### Scene transition consistency — what protects against path-3 hazards

The synchronized-scene rule on the device prevents path-3 from activating a `synchronized` scene. But what about transitioning *away* from a synchronized scene to a non-synchronized one?

```
T+0    operator changes scene_service: show (sync) → day (not sync)
T+ε    scene_service auto-fires SceneChange("day")
T+2ε   mur_gateway receives the event; cached_scene becomes "day"
       (set synchronously, before any await), then trigger fans out
       to subscribers with target_tsf_us = T + 100 ms
T+10ms each MUR has the trigger
T+100ms all MURs synchronously activate "day" via path 1
```

Two invariants close this hazard without any extra gateway machinery:

1. **Cache update is synchronous on trigger receipt.** `_handle_trigger_event` writes `cached_scene = new_scene` *before* any `await`. There is no asyncio yield-point between the trigger arriving and the cache holding the new value. So once `mur_gateway` knows about the transition, it never hands out the old scene name to anyone.

2. **TCP per-connection FIFO.** Any `get_scene` reply that mur_gateway sent before the trigger arrived is on the wire ahead of the trigger on the same TCP connection. The MUR receives the old reply (correct at the time it was sent — MUR was on the old scene anyway, so handle_scene_response is a no-op), then receives the trigger and activates the new scene synchronously.

What remains is a small asyncio-interleaving window: between the cache update and the per-MUR `await device.send_line(line)` of the trigger, an interleaved `_answer_get_scene` for *that same MUR* can put the scene reply onto the TCP connection ahead of the trigger. That MUR processes the new scene via path 3 and activates it ~`fanout_delay_ms` early (default 100 ms). This is brief, stochastic (only fires if a poll happens to land in the gap), and the MUR converges to the correct scene either way. Treated as TCP-async noise rather than a structural hazard.

If you want to reduce that residual exposure further, the lever is on the gateway side, not the device:
- **Tighten the gateway's freshness with the scene service** by lowering `scene_cache_ttl` so lazy refresh is more aggressive (matters only if `SceneChange` events are ever lost — push-path is authoritative when working).
- **Don't tighten the device's `get_scene` polling interval** — that *increases* hazard exposure (more polls per unit time = more chances to land in the gap) without speeding recovery from a missed event meaningfully.

### Operational rule — never fire `SceneChange` from outside `scene_service`

`mur_gateway`'s only source of truth for the active scene is `scene_service`. It learns about scene changes via two channels, both originating there: the push-path `SceneChange` trigger that `scene_service.set_active_scene` auto-fires, and the pull-path HTTP fetch from `{scene_service}/api/scenes/active` (initial prime + lazy refresh on `scene_cache_ttl`). `mur_gateway` exposes no inbound HTTP endpoint that could inject a scene; nothing else writes `cached_scene`.

The hazard: `trigger_gateway` is a fan-in for trigger events from any registered source. If something other than `scene_service` POSTs a `SceneChange` event with an arbitrary scene name, `mur_gateway` will believe it — cache updates synchronously, the trigger fans out to subscribed MURs, and MURs activate that scene. Then on the next lazy refresh (within `scene_cache_ttl`, default 30 s), `mur_gateway` re-fetches from `scene_service` and the cache silently snaps back to whatever `scene_service` actually has. Any `get_scene` poll between those two events answers with the bogus value. Net effect: a transient flip-and-flap that can desync the fleet from the real show state, with no audit trail beyond the gateway log line `Scene cache updated from trigger: <bogus>`.

**Operational rule:** never fire `SceneChange` from anywhere except `scene_service`. The trigger name is reserved for `scene_service.set_active_scene`'s auto-emit. Any other source firing it is pilot error and the system has no way to defend against it.

If at some point the fleet needs to support a scene change initiated outside `scene_service`, do it by calling `scene_service.set_active_scene` (which then auto-fires `SceneChange`), not by firing the trigger directly.

### `SceneChange` always gets fanout, even with one subscriber

For arbitrary triggers, the gateway adds the fanout delay only when the subscriber count is greater than 1 (no point synchronizing to nothing). `SceneChange` is the exception: the gateway always adds `fanout_delay_ms` regardless of subscriber count.

Two reasons:
- **Test-vs-production parity.** A single-MUR test rig should exhibit the same scheduling path as a multi-MUR production install — both go through `mur_scheduler` with a future `target_tsf_us` deadline. Behavior doesn't snap from "instant fire" to "scheduled fire" the moment a second MUR connects.
- **Synchronized-scene activation always lands on the deferred path.** Even with one MUR, `scene_activate(SCENE_ACTIVATE_TRIGGER)` is invoked from the scheduler callback rather than from the listener's immediate dispatch. Cleaner separation of "this is a synchronized event" from "this is the listener's hot path."

Cost: `fanout_delay_ms` (default 2500 ms) of latency on single-MUR scene changes — sized for production WiFi. Acceptable for scene transitions (visitors don't notice 2 s on a wall display); if you're testing on a clean LAN and want faster scene-change feedback, lower the value in `config.json`.

This carve-out lives in `_resolve_target_tsf` and applies only when no explicit time field is provided (`target_tsf_us` / `iso_time` / `delta_ms` are still honored verbatim, as for any trigger).

**Critical design feature — do not "fix":** the 1-subscriber non-SceneChange passthrough (regular triggers fire immediately when only one MUR is subscribed) is intentional. A single MUR receiving a regular trigger has no one to sync with, so it fires on receipt for snappy local response. Removing this branch and applying `fanout_delay_ms` "everywhere" has been proposed and rejected — the asymmetry is the whole point.

## Event lifetime on the MUR

A scheduled trigger event passes through two distinct on-device queues. Both have specific properties that constrain what the device can do safely.

### Queue 1 — `mur_scheduler` heap (capacity 32)

A min-heap keyed on `target_tsf_us`, drained by a dedicated FreeRTOS task on core 0. When the listener parses an incoming event with a future `target_tsf_us`, it allocates a context (strdup'd name + value, plus an integer `volume`), submits to the heap, and returns to read the next line. The scheduler task wakes at the head's deadline, pops, and invokes the dispatch callback.

The dispatch callback may enqueue more than one `audio_control_msg_t`. A conducted downbeat carrying a `volume` enqueues `SET_VOLUME` and then `START_TRACK`; because `audio_control_task` drains a single FIFO queue, submission order is execution order, so the level is in place before the first sample of the new file. Both therefore take effect at the shared deadline. See MUR_PROTOCOL.md, "Per-event volume".

- **Strict deadline order, not arrival order.** Two events submitted A then B with `A.target_tsf_us > B.target_tsf_us` → B fires first. With the current symmetric-fanout-delay gateway logic this can't happen (target_tsf_us is monotonic with arrival, and TCP gives per-connection FIFO), but any future feature that uses `delta_ms` upstream or breaks the symmetric invariant could create it.
- **No merging, no cancellation, no event IDs.** A series of `START STOP START STOP STOP` runs every entry in order; the scheduler has no way to detect that some cancel each other. Implementing dedup/collapse would require either a numeric event id from the upstream (we don't track one today) or trigger-type-aware logic on the device. Both are tractable but unbuilt — see "On not building defensive complexity" below.
- **Capacity 32.** Submit beyond the cap returns `ESP_ERR_NO_MEM`; the listener falls back to immediate dispatch with a logged warning.

### Queue 2 — `audio_control_queue` (depth 10)

A FreeRTOS queue drained by `audio_control_task` on core 1. When a scheduled event's deadline arrives, `dispatch_event` enqueues one or more `audio_control_msg_t` entries here for the audio task to execute.

- **Per-action cost varies.** `STOP_TRACK` is cheap (idempotent `audio_pipeline_terminate`). `START_TRACK` on a playing track is expensive — terminate + reset ringbuffer + reset elements + run, full pipeline rebuild.
- **Drop on full.** If the queue fills before `audio_control_task` drains it, `xQueueSend` returns failure and the message is dropped with `ESP_LOGW`. Realistically only happens with 10+ rapid-fire events within a few milliseconds.

### Why arrival order matters

The scheduler fires strictly in deadline order, so the gateway's job is to ensure `target_tsf_us` is monotonic with the user's intended sequence. The current code achieves this two ways:
1. `now_to_tsf()` is sampled at the moment the gateway processes each event — strictly increasing.
2. TCP's per-connection FIFO guarantees the gateway processes events in the order the upstream sent them.

Breaking either invariant — for example, by skipping the fanout delay on `OFF` events while keeping it on `ON` — opens races where the device fires events in the wrong order. (Concrete failure: ON queues for T+100ms, OFF arrives without delay and fires immediately while pending; OFF dispatches as STOP on a not-yet-started track and is a no-op; ON later fires START with no matching STOP, sound plays forever.)

### On not building defensive complexity

Given the invariants above, defensive logic on the device — IDs, dedup, type-aware collapse — is not required for current behavior. It only earns its keep when we deliberately break an invariant for some other reason (snappier OFF, asymmetric delays, etc.). The simple, correct rule today is:

> **The scheduler executes every event it accepts, in deadline order, with no merging.** A series of `START STOP START STOP STOP` runs as exactly that. If that produces audible glitching, fix the upstream that's emitting redundant events; don't filter on the device.

The one defensive guard worth considering anyway is in the `START_TRACK` handler inside `audio_control_task`: "if already playing this exact file, no-op." Five lines, addresses the audibly-bad failure mode (rapid ON-spam causing pipeline rebuilds), and doesn't depend on event ordering. Not implemented today.

### Edge case — AP reboot

When the AP reboots, TSF on every associated STA resets to ~0. Pending events in each device's `mur_scheduler` heap have `target_tsf_us` values from the old epoch — typically much larger than the new TSF.

**Detection on the device.** `mur_scheduler` tracks the maximum `now_tsf` it has ever observed (`s_max_now_tsf`). On every wake, before scheduling work, it compares the fresh `now_tsf` reading against this max. If the fresh sample is more than `TSF_ROLLBACK_THRESHOLD_US` (1 second) below the previous max, the scheduler declares a rollback, **flushes every pending entry** (calling each entry's `free_ctx` so strdup'd payloads don't leak), logs a warning, and resets the max to the new `now`. The first event submitted after the rollback rebuilds the heap from scratch in the new epoch.

The threshold of 1 second is large enough that ordinary `esp_wifi_get_tsf_time()` measurement jitter doesn't trigger it, and small enough that any real AP-reboot signature is caught (the new TSF starts at ~0 microseconds, an enormous regression from any non-trivial uptime).

Look for this log line if you suspect an AP reboot: `TSF rollback detected (max=N now=M) — likely AP reboot; flushed K pending entries`.

**Gateway side.** The gateway's `TsfMap` canonical sample is also stranded after the reboot, but the periodic `tsf_query` pull replaces it from a re-associated MUR within one `tsf_query_interval_s` cycle, and translation resumes. Events forwarded *during* the gap have `target_tsf_us` computed from the stale map — those will be flushed by the device-side rollback detector on the next scheduler wake.

**What's still on the operator.** Devices in the middle of a deferred action (e.g., scheduled a `START_TRACK` for 50 ms in the future, AP rebooted 10 ms later) will lose that action — it's flushed alongside everything else. The audible result is "the cued sound didn't play." If the trigger source re-fires after the AP comes back, the system recovers cleanly because `dispatch_or_defer` operates on the new TSF epoch.

### Edge case — WiFi flap (device disassociates from same AP, re-associates)

`esp_wifi_get_tsf_time()` returns 0 while disassociated. The scheduler observes this and sleeps in 500 ms increments until TSF comes back. Pending events fire on schedule when WiFi recovers — often very late, in which case the per-MUR `late_policy` decides whether to fire-late-with-warning or drop. The AP's TSF kept ticking through the device's outage, so deadlines retain their original meaning. No special handling needed.

### Edge case — Device reboot

Scheduler heap is RAM-only. All pending events are cleared. Device re-announces with current TSF, gateway re-seeds map, normal operation resumes. No special handling needed and nothing to recover.

## Limits

- **Single-AP assumption.** All MURs must associate to the same AP. TSF is per-AP. Mesh, multi-AP roaming, or band steering will break this — the gateway will see jitter warnings but won't refuse to operate.
- **Sub-millisecond, not sample-accurate.** Audio frame at 44.1 kHz is 22 µs. We don't align I²S DMA buffers across devices; deadline precision is dominated by FreeRTOS task wake (~5 ms tick). For multi-room contexts the air-path differences between devices already swamp this; sample-accurate alignment would be wasted work.
- **TSF is undefined when not associated.** A MUR that loses WiFi can't read TSF. The scheduler holds entries until WiFi recovers (a late warning fires when it does, governed by `late_policy`). Triggers that arrive while disassociated are dispatched immediately when received post-reconnect (no `target_tsf_us` is set on them by the gateway in that path).
- **No formal Wi-Fi TimeSync.** We use TSF directly. Sufficient for our latency target.

## Validating on a new install

1. **One-MUR drift check** — flash a single MUR, leave it on the target AP, watch the `TSF_DRIFT` log for at least 10 minutes. `delta` should oscillate in a band, not trend. If it trends by ms-per-minute, something is wrong with the AP's beaconing — investigate before relying on sync.

2. **Two-MUR sanity check** — connect two MURs to the gateway. From the gateway status endpoint (`GET /status`), look at `sync.tsf_map.mur_sample_count` (should be 2 after the first pull cycle) and watch the gateway log for any `TSF jitter between A and B: X us` warnings.

3. **Fanout delay check** — send a "now" trigger to two subscribed MURs (no `delta_ms`/`iso_time`). Confirm the gateway log shows `target_tsf_us=N` on both forwards, and both MURs fire within the cross-device TSF jitter window. With a microphone, the two audio outputs should be indistinguishable in time.

   **Per-MUR offset tune-up** — once the fanout-delay check passes, set `playback_offset_us` on individual MURs to compensate for physical placement: each meter of extra air path is roughly +2900 µs. Recheck with the microphone after each change. Any leftover audible flam means the offsets are wrong, not the sync.

4. **Late event check** — set one MUR's `late_policy` to `drop`, the other to `play`. Send a trigger with `iso_time` set 1 s in the past. Confirm the "drop" MUR logs `late event ... — dropped` and silently does nothing; the "play" MUR logs `late event ... — playing anyway` and fires.

5. **Stale map check** — disconnect all MURs from the gateway, wait 2× `tsf_map_max_age_s`, send a timed trigger. Confirm the gateway logs `TSF map is stale` but still translates (best effort).

If any of these don't behave as described, fix that before relying on multi-device sync in production.
