# WiFi Network Interface Setup with Multi-Network Support

This document describes the WiFi networking functionality added to the ESP-ADF audio system.

## Overview

The system now includes an advanced WiFi manager that:
- **Stores multiple WiFi networks** (up to 10) in NVS (Non-Volatile Storage)
- **Scans for available networks** and automatically selects the best one
- **Remembers authentication failures** and skips problematic networks
- **Automatically fails over** to the next best network if connection fails
- **Prioritizes by signal strength** when multiple stored networks are available
- Handles connection retries and failures gracefully
- Continues audio playback functionality even if WiFi connection fails

## Files Added

- `main/wifi_manager.h` - WiFi manager API declarations
- `main/wifi_manager.c` - WiFi manager implementation
- `main/wifi_config_util.h` - Utility functions for WiFi configuration
- `main/play_sdcard.c` - Modified to initialize WiFi on startup

## How to Configure WiFi

### Adding Multiple Networks

The system supports storing up to 10 WiFi networks. Add networks using:

```c
// Add multiple networks
wifi_manager_add_network("HomeWiFi", "HomePassword");
wifi_manager_add_network("OfficeWiFi", "OfficePassword");
wifi_manager_add_network("MobileHotspot", "HotspotPassword");
```

### Method 1: Pre-compile Configuration

Modify your initialization code to add multiple networks:

```c
// In app_main or initialization function:
wifi_manager_add_network("Network1_SSID", "Network1_Password");
wifi_manager_add_network("Network2_SSID", "Network2_Password");
wifi_manager_add_network("Network3_SSID", "Network3_Password");
```

### Method 2: Using ESP-IDF Monitor

You can use the ESP-IDF partition tool to write credentials directly to NVS:

```bash
# Write SSID
python $IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py generate wifi_config.csv wifi_config.bin 0x6000

# Content of wifi_config.csv:
key,type,encoding,value
wifi_config,namespace,,
ssid,data,string,YourWiFiSSID
password,data,string,YourWiFiPassword

# Flash the NVS partition
esptool.py --port COM_PORT write_flash 0x9000 wifi_config.bin
```

### Method 3: Runtime Configuration

Add code to handle button press or serial input to configure WiFi:

```c
// Example: Configure WiFi when REC button is pressed
if ((int) msg.data == get_input_rec_id()) {
    ESP_LOGI(TAG, "Configuring WiFi...");
    wifi_manager_save_credentials("YourSSID", "YourPassword");
    wifi_manager_reconnect();
}
```

## NVS Storage Details

WiFi credentials are stored in NVS with:
- **Namespace**: `wifi_config`
- **Network Count Key**: `network_count`
- **SSID Keys**: `ssid_0`, `ssid_1`, ... `ssid_9`
- **Password Keys**: `pass_0`, `pass_1`, ... `pass_9`
- **Auth Failure Keys**: `fail_0`, `fail_1`, ... `fail_9`

## API Functions

### Core Functions

- `wifi_manager_init()` - Initialize WiFi, scan networks, and connect to best available
- `wifi_manager_deinit()` - Cleanup WiFi resources
- `wifi_manager_reconnect()` - Trigger new scan and reconnection attempt
- `wifi_manager_is_connected()` - Check connection status
- `wifi_manager_get_ip_string()` - Get current IP address
- `wifi_manager_get_connected_ssid()` - Get the SSID of connected network

### Multi-Network Management

- `wifi_manager_add_network()` - Add a new network or update existing
- `wifi_manager_remove_network()` - Remove a network from storage
- `wifi_manager_get_stored_networks()` - Get list of all stored networks
- `wifi_manager_clear_all_networks()` - Clear all stored networks
- `wifi_manager_clear_auth_failure()` - Clear auth failure for specific network
- `wifi_manager_clear_all_auth_failures()` - Reset all auth failure flags

### Legacy Functions (for backward compatibility)

