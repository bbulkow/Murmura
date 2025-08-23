# ESP32 Network Device Management Tools

This repository contains Python tools for discovering and managing multiple ESP32 audio loop controller devices on a network.

## Features

- **Network Scanner** (`device_scanner.py`): Discovers all ESP32 devices on your network
- **Batch Controller** (`batch_controller.py`): Performs batch operations on multiple devices
- **Device Controller** (`device_controller.py`): Controls a single device by ID
- **Asynchronous Operations**: Fast parallel scanning and control of multiple devices
- **Device Tracking**: Maintains a JSON map of all devices with MAC addresses as unique identifiers

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
  --net CIDR, --network CIDR
                        Network range in CIDR format (e.g., 192.168.1.0/24)
  --action {create,add,update}, --mode {create,add,update}
                        Scan action: create (new map), add (merge), or update (existing only)

Optional arguments:
  --timeout SEC         Connection timeout in seconds (default: 2)
  --concurrent NUM      Maximum concurrent connections (default: 50)
  --map-file PATH       Path to device map JSON file (default: device_map.json)
  --verbose, -v         Enable verbose debug logging
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
  --map-file PATH       Path to device map JSON file (default: device_map.json)
  --timeout SEC         Request timeout in seconds (default: 5)
  --concurrent NUM      Maximum concurrent connections (default: 10)

Volume control:
  --track {0,1,2}       Track number for track-specific commands
  --volume LEVEL        Volume level (0-100)
  --global              Set global volume instead of track volume

Device filters:
  --all-devices         Include offline devices (default: online only)
  --filter-id REGEX     Filter devices by ID pattern (regex)
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
  --id ID               Device ID to control
  --command {status,stop,start,set-volume,set-id,save-config,load-config,get-loops,set-file}, -c
                        Command to execute on the device

Optional arguments:
  --map-file PATH       Path to device map JSON file (default: device_map.json)
  --timeout SEC         Request timeout in seconds (default: 5)

Volume control:
  --track {0,1,2}       Track number for track-specific commands
  --volume LEVEL        Volume level (0-100)
  --global              Set global volume instead of track volume

Device ID control:
  --new-id ID           New device ID for set-id command

File control:
  --file-index INDEX    File index from /api/files for set-file command
  --file-path PATH      Direct file path for set-file command
```

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

## Advanced Usage

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
- Devices must be running the ESP32 Audio Loop Controller firmware

## License

These tools are part of the ESP32 Audio Loop Controller project.
