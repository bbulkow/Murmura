# PLAN: Ensemble playback (mur-conductor)

Status: **software complete and verified; hardware verification pending.**
Per the AGENTS.md rule, this plan lives in-repo; delete this file once the
hardware checklist below has passed.

## Goal

Multiple MURs play a playlist together: starting together from a cold boot
(including the Pi), staying aligned for hours, a solo-rebooted device staying
SILENT until it rejoins at the next downbeat. Murmura-only deployment — no
Haven; the conductor is the top of the trigger chain.

## What was built (done, software-verified)

- **mur-conductor/** — Pi service: embeds the trigger-server role (gateway
  registers with it on :5002, conductor pushes events to gateway :5100),
  playlist sequencer on absolute monotonic deadlines, readiness-gated first
  downbeat, health monitoring via gateway `:4001/status` only, status/admin API
  on :4002, systemd unit, `fake_device.py`, `setup_ensemble.py`. Design and
  operational details: `mur-conductor/README.md`.
- **Firmware**: `GET /api/file/download` in `main/http_server.c` (chunked
  stream; enables mur→mur copy + WAV duration probing). Documented in
  HTTP_API.md; builds clean; covered by device_test.py Group 2b.
- **mur-config-server**: `/ensembles` page — live group status, playlist editor
  offering only the filename-intersection across members, durations auto-read
  from WAV headers, throttled one-at-a-time file-sync engine. `peer_ip` added
  to both gateways' `/status` (additive).
- **Verified without hardware**: 16/16 conductor checks (identical
  `target_tsf_us` on every beat across devices — the property everything rests
  on), 22/22 UI checks (intersection, probing, playlist persist, byte-identical
  copy, "differs" files untouched).

## Remaining: hardware verification (2+ real MURs + the Pi)

1. Per device, one-time: `python mur-conductor/setup_ensemble.py --group <g>`,
   then `--verify`. Confirm a rebooted device boots silent-armed.
2. `device_test.py` against one device — Group 2b (download round trip) passes;
   watch a long download alongside the device's gateway TCP connection.
3. Cold start: power-cycle everything incl. the Pi → devices silent, then one
   unified first downbeat within seconds of the Pi settling (readiness gate).
4. Solo reboot mid-entry → silent, joins at the next entry boundary.
5. Entry transitions: single attack across speakers (ear + phone recording
   between two speakers); device serial shows `Trigger '<name>' matched track`
   with no steady-state `late event` warnings; gateway journal shows identical
   `target_tsf_us` per beat, no TSF-jitter/stale-map warnings.
6. `systemctl stop mur-conductor` → all finish the current entry, silence
   together.
7. Soak ≥4 h including the longest entry (15 min file): listen at boundaries.
8. File sync against real SD cards (copy a file to a device missing it,
   confirm playable; confirm a "differs" file untouched).

## Known limitations (accepted, documented in mur-conductor/README.md)

No mid-entry join (no seek API) · not sample-accurate (~5-10 ms floor +
`playback_offset_us` per speaker) · no hard stop (gateway drops falsy events) ·
conductor restart re-runs readiness and restarts the playlist at entry 0 ·
gateway re-registration after a conductor restart can take up to 30 s.

## Invariants this work must not break

- Never fire `SceneChange` from the conductor (scene-service-only; gateway
  special-cases it).
- No inbound injection endpoint on the gateway; the conductor injects by BEING
  the upstream.
- The 1-subscriber unscheduled passthrough in `_resolve_target_tsf` stays.
- The prep patch keeps `"active": true` — see the FOOTGUN bullet in AGENTS.md.
