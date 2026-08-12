# Ensembles — conducted multi-device playlists

An **ensemble** makes several Murs play the same material *together*: starting
together from a cold boot, staying together for hours, advancing through a
playlist in lockstep, and letting a unit that reboots alone rejoin cleanly
without ever being heard out of phase.

This is the layer above [SYNC_DESIGN.md](SYNC_DESIGN.md). Synchronization gets
many devices to start one sound at the same instant. An ensemble is what happens
when you want that to keep being true across a 17-track, hour-long playlist,
unattended, for the length of an installation.

Requires no firmware changes to the audio path.

## Why this is not just "loop the same file everywhere"

Three independent things break the naive approach, and all three have the same
fix — something with a fleet-wide view has to emit the beat.

**Loops drift.** Looping in the firmware is not a seamless pipeline loop: each
iteration is a full teardown and rebuild with a variable software gap. Four
devices started in perfect sync random-walk apart within minutes.

**Boot is unsynchronized.** Devices come up whenever they come up. On a cold
start the Murs typically reach the gateway before the Pi has finished booting its
services.

**A solo reboot cannot be fixed by the device.** A rebooted unit can learn *what*
to play, but it has no way to know *when* the rest of the group is in its
material. Guessing means playing out of phase, which is worse than silence.

## How it works

```
[mur-conductor]   :5002 registration + ingest, :4002 status/admin
      |  emits ONE trigger event per playlist boundary
[mur-gateway]     :4000 devices, :4001 status, :5100 upstream
      |  stamps that event with an absolute WiFi-TSF deadline
      |  and fans the SAME deadline to every subscribed device
[Murs]            each defers execution to that deadline locally
```

The conductor is a **metronome, and deliberately nothing more**. It has no
accurate clock and never computes a timestamp. At each playlist boundary — a
*downbeat* — it emits a single trigger event. The **gateway** converts that into
one absolute deadline (`target_tsf_us`) on the shared WiFi-TSF clock and fans the
*identical* value out to every subscribed device. Each device defers to that
deadline with its own on-device scheduler.

That indirection is the whole trick, and it buys a property worth stating
plainly: **network jitter between the conductor and the gateway cannot affect
device-to-device alignment.** A slow or late event shifts the whole grid
together; it never spreads the fleet apart. The only thing that determines
whether two speakers agree is whether they got the same number, and they always
do — the gateway computes it once.

Three further properties fall out for free:

- **Silent until the downbeat.** The ensemble track is in `trigger` mode, and
  `config_apply` never starts audio for trigger-mode tracks — it only enables
  them. A device boots *silent and armed*. A unit that reboots alone makes no
  sound at all until it rejoins at the next boundary. It cannot be heard out of
  phase, because it is not heard.
- **Drift correction is structural.** Every entry boundary re-aligns the entire
  fleet, so drift cannot accumulate beyond a single playlist entry no matter how
  long the installation runs.
- **Fast cold start.** The conductor waits for the fleet (the *readiness gate*)
  and then fires immediately, rather than making everyone wait out an entry. When
  everything including the Pi power-cycles together, the first unified downbeat
  lands within seconds of the Pi settling.

For alignment figures, the measurement procedure and the prior art (this is the
same architectural pattern as Sonos multi-room and Wi-Fi TimeSync / 802.11mc),
see [SYNC_DESIGN.md](SYNC_DESIGN.md). The residual floor for a conducted downbeat
is the on-device scheduler plus I2S buffer alignment, documented in
[mur-conductor/README.md](mur-conductor/README.md); per-speaker air-path
differences are trimmed with `playback_offset_us` (about 2.9 ms per metre).

### Why the playlist lives on the Pi

An earlier design put playlists on the device, advancing on end-of-file
detection. That cannot work for an ensemble: EOF happens at a slightly different
instant on every device, cursors drift apart, and there is no mechanism to
re-align them. Keeping the playlist on the Pi makes **every entry boundary a
synchronized event** instead.

Device-local playlists remain useful for a standalone Mur. They must not be
combined with an ensemble on the same track.

## Setting one up

Everything below is in the fleet dashboard — **Ensembles** panel → *Open
Ensembles*. `mur-conductor/config.json` is written for you; you should not need
to open it.

1. **Create the group.** Name, trigger name, scene name, and which track (0–2)
   carries the material. Leave it **disabled** — the default — so it does not
   start firing downbeats at devices that are not set up yet.
2. **Add members.** *Members* lists every device the fleet server knows plus
   anything already in the group, and warns if a device is claimed by another
   group.
3. **Check setup.** Reads each member's scenes directly and lists exactly what is
   missing, in *dependency* order — a wrong active scene is the cause of "not
   subscribed", not a separate problem.
4. **Configure.** *Configure this device* (or *Configure all*) applies only the
   missing steps: create the scene, point the track at the group's trigger in
   OneShot mode, make the scene active and the boot default, save to SD.
   Idempotent. Doing it by hand on the device page also works and the checklist
   links there.
5. **Set the playlist.** Pick files; durations are read from the WAV headers
   automatically.
6. **Enable.** Last, once the checklist is green.

