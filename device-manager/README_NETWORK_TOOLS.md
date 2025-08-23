# ESP32 Network Device Management Tools

This repository contains Python tools for discovering and managing multiple ESP32 audio loop controller devices on a network.

## Features

- **Network Scanner** (`device_scanner.py`): Discovers all ESP32 devices on your network
- **Batch Controller** (`batch_controller.py`): Performs batch operations on multiple devices
- **Device Controller** (`device_controller.py`): Controls a single device by ID
- **File Manager** (`file_manager.py`): Upload, sync, and manage audio files on devices
- **ID Manager** (`id_manager.py`): Manage device IDs, find duplicates, and identify devices
- **Asynchronous Operations**: Fast parallel scanning and control of multiple devices
- **Device Tracking**: Maintains a JSON map of all devices with MAC addresses as unique identifiers
- **Shortcut Options**: All commands now support single-letter shortcuts for faster command entry

## Installation

1. Install Python 3.7 or higher
2. Install required dependencies:
```bash
pip install -r requirements.txt
```

## Usage

### Device Scanner

The scanner discovers ESP32 devices on your network and creates/maintains a device map JSON file.

#### Basic Usage

```bash
# Create a new device map (overwrites existing)
python device_scanner.py --net 192.168.1.0/24 --action create

# Add new devices to existing map (preserves offline devices)
python device_scanner.py --net 192.168.1.0/24 --action add

# Update existing devices only (ignores new devices)
python device_scanner.py --net 192.168.1.0/24 --action update
```

#### Command Line Options

```bash
python device_scanner.py --help

Required arguments:
  --net CIDR, --network CIDR, -n CIDR
                        Network range in CIDR format (e.g., 192.168.1.0/24)
  --action {create,add,update}, --mode {create,add,update}, -a {create,add,update}
                        Scan action: create (new map), add (merge), or update (existing only)

Optional arguments:
  --timeout SEC, -t SEC         Connection timeout in seconds (default: 2)
  --concurrent NUM, -c NUM      Maximum concurrent connections (default: 50)
  --map-file PATH, -m PATH      Path to device map JSON file (default: device_map.json)
  --verbose, -v                 Enable verbose debug logging
```

#### Shortcut Examples

```bash
# Using shortcuts for faster command entry
python device_scanner.py -n 192.168.1.0/24 -a create
python device_scanner.py -n 192.168.1.0/24 -a add -t 5 -c 100
python device_scanner.py -n 192.168.1.0/24 -a update -m production.json -v
```

#### Scanning Modes Explained

- **`create`**: Completely replaces the device map with newly discovered devices
- **`add`**: Adds new devices and updates existing ones (by MAC address), marks offline devices
- **`update`**: Only updates devices already in the map, ignores new devices

### Batch Controller

The batch controller performs operations on all devices in the device map simultaneously.

#### Basic Usage

```bash
# Show status of all devices
python batch_controller.py --command status

# Stop all loops on all devices
python batch_controller.py --command stop-all

# Start all configured loops
python batch_controller.py --command start-all

# Set volume on all devices
python batch_controller.py --command set-volume --track 0 --volume 50
python batch_controller.py --command set-volume --global --volume 75

# Save current configuration on all devices
python batch_controller.py --command save-config

# Load saved configuration on all devices
python batch_controller.py --command load-config
```

#### Command Line Options

```bash
python batch_controller.py --help

Required arguments:
  --command {status,stop-all,start-all,set-volume,save-config,load-config}, -c
                        Command to execute on all devices

Optional arguments:
  --map-file PATH, -m PATH      Path to device map JSON file (default: device_map.json)
  --timeout SEC, -t SEC         Request timeout in seconds (default: 5)
  --concurrent NUM, -n NUM      Maximum concurrent connections (default: 10)

Volume control:
  --track {0,1,2}, -k {0,1,2}   Track number for track-specific commands
  --volume LEVEL, -v LEVEL       Volume level (0-100)
  --global, -g                   Set global volume instead of track volume

Device filters:
  --all-devices, -a              Include offline devices (default: online only)
  --filter-id REGEX, -f REGEX    Filter devices by ID pattern (regex)
```

#### Shortcut Examples

```bash
# Using shortcuts
python batch_controller.py -c status
python batch_controller.py -c stop-all -a
python batch_controller.py -c set-volume -k 0 -v 50
python batch_controller.py -c set-volume -g -v 75
python batch_controller.py -c status -f "^STAGE"
```

