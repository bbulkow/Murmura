# Murmura

Murmura is a low-cost, scalable platform for creating large installations of independent networked sound devices. Each unit plays audio loops from an SD card while being remotely controllable over WiFi, enabling sound artists to deploy dozens of autonomous speakers across an installation space and manage them from a single dashboard.

First deployed at **Burning Man 2025** as part of [Flaming Lotus Girls](https://flaminglotus.com/)' **Haven**.

## How It Works

Each Murmura unit is a self-contained audio player built on an ESP32 board with an SD card slot and audio output. On power-up it mounts the SD card, connects to WiFi, loads its saved configuration, and begins looping audio. A JSON HTTP API on each device allows remote control of playback, volume, and file management. A companion fleet management server running on a Raspberry Pi (or any machine on the network) provides a web dashboard to discover, monitor, and control all units simultaneously.

## Features

- **Multi-track looping** -- up to 3 simultaneous audio loops per device, mixed via hardware downmix
- **Per-track and global volume control** with real-time adjustment
- **WAV and MP3 playback** from SD card
- **WiFi with multi-network failover** -- stores up to 10 networks, auto-selects the strongest available signal
- **HTTP API** for full remote control (playback, volume, files, configuration, WiFi, device identity, reboot)
- **Configuration persistence** -- loop assignments, volumes, and play states saved to SD card and restored on boot
- **File upload/delete over HTTP** -- push audio files to devices without physically touching the SD card
- **Unique device identity** -- each unit has a configurable ID and reports its MAC address, IP, firmware version, and uptime
- **Fleet management server** (scape-server) -- web UI with network scanning, device dashboard, batch operations, and WebSocket live updates
- **CLI device tools** (device-manager) -- Python scripts for batch file upload, scanning, ID assignment, and device control

## Hardware

The current hardware platform is the **AI Thinker ESP32 Audio Kit (Rev B)**:

- ESP32 with PSRAM
- ES8388 audio codec
- SD card slot
- 3.5mm headphone/line output
- Onboard buttons and LEDs

**Note:** The AI Thinker board is no longer available through any channel. It appears to be discontinued -- possibly due to difficulty of use and its large form factor. This codebase is built exclusively for it, and the project currently relies on approximately 40 units in stock. A new hardware platform will be needed for future deployments.

### Potential Replacement Boards

- **Espressif LyraT Mini** -- in stock and produced by Espressif, ~$20 on Digi-Key
- **Waveshare ESP32-S3-Audio** -- ~$15, very similar form factor to the AI Thinker
- **Sonatino** -- currently out of stock (designer lost interest), but design files are published and could be manufactured with an updated audio chip

See [aithinker-adf/README.md](aithinker-adf/README.md) for board setup instructions including DIP switch configuration, efuse settings, and ESP-ADF overlay files.

## Repository Structure

```
main/                   ESP32 firmware source
  murmura.c/h             Audio pipeline and multi-track looper
  http_server.c/h         HTTP API server
  config_manager.c/h      Configuration persistence (JSON on SD card)
  wifi_manager_async.c    WiFi manager with multi-network support
  wifi_manager.h          WiFi manager API
  music_files.c/h         SD card file enumeration
  unit_status_manager.c/h Device identity and status
aithinker-adf/          Board support overlay files and build instructions
scape-server/           Flask web server for fleet management (Python)
device-manager/         CLI tools for batch device operations (Python)
```

## Building the Firmware

### Prerequisites

- ESP-ADF v2.7 (includes ESP-IDF v5.3.1)
- Python 3.x (for ESP-IDF tools)

### Setup

1. Clone ESP-ADF and apply the required patches. See [aithinker-adf/README.md](aithinker-adf/README.md) for step-by-step instructions.

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

## Running the Fleet Management Server

The scape-server provides a web dashboard for managing all Murmura devices on the network. It is designed to run on a Raspberry Pi deployed alongside the installation.

```bash
cd scape-server
pip install -r requirements.txt
python app.py
```

Access the dashboard at `http://localhost:8765`. See [scape-server/README.md](scape-server/README.md) for full documentation including systemd auto-start setup.

## Device Manager CLI Tools

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

See [device-manager/README_NETWORK_TOOLS.md](device-manager/README_NETWORK_TOOLS.md) for usage details.

## HTTP API

Each device exposes a JSON API on port 80. Key endpoints:

| Endpoint | Method | Description |
|---|---|---|
| `/api/files` | GET | List audio files on SD card |
| `/api/loops` | GET | Get status of all tracks |
| `/api/loop/file` | POST | Set file for a track (starts playing) |
| `/api/loop/start` | POST | Start/restart a track |
| `/api/loop/stop` | POST | Stop a track |
| `/api/loop/volume` | POST | Set per-track volume (0-100%) |
| `/api/global/volume` | POST | Set master volume (0-100%) |
| `/api/config/save` | POST | Save configuration to SD card |
| `/api/config/load` | POST | Load and apply saved configuration |
| `/api/upload` | POST | Upload audio file to SD card |
| `/api/status` | GET | Device status (MAC, IP, uptime, firmware) |
| `/api/id` | GET/POST | Get or set device ID |
| `/api/wifi/status` | GET | WiFi connection status |
| `/api/wifi/add` | POST | Add a WiFi network |
| `/api/system/reboot` | POST | Reboot the device |

See [HTTP_API.md](HTTP_API.md) for full API documentation with request/response examples.

## Documentation

- [HTTP_API.md](HTTP_API.md) -- complete HTTP API reference
- [WIFI_SETUP.md](WIFI_SETUP.md) -- WiFi configuration guide
- [aithinker-adf/README.md](aithinker-adf/README.md) -- hardware setup and ESP-ADF build instructions
- [scape-server/README.md](scape-server/README.md) -- fleet management server documentation
- [scape-server/SYSTEMD_INSTALL.md](scape-server/SYSTEMD_INSTALL.md) -- auto-start on Raspberry Pi
- [device-manager/README_NETWORK_TOOLS.md](device-manager/README_NETWORK_TOOLS.md) -- CLI tools reference

## License

This project is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

## Author

Brian Bulkowski (brian@bulkowski.org)