What each member ends up with is one track in `trigger` / `OneShot` mode listening
for the group's trigger name, in a scene that is both active and the boot
default, saved to SD. That is the entire device-side state — there is no
"ensemble" object on a Mur.

### The playlist

Each entry is a file, a duration, an optional gap, and a volume. The next downbeat lands at
`duration + gap` regardless of what the audio is doing, so the duration should
match the real file length: too long leaves silence, too short cuts the file off.
The editor reads the true length from the WAV header when you pick a file, and
*Fit to file length* re-reads them all. Each row flags a mismatch.

**Volume** is a per-entry level trim, 0-100, default 100. Entries are
loudness-normalized in principle; this is the trim for what the room actually
needs. It rides the downbeat and lands on every member at the same instant, so it
changes exactly at the boundary rather than sliding during an entry. If one unit
needs to be quieter than the rest, that is `device_volume` on that device, not
this. 0 mutes the entry, and the row says so.

There is deliberately **no "play to the end"**. The conductor schedules the next
downbeat on an absolute deadline computed in advance, and no device reports
playback completion back to it. That fixed grid is precisely what keeps the fleet
aligned.

> **Audio files must be 44100 Hz, 16-bit, stereo PCM WAV.** Nothing in the system
> transcodes. A mono file plays at exactly double speed with no error on any
> surface. See the format section in [AGENTS.md](AGENTS.md) and the analysis in
> [main/README.md](main/README.md).

## Operating it

The group card shows what is playing, which entry of how many, when the next
downbeat lands, and how many have gone. Members show present / subscribed / last
file push, and *Check files* compares SD contents across the group.

**Getting files onto members** is done with `device-manager/file_manager.py` from
a machine that holds the WAVs, not from this page. There was a *Copy missing
files* button; it sent the upload with chunked framing, which `esp_http_server`
cannot read, so the firmware saw a zero content length, wrote an empty file and
answered 200 — every copy silently produced a 0-byte WAV and reported success.
The framing is fixed and `POST /api/ensemble/<group>/sync` works, but the button
is gone: a transfer that holds a device for minutes has no place behind one click
on a page that polls every 2 s, with no progress, no cancel, and a lock hold long
enough to make the dashboard report the fleet offline. It needs a page of its
own. Pushing from a laptop is also faster — no device-to-device double hop, no
throttle, and it prints progress.

**Device setup** reports two independent facts per member and never mixes them.
*at gateway* is whether the gateway holds that device's outbound connection —
without it no downbeat can reach it, whatever else is correct. *responding* is
whether this server just reached its HTTP API. A device can be responding but not
at the gateway; that is a normal state, usually a stale `mur_gateway_ip`, and the
check says so by name rather than calling the device offline. Such a device is
still fully configurable from here: the conductor only knows a member's address
as the peer address of its gateway connection, so this server falls back to the
address from its own network scan. The one state that blocks the check is *no
address*, where neither source has one and a network scan from the dashboard is
the fix.

**Editing a live group.** Most fields apply at the next downbeat with nothing
interrupted: membership, `prep_lead_ms`, `loop_playlist`, `readiness_timeout_s`,
and renaming. Four fields restart the group runner — `enabled`, `trigger_name`,
`scene_name`, `track` — which cuts the current entry and restarts the playlist at
entry 1. The UI marks those and says so before saving. Playlist edits always take
effect at the next boundary, so an entry is never cut off mid-file.

**Two lags worth knowing.** The conductor's view of the fleet is a 10-second
poll, and subscriptions are published by the *device* — so after a fix the
checklist (a direct device read) goes green before the Status column catches up.
The UI says so where it matters.

**Stopping.** Disable the group, or stop the conductor: devices finish the
current entry and fall silent together. There is no hard stop mid-file — the
gateway drops falsy trigger events by design.

## Limitations

- **No mid-entry join.** A device that reboots into a 15-minute entry waits it
  out in silence; there is no seek or playback-position API to splice it in.
- **Not sample-accurate.** See the floor discussion above.
- **Timeline is not persisted.** A conductor restart re-runs the readiness gate
  and starts the playlist from entry 0.
- **Gateway recovery takes up to 30 s** after a conductor restart, on the
  gateway's re-registration loop.
- **One group per device track.** A track carries exactly one trigger name, so
  two groups using the same device and track will have one group's downbeats
  silently ignored. The membership editor warns about this.
- **Entry volume is fleet-wide.** The gateway serializes one event and sends
  identical bytes to every subscriber, so an entry's volume is the same on every
  member by construction. Per-device trim is `device_volume`.

## Where the code is

| | |
|---|---|
| `mur-conductor/` | the service: playlist sequencer, readiness gate, admin API, systemd unit |
| `mur-conductor/README.md` | operational detail, endpoint reference, which edits restart a group |
| `mur-conductor/setup_ensemble.py` | CLI equivalent of *Configure device*, plus `--verify` |
| `mur-conductor/fake_device.py` | simulate members without hardware |
| `mur-config-server/` | the `/ensembles` UI, file comparison, readiness check |
| `device-manager/file_manager.py` | push audio files to devices |
| `mur-gateway/` | stamps the shared TSF deadline and fans it out |
| `SYNC_DESIGN.md` | the synchronization layer this is built on |
