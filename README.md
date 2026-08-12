# Murmura

Murmura is a low-cost, scalable platform for creating large installations of independent networked sound devices. Each unit plays audio loops from an SD card while being remotely controllable over WiFi, enabling sound artists to deploy dozens of autonomous speakers across an installation space and manage them from a single dashboard.

A key feature is the distributed nature and lack of reliance on anything besides power. After configuration, it will do its thing.
There is no required configuration server - the configuration server exists to find and configure Murs, but then configuration
is stored locally. The configuration is stored on the SD card so can be updated with a laptop.

It is designed to work with 20 to 40 to 100 individual units simultaenously. Systems that use wireless streaming speakers
simply don't work at that scale, as networks saturate. Murmura has all sound files local - commands pass over wifi (and files can be updated).

Low cost is also a goal. By using ESP32, a powerful but cheap DAC, and an SD card, per unit price is plausibly around $10 plus an SD card ($4) and a speaker ($5). Current hardware boards are closer to $17.

First deployed at **Burning Man 2025** as part of [Flaming Lotus Girls](https://flaminglotus.com/)' **Haven**. The initial configuration ended
up at about 18 units. The speaker chosen was a remaindered unit from Sony, installed in a custom 3d printed enclosure.

## Using Murmura

For instructions on operating deployed devices — WiFi setup, file management, playback and volume control — see [USE.md](USE.md).

## Known Issues

### Volume spike on scene change

When switching scenes (via trigger or API), volume briefly jumps to 100% before the new scene's volume takes effect. The scene activation pipeline sends track enable/start messages before the global volume message is applied, causing a momentary full-volume burst.

## Synchronized Multi-Device Playback

Murmura supports **sub-millisecond synchronized playback across multiple devices** using the WiFi MAC's TSF (Timing Synchronization Function) clock — a microsecond-precision timer that the WiFi stack already locks to the AP via beacon timestamps. Every STA on the same AP sees the same TSF, so it works as a free, zero-overhead shared clock with no central time server, no NTP, no PTP, and no extra packets on the wire.

This is the same architectural pattern used by **Sonos** for multi-room audio and standardized as **Wi-Fi TimeSync** (Wi-Fi Alliance, 2017) on top of **IEEE 802.11mc**. Murmura's implementation is a parallel one in the ESP-IDF / ESP-ADF stack.

Measured on production WiFi: cross-device alignment is bounded sub-millisecond, drift-free, and stable across hours of continuous operation. Each MUR also exposes a per-device `playback_offset_us` knob (settable via the fleet UI) so installers can compensate for the difference in air path between speakers — about 2.9 ms per metre — and tune the perceived simultaneity by ear at the listening position.

A `synchronized: true` flag on a scene enforces that the scene can only be entered through the cross-device-synchronized trigger path; the device refuses sideband activation paths (admin REST, periodic-poll backstop) for synchronized scenes, so a partial trigger drop leaves devices on the previous scene rather than half-converted to the new one.

See [SYNC_DESIGN.md](SYNC_DESIGN.md) for the full design — prior art, the validation procedure, the measurement results, the on-device scheduler, AP-reboot handling, and the end-to-end protocol.

## Ensembles — Conducted Playlists

Synchronization gets many devices to start one sound at the same instant. An **ensemble** is what happens when you want that to keep being true across a multi-hour playlist, unattended, for the length of an installation. A group of Murs advances through a shared playlist in lockstep, starting together from a cold boot, and a unit that reboots alone rejoins cleanly at the next boundary — without ever being heard out of phase.

The trick is an indirection. **mur-conductor** is a metronome and deliberately nothing more: it has no accurate clock and never computes a timestamp. At each playlist boundary — a *downbeat* — it emits a single trigger event. The **gateway** converts that into one absolute deadline on the shared WiFi-TSF clock and fans the *identical* value out to every subscribed device, each of which defers to it locally.

That buys a property worth stating plainly: **network jitter between the conductor and the gateway cannot spread the fleet apart.** A late event shifts the whole grid together. What determines whether two speakers agree is only whether they received the same number — and they always do, because the gateway computes it once. Three things follow for free:

- **Silent until the downbeat.** The ensemble track is in trigger mode, and `config_apply` never starts audio for trigger-mode tracks. A device boots *silent and armed*, so a solo reboot cannot be heard out of phase — it simply is not heard until it rejoins.
- **Drift correction is structural.** Every entry boundary re-aligns the whole fleet, so drift cannot accumulate beyond a single playlist entry however long the piece runs.
- **Fast cold start.** A readiness gate waits for the fleet and then fires immediately rather than making everyone wait out an entry, so a whole-installation power cycle converges within seconds of the Pi settling.

The playlist lives on the Pi, not the devices, and that is deliberate: device-local playlist advance is driven by end-of-file detection, which happens at a slightly different instant on every unit and cannot stay aligned. Keeping it central makes every boundary a synchronized event instead.

Configuration is entirely in the fleet dashboard — create a group, add members, and a readiness check tells you exactly what each device is still missing and configures it for you. See [ENSEMBLES.md](ENSEMBLES.md) for the full picture: setup walkthrough, playlist and duration handling, which edits restart a running group, and the known limitations.

## How It Works

Each Murmura unit (a Mur) is a self-contained audio player built on an ESP32 board with an SD card slot and audio output. On power-up it mounts the SD card, connects to WiFi, loads its saved configuration, and begins looping audio. A JSON HTTP API on each device allows remote control of playback, volume, and file management. A companion fleet management server running on a Raspberry Pi (or any machine on the network) provides a web dashboard to discover, monitor, and control all units simultaneously.

The ESP-ADF codebase was chosen. It supports multiple decoders, looping, mixing, and eq. However, its not simple, and it's
not clear Espressif's desire to continue with updates. THe last label was 2024.

## Features

- **Serverless distributed** -- acts alone after configuration.
- **Multi-track looping** -- up to 3 simultaneous audio loops per device, mixed via hardware downmix
- **Per-track and global volume control** with real-time adjustment
- **WAV and MP3 playback** from SD card
- **WiFi with multi-network failover** -- stores up to 10 networks, auto-selects the strongest available signal
- **HTTP API** for full remote control (playback, volume, files, configuration, WiFi, device identity, reboot)
- **Scenes** -- named playback configurations ("day", "night", "show") with instant switching, default boot scene, full config per scene
- **Trigger-based scene switching** -- discrete triggers (value = scene name) and per-scene button triggers for hands-free scene changes via the Haven trigger system
- **Sub-millisecond synchronized playback** across multiple devices, using the WiFi MAC's TSF clock — same architectural pattern as Sonos / Wi-Fi TimeSync (802.11mc), implemented natively on the ESP-ADF stack with no central time server. Includes per-device speaker-placement offset tuning and `synchronized` scene flag for atomic fleet-wide scene changes. See [SYNC_DESIGN.md](SYNC_DESIGN.md).
- **Ensembles** -- conducted playlists across a group of devices: shared playlist advanced in lockstep, drift re-corrected at every entry boundary, devices boot silent-and-armed so a solo reboot never plays out of phase, readiness-gated cold start. Configured entirely from the web dashboard, including a per-device readiness check that fixes what it finds. See [ENSEMBLES.md](ENSEMBLES.md).
- **Configuration persistence** -- scene configs saved to SD card and restored on boot
- **File upload/delete over HTTP** -- push audio files to devices without physically touching the SD card
- **Unique device identity** -- each unit has a configurable ID and reports its MAC address, IP, firmware version, and uptime
- **Fleet management server** (mur-config-server) -- web UI with network scanning, device dashboard, batch operations, and WebSocket live updates
- **CLI device tools** (device-manager) -- Python scripts for batch file upload, scanning, ID assignment, and device control

## Hardware

The current hardware platform is the **AI Thinker ESP32 Audio Kit (Rev B)**:

- ESP32 with PSRAM
- ES8388 audio codec
- SD card slot
- 3.5mm headphone/line output
- Onboard buttons and LEDs

The AI Thinker boards are still available on Amazon and are expected to settle back near $10 each as tariffs decrease.

### Alternative Boards

- **Espressif LyraT Mini** -- in stock and produced by Espressif, ~$20 on Digi-Key
- **Waveshare ESP32-S3-Audio** -- ~$15, very similar form factor to the AI Thinker
- **Sonatino** -- currently out of stock (designer lost interest), but design files are published and could be manufactured with an updated audio chip

See [aithinker-adf/README.md](aithinker-adf/README.md) for board setup instructions including DIP switch configuration, efuse settings, and ESP-ADF overlay files.

## Repository Structure

```
main/                   ESP32 firmware source
  murmura.c/h             Audio pipeline and multi-track looper
  http_server.c/h         HTTP API server
  scene_manager.c/h       Scene system (named playback configs, CRUD, activate, patch)
  config_manager.c/h      Configuration persistence and shared JSON helpers
  wifi_manager_async.c    WiFi manager with multi-network support
  wifi_manager.h          WiFi manager API
  music_files.c/h         SD card file enumeration
  unit_status_manager.c/h Device identity and status
  mur_listener.c/h        Mur Gateway TCP client, trigger event processing, scene trigger dispatch
aithinker-adf/          Board support overlay files and build instructions
mur-config-server/      Flask web server for fleet management (Python)
mur-scene-server/       Fleet-wide active scene + schedules, with web UI (Python)
device-manager/         CLI tools for batch device operations (Python)
mur-gateway/            Mur Gateway server (bridges trigger sources to devices)
mur-conductor/          Ensemble conductor -- playlist sequencer and downbeat source
mur-abs-gateway/        Abstract-trigger gateway variant
mock-mur-gateway/       Mock Mur Gateway for device testing (replaces real gateway)
mock-trigger-server/    Mock Haven Trigger Server for end-to-end testing
```

### Two servers, two layers of "scene"

These are easy to confuse, and mixing them up is the most common source of
scene-related bugs:

| | **mur-scene-server** (:5003) | **mur-config-server** (:8765) |
|---|---|---|
| Owns | *Which* scene is active, fleet-wide | *What* each scene plays, per device |
| Data | Scene names, active scene, schedules | Per-track file, volume, mode, triggers |
| Stored in | `mur-scene-server/mur_scene_server/scenes.json` | each MUR's `/sdcard/scenes.json` |

A scene is just a **name** shared between them. `mur-scene-server` says "the scene
is now `night`"; every MUR looks up `night` in its own config and plays whatever it
finds there. A MUR that has no `night` scene falls back to its default.

## Building and Running a Mur

### Prerequisites

- ESP-ADF v2.7 (includes ESP-IDF v5.3.1)
- Python 3.x (for ESP-IDF tools)

### Setup

1. Clone ESP-ADF and apply the required patches. See [aithinker-adf/README.md](aithinker-adf/README.md) for step-by-step instructions.

Note that you will be using the esp-idf that is within the esp-adf project.

2. Set environment variables:
   ```bash
   export ADF_PATH=/path/to/esp-adf
   export IDF_PATH=$ADF_PATH/esp-idf
   ```

3. Install ESP-IDF tools:
   ```bash
   cd $IDF_PATH
   ./install.sh    # or install.ps1 on Windows
   source export.sh # or export.ps1 on Windows
   ```

4. Copy the AI Thinker board overlay files into ESP-ADF (see [aithinker-adf/README.md](aithinker-adf/README.md)).

### Build and Flash

Note that there is a sdkconfig.defaults with quite a few key elements. These will get picked up
as part of the standard build below.

```bash
cd /path/to/Murmura
idf.py build
idf.py -p <PORT> flash monitor
```

The `sdkconfig.defaults` file provides the required configuration for the AI Thinker board. Run `idf.py menuconfig` if you need to adjust settings.

### VSCode ESP-IDF Extension

This project was developed using the **ESP-IDF v2.0 extension for VSCode**. To use it with a custom ESP-ADF/ESP-IDF installation (rather than one managed by the extension):

1. Set up the `ESP_TOOLS_PATH` directory to point to your toolchain installation.
2. Edit the `esp_idf.json` configuration file to include a pointer to the custom ESP-IDF installation you created above.
3. **Do not use the ESP-IDF version manager** built into the extension. It will download a fresh ESP-IDF version and attempt to update it, which will overwrite the overlay files and patches that this project requires.

## Fleet Management

Murmura provides robust fleet management, through two components.

There are a set of python commands which can be used without UI to identify boards and name them, and also to do
the commands in the fleet management web server. They were built before, and will be preferred in some cases.

### Running the fleet management server

The mur-config-server provides a web dashboard for managing all Murmura devices on the network. It is designed to run on a Raspberry Pi deployed alongside the installation.

```bash
cd mur-config-server
pip install -r requirements.txt
python app.py
```

Access the dashboard at `http://localhost:8765`. See [mur-config-server/README.md](mur-config-server/README.md) for full documentation including systemd auto-start setup.

### Device Manager CLI Tools

The device-manager directory contains Python scripts for command-line batch operations:

- **device_scanner.py** -- discover devices on the network
- **device_controller.py** -- control individual devices
- **batch_controller.py** -- batch operations across multiple devices
- **file_manager.py** -- upload, download, and manage audio files
- **id_manager.py** -- assign and manage device IDs

```bash
cd device-manager
pip install -r requirements.txt
python device_scanner.py
```

See [device-manager/README.md](device-manager/README.md) for usage details.

## HTTP API

Each device exposes a JSON API on port 80. Key endpoints:

| Endpoint | Method | Description |
|---|---|---|
| `/api/scenes` | GET | Get all scene configurations + metadata (active, default) |
| `/api/scenes` | POST | Patch-style update of scene configs (atomic, body keys = scene names) |
| `/api/scene` | POST | Scene management: create, delete, activate, set_default |
| `/api/device` | GET | Device config and status (identity, gateway, wifi) |
| `/api/device` | POST | Update device config (id, mur gateway, scene trigger) |
| `/api/files` | GET | List audio files on SD card |
| `/api/upload` | POST | Upload audio file to SD card |
| `/api/file/delete` | DELETE | Delete an audio file from SD card |
| `/api/wifi/add` | POST | Add a WiFi network |
| `/api/wifi/remove` | POST | Remove a WiFi network |
| `/api/config/save` | POST | Save scenes + gateway config to SD card |
| `/api/config/load` | POST | Load scenes from SD card, activate default |
| `/api/config/status` | GET | Config file status (scene count, active, default) |
| `/api/config/delete` | DELETE | Delete saved configuration |
| `/api/system/reboot` | POST | Reboot the device |

See [HTTP_API.md](HTTP_API.md) for full API documentation with request/response examples.

## Future

- **Audio ducking** — pull back the volume on other tracks while a triggered sample plays, so the cued sound cuts through cleanly. Closer to a near-term TODO than speculation; the per-track and global volume infrastructure is already in place.
- **Direct hardware interaction** — wired buttons or capacitive touch on the Mur itself for local triggers that don't depend on WiFi or the trigger-server pathway. Useful as both a primary interaction model for installations without networked control and a fallback when the network is degraded.
- **Human sensing (PIR)** — onboard passive-infrared motion detection so a Mur can react to viewer presence without an external sensor network. Closes the loop for installations that don't have a separate sensor fleet.
- **Alternative hardware platforms** — smaller boards (LyraT Mini, Sonatino) for tight enclosures and higher-amplifier-power boards for larger spaces. The current AI Thinker board is one form factor and one amplifier class; the deployment envelope wants more.

## Documentation

- [HTTP_API.md](HTTP_API.md) -- complete HTTP API reference
- [SYNC_DESIGN.md](SYNC_DESIGN.md) -- sub-millisecond multi-device synchronization (TSF, prior art, measurement, validation procedure)
- [ENSEMBLES.md](ENSEMBLES.md) -- conducted playlists across a group of devices (setup, playlist handling, operation, limitations)
- [mur-conductor/README.md](mur-conductor/README.md) -- the conductor service: endpoints, which edits restart a group, device provisioning
- [main/README.md](main/README.md) -- firmware notes, including the audio file format requirement and why mono misplays
- [WIFI_SETUP.md](WIFI_SETUP.md) -- WiFi configuration guide
- [aithinker-adf/README.md](aithinker-adf/README.md) -- hardware setup and ESP-ADF build instructions
- [mur-config-server/README.md](mur-config-server/README.md) -- fleet management server documentation
- [mur-config-server/SYSTEMD_INSTALL.md](mur-config-server/SYSTEMD_INSTALL.md) -- auto-start on Raspberry Pi
- [device-manager/README.md](device-manager/README.md) -- CLI tools reference
- [device-manager/CHEATSHEET.md](device-manager/CHEATSHEET.md) -- CLI quick reference
- [MUR_PROTOCOL.md](MUR_PROTOCOL.md) -- device ↔ Mur Gateway protocol spec

## License

This project is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

## Author

Brian Bulkowski (brian@bulkowski.org)