### Device Controller

The device controller performs operations on a single device by its ID.

#### Basic Usage

```bash
# Show device status
python device_controller.py --id LOUDFRAME-001 --command status

# Stop all loops on a device
python device_controller.py --id LOUDFRAME-001 --command stop

# Start configured loops
python device_controller.py --id LOUDFRAME-001 --command start

# Set volume on a specific device
python device_controller.py --id LOUDFRAME-001 --command set-volume --track 0 --volume 50
python device_controller.py --id LOUDFRAME-001 --command set-volume --global --volume 75

# Change device ID (unique operation)
python device_controller.py --id LOUDFRAME-001 --command set-id --new-id STAGE-01

# Get loop status
python device_controller.py --id LOUDFRAME-001 --command get-loops

# Set file for a track
python device_controller.py --id LOUDFRAME-001 --command set-file --track 0 --file-index 2
```

#### Command Line Options

```bash
python device_controller.py --help

Required arguments:
  --id ID, -i ID        Device ID to control
  --command {status,stop,start,set-volume,set-id,save-config,load-config,get-loops,set-file}, -c
                        Command to execute on the device

Optional arguments:
  --map-file PATH, -m PATH      Path to device map JSON file (default: device_map.json)
  --timeout SEC, -t SEC         Request timeout in seconds (default: 5)
  --force, -f                   Skip device ID verification (not recommended)

Volume control:
  --track {0,1,2}, -k {0,1,2}   Track number for track-specific commands
  --volume LEVEL, -v LEVEL       Volume level (0-100)
  --global, -g                   Set global volume instead of track volume

Device ID control:
  --new-id ID, -n ID             New device ID for set-id command

File control:
  --file-index INDEX, -x INDEX   File index from /api/files for set-file command
  --file-path PATH, -p PATH      Direct file path for set-file command
```

#### Shortcut Examples

```bash
# Using shortcuts
python device_controller.py -i LOUDFRAME-001 -c status
python device_controller.py -i LOUDFRAME-001 -c stop
python device_controller.py -i LOUDFRAME-001 -c set-volume -k 0 -v 50
python device_controller.py -i LOUDFRAME-001 -c set-id -n STAGE-01
python device_controller.py -i LOUDFRAME-001 -c set-file -k 0 -x 2
```

### File Manager

The file manager handles uploading, syncing, and managing audio files on devices.

#### Basic Usage

```bash
# List files on all devices
python file_manager.py --command list

# List files on a specific device
python file_manager.py --command list --id LOUDFRAME-001

# Upload a file to all devices (skips if already exists)
python file_manager.py --command upload --file music.wav

# Upload a file to a specific device
python file_manager.py --command upload --file music.wav --id LOUDFRAME-001

# Force upload (overwrite even if exists)
python file_manager.py --command upload --file music.wav --force

# Sync all audio files from a directory
python file_manager.py --command sync --directory ./loops

# Delete a file from all devices
python file_manager.py --command delete --file old_music.wav
```

#### Command Line Options

```bash
python file_manager.py --help

Required arguments:
  --command {list,upload,sync,delete}, -c
                        Command to execute

Optional arguments:
  --map-file PATH, -m PATH      Path to device map JSON file (default: device_map.json)
  --timeout SEC, -t SEC         Request timeout in seconds (default: 30)
  --concurrent NUM, -n NUM      Maximum concurrent uploads (default: 5)

Target selection:
  --id ID, -i ID                Specific device ID (default: all devices)

File operations:
  --file PATH, -f PATH          File to upload or delete
  --directory PATH, -d PATH     Directory for sync operation
  --force, -F                   Force upload even if file exists
  --target-name NAME, -r NAME   Custom filename for uploaded file
```

#### Shortcut Examples

```bash
# Using shortcuts
python file_manager.py -c list
python file_manager.py -c upload -f music.wav
python file_manager.py -c upload -f music.wav -i LOUDFRAME-001
python file_manager.py -c upload -f music.wav -F
python file_manager.py -c sync -d ./loops
python file_manager.py -c delete -f old_music.wav
```

#### Features

- **Smart Upload**: Checks if files already exist (by name and size) before uploading
- **Batch Upload**: Upload to multiple devices concurrently (limited to 5 by default for stability)
- **Directory Sync**: Sync all audio files (WAV, MP3, M4A, AAC, FLAC) from a directory
- **Progress Tracking**: Shows upload progress and summary statistics