- `wifi_manager_save_credentials()` - Alias for add_network
- `wifi_manager_clear_credentials()` - Alias for clear_all_networks

### Example Usage

```c
// Add multiple networks
wifi_manager_add_network("HomeWiFi", "HomePassword123");
wifi_manager_add_network("OfficeWiFi", "WorkPass456");
wifi_manager_add_network("CafeWiFi", "CoffeeTime789");

// Initialize WiFi - will scan and connect to best available
esp_err_t ret = wifi_manager_init();
if (ret == ESP_OK) {
    ESP_LOGI(TAG, "WiFi connected");
    
    // Get connected network name
    char ssid[33];
    if (wifi_manager_get_connected_ssid(ssid, sizeof(ssid)) == ESP_OK) {
        ESP_LOGI(TAG, "Connected to: %s", ssid);
    }
    
    // Get IP address
    char ip_str[16];
    if (wifi_manager_get_ip_string(ip_str, sizeof(ip_str)) == ESP_OK) {
        ESP_LOGI(TAG, "IP: %s", ip_str);
    }
}

// List stored networks
wifi_network_entry_t networks[10];
size_t count;
if (wifi_manager_get_stored_networks(networks, 10, &count) == ESP_OK) {
    for (int i = 0; i < count; i++) {
        ESP_LOGI(TAG, "Network %d: %s (Auth failed: %d, Available: %d, RSSI: %d)",
                 i, networks[i].ssid, networks[i].auth_failed, 
                 networks[i].available, networks[i].rssi);
    }
}

// Clear auth failure and reconnect if password was fixed
wifi_manager_clear_auth_failure("HomeWiFi");
wifi_manager_reconnect();

// Remove a network
wifi_manager_remove_network("OldWiFi");
```

## Connection Behavior

1. On startup, the system reads all stored WiFi networks from NVS
2. Performs a WiFi scan to find available access points
3. Matches scan results with stored credentials
4. Selects the network with the strongest signal (RSSI) that hasn't failed authentication
5. Attempts connection with up to 2 retries per network
6. On authentication failure:
   - Marks the network as failed (persisted to NVS)
   - Automatically tries the next best available network
7. Continues until successful connection or all networks exhausted
8. Audio playback functionality is not affected by WiFi status

### Network Selection Priority

Networks are prioritized by:
1. **Authentication status** - Networks without auth failures are tried first
2. **Signal strength (RSSI)** - Stronger signals are preferred
3. **Availability** - Only networks found in scan are attempted

## Troubleshooting

### No WiFi Connection
- Check if credentials are stored in NVS
- Verify SSID and password are correct
- Check WiFi signal strength
- Review serial output for connection errors

### Managing Networks

```c
// Clear all stored networks
wifi_manager_clear_all_networks();

// Clear specific network's auth failure
wifi_manager_clear_auth_failure("NetworkName");

// Clear all auth failures (useful after password changes)
wifi_manager_clear_all_auth_failures();
```

### Debug Output
The WiFi manager logs detailed information at INFO and ERROR levels with tag "WIFI_MANAGER".

## Future Enhancements

Potential improvements for WiFi configuration:
1. **Web Server Configuration**: Add an HTTP server for WiFi setup via web interface
2. **AP Mode**: Create an access point for initial configuration
3. **Bluetooth Configuration**: Use BLE for credential provisioning
4. **SD Card Config**: Read WiFi settings from a config file on SD card
5. **Smart Config**: Implement ESP SmartConfig or ESP-Touch for easy setup
6. **Multiple Networks**: Support multiple WiFi networks with fallback

## Building and Flashing

```bash
# Build the project
idf.py build

# Flash to device
idf.py -p COM_PORT flash

# Monitor output
idf.py -p COM_PORT monitor
```

## Notes

- The C/C++ IntelliSense errors in VSCode are expected and don't affect compilation
- WiFi functionality is independent of audio playback
- Credentials are stored persistently and survive power cycles
- The system uses WPA2-PSK authentication by default