## Device Map Format

The device map is stored as a JSON file with the following structure:

```json
{
  "scan_time": "2025-01-22T22:00:00",
  "scan_mode": "create",
  "network_range": "192.168.1.0/24",
  "device_count": 3,
  "devices": [
    {
      "ip_address": "192.168.1.100",
      "mac_address": "AA:BB:CC:DD:EE:FF",
      "id": "LOUDFRAME-001",
      "wifi_connected": true,
      "firmware_version": "1.0.0",
      "uptime_seconds": 3600,
      "last_seen": "2025-01-22T22:00:00",
      "online": true
    }
  ]
}
```

## Typical Workflow

### Initial Setup

1. **Scan the network** to discover all devices:
```bash
python device_scanner.py --net 192.168.1.0/24 --action create
```

2. **Check device status**:
```bash
python batch_controller.py --command status
```

3. **Assign unique IDs** to each device individually:
```bash
python device_controller.py --id LOUDFRAME-001 --command set-id --new-id "STAGE-01"
python device_controller.py --id LOUDFRAME-002 --command set-id --new-id "STAGE-02"
# etc...
```

### Daily Operations

1. **Update device map** with current IPs:
```bash
python device_scanner.py --net 192.168.1.0/24 --action update
```

2. **Start all devices**:
```bash
python batch_controller.py --command start-all
```

3. **Adjust volume** on all devices:
```bash
python batch_controller.py --command set-volume --global --volume 80
```

4. **Stop all devices**:
```bash
python batch_controller.py --command stop-all
```

### Adding New Devices

1. **Scan with add mode** to discover new devices while preserving existing ones:
```bash
python device_scanner.py --net 192.168.1.0/24 --action add
```

2. **Set IDs** for new devices individually:
```bash
# Check which devices are new
python batch_controller.py --command status

# Set ID for each new device
python device_controller.py --id UNKNOWN --command set-id --new-id "LOUDFRAME-003"
```

3. **Upload audio files** to the devices:
```bash
# Upload a single file to all devices
python file_manager.py --command upload --file loop1.wav

# Sync entire directory of loops
python file_manager.py --command sync --directory ./audio_loops
```

## Advanced Usage

### File Management Workflow

1. **Check what files are on devices**:
```bash
python file_manager.py --command list
```

2. **Upload new loops to all devices**:
```bash
# Upload single file
python file_manager.py --command upload --file new_loop.wav

# Sync entire folder (only uploads new/changed files)
python file_manager.py --command sync --directory ./loops
```

3. **Clean up old files**:
```bash
python file_manager.py --command delete --file old_loop.wav
```

4. **Upload to specific device only**:
```bash
python file_manager.py --command upload --file special_loop.wav --id STAGE-CENTER
```

### Filtering Devices

Control specific devices by filtering:

```bash
# Only control devices with IDs starting with "STAGE"
python batch_controller.py --command stop-all --filter-id "^STAGE"

# Include offline devices in operations
python batch_controller.py --command status --all-devices

# Control a specific device
python device_controller.py --id "STAGE-01" --command stop
```

### Performance Tuning

Adjust concurrency for your network:

```bash
# Slower network or many devices - reduce concurrent connections
python device_scanner.py --net 192.168.1.0/24 --action create --concurrent 20

# Fast network - increase concurrent connections
python device_scanner.py --net 192.168.1.0/24 --action create --concurrent 100
```

### Custom Network Ranges

Scan specific subnets:

```bash
# Scan a /24 subnet (256 addresses)
python device_scanner.py --net 192.168.1.0/24 --action create

# Scan a smaller range
python device_scanner.py --net 192.168.1.100/28 --action create  # 16 addresses

# Scan a larger range
python device_scanner.py --net 10.0.0.0/16 --action create  # 65536 addresses (will be slow)
```

### Custom Map File Location

Use different device map files for different environments:

```bash
# Production environment
python device_scanner.py --net 192.168.1.0/24 --action create --map-file production.json
python batch_controller.py --command status --map-file production.json
python device_controller.py --id PROD-001 --command status --map-file production.json

# Testing environment
python device_scanner.py --net 10.0.0.0/24 --action create --map-file testing.json
python batch_controller.py --command status --map-file testing.json
python device_controller.py --id TEST-001 --command status --map-file testing.json
```

## Device ID Management

Device IDs must be set individually using the device controller to ensure uniqueness:

```bash
# Change a device ID
python device_controller.py --id OLD-ID --command set-id --new-id NEW-ID

# Examples
python device_controller.py --id LOUDFRAME-001 --command set-id --new-id "STAGE-CENTER"
python device_controller.py --id LOUDFRAME-002 --command set-id --new-id "STAGE-LEFT"
python device_controller.py --id LOUDFRAME-003 --command set-id --new-id "STAGE-RIGHT"
```

After changing device IDs, rescan the network to update the device map:
```bash
python device_scanner.py --net 192.168.1.0/24 --action update
```

## Troubleshooting

### Scanner finds no devices

1. Verify devices are powered on and connected to WiFi
2. Check network range is correct
3. Increase timeout: `--timeout 5`
4. Check firewall settings
5. Use verbose mode for debugging: `--verbose`

### Controller operations fail

1. Run scanner first to update device map
2. Check devices are online: `python batch_controller.py --command status`
3. For single device issues: `python device_controller.py --id DEVICE-ID --command status`
4. Verify network connectivity to devices
5. Increase timeout: `--timeout 10`

### Slow scanning

1. Reduce concurrent connections if network is overloaded
2. Decrease timeout for faster failure detection
3. Scan smaller network ranges

## Security Considerations

- The tools communicate over HTTP (not HTTPS)
- No authentication is required to access device APIs
- Device maps may contain sensitive network information
- Use only on trusted, isolated networks

## Requirements

- Python 3.7+
- aiohttp library
- Network access to ESP32 devices on port 80
- Devices must be running the ESP32 Audio Loop Controller firmware with file upload API support

## Tool Summary

| Tool | Purpose | Key Commands |
|------|---------|--------------|
| `device_scanner.py` | Discover devices on network | `--net 192.168.1.0/24 --action create` (or `-n 192.168.1.0/24 -a create`) |
| `batch_controller.py` | Control all devices at once | `--command status/stop-all/start-all` (or `-c status/stop-all/start-all`) |
| `device_controller.py` | Control single device by ID | `--id DEVICE --command status/stop/start` (or `-i DEVICE -c status/stop/start`) |
| `file_manager.py` | Manage audio files | `--command list/upload/sync/delete` (or `-c list/upload/sync/delete`) |
| `id_manager.py` | Manage device IDs and identify | `--command find-duplicates/set-id/identify` (or `-c find-duplicates/set-id/identify`) |

### ID Manager

The ID manager handles device ID operations and can help identify specific devices.

#### Basic Usage

```bash
# Find all devices with duplicate IDs
python id_manager.py --command find-duplicates

# List all devices with their IDs and MACs
python id_manager.py --command list-all

# Set device ID based on MAC address
python id_manager.py --command set-id --mac 34:5F:45:26:76:2C --new-id STAGE-01

# Identify a device by playing a sound
python id_manager.py --command identify --id LOUDFRAME-001
python id_manager.py --command identify --mac 34:5F:45:26:76:2C --duration 60

# Auto-assign unique IDs to all devices
python id_manager.py --command auto-assign --prefix LOUD
python id_manager.py --command auto-assign --prefix STAGE --start-num 100
```

#### Command Line Options

```bash
python id_manager.py --help

Required arguments:
  --command {find-duplicates,set-id,identify,list-all,auto-assign}, -c
                        Command to execute

Optional arguments:
  --map-file PATH, -f PATH      Path to device map JSON file (default: device_map.json)
  --timeout SEC, -t SEC         Request timeout in seconds (default: 5)

Device identification:
  --id ID, -i ID                Device ID
  --mac MAC, -m MAC             Device MAC address (e.g., 34:5F:45:26:76:2C)

ID management:
  --new-id ID, -n ID            New device ID for set-id command

Identify options:
  --duration SEC, -d SEC        Duration of identify sound in seconds (default: 30)

Auto-assign options:
  --prefix PREFIX, -p PREFIX    Prefix for auto-generated IDs (default: LOUD)
  --start-num NUM, -s NUM       Starting number for auto-generated IDs (default: 1)
```

#### Shortcut Examples

```bash
# Using shortcuts
python id_manager.py -c find-duplicates
python id_manager.py -c list-all
python id_manager.py -c set-id -m 34:5F:45:26:76:2C -n STAGE-01
python id_manager.py -c identify -i LOUDFRAME-001 -d 60
python id_manager.py -c auto-assign -p STAGE -s 100
```

## License

These tools are part of the ESP32 Audio Loop Controller project.
