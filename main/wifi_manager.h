#ifndef WIFI_MANAGER_H
#define WIFI_MANAGER_H

#include "esp_err.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include <stdbool.h>

// NVS namespace and keys for WiFi credentials
#define WIFI_NVS_NAMESPACE "wifi_config"
#define WIFI_NVS_COUNT_KEY "network_count"
#define WIFI_NVS_SSID_PREFIX "ssid_"
#define WIFI_NVS_PASSWORD_PREFIX "pass_"
#define WIFI_NVS_AUTH_FAIL_PREFIX "fail_"

// Maximum number of stored networks
#define WIFI_MAX_NETWORKS 10

// Maximum retry attempts per network
#define WIFI_RETRY_PER_NETWORK 2

// Maximum retry attempts
#define WIFI_MAXIMUM_RETRY 5

// WiFi connection states
typedef enum {
    WIFIMAN_STATE_DISCONNECTED,
    WIFIMAN_STATE_SCANNING,
    WIFIMAN_STATE_CONNECTING,
    WIFIMAN_STATE_CONNECTED,
    WIFIMAN_STATE_CONNECTION_FAILED,
    WIFIMAN_STATE_ERROR
} wifiman_state_t;

// WiFi network entry structure
typedef struct {
    char ssid[33];              // SSID (max 32 chars + null terminator)
    char password[65];          // Password (max 64 chars + null terminator)
    bool available;             // Whether this network was found in scan
    uint8_t auth_fail_count;   // Number of consecutive authentication failures
    int8_t rssi;                // Signal strength when available
} wifiman_network_entry_t;

// WiFi configuration structure
typedef struct {
    wifiman_network_entry_t networks[WIFI_MAX_NETWORKS];
    uint8_t network_count;
    uint8_t current_network_index;
} wifiman_config_t;

/**
 * Initialize WiFi manager
 * - Initializes WiFi in station mode
 * - Reads credentials from NVS
 * - Attempts to connect to the configured access point
 * 
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t wifi_manager_init(void);

/**
 * Deinitialize WiFi manager
 * - Disconnects from WiFi
 * - Stops WiFi
 * 
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t wifi_manager_deinit(void);

/**
 * Get current WiFi connection state
 * 
 * @return Current WiFi state
 */
wifiman_state_t wifi_manager_get_state(void);

/**
 * Check if WiFi is connected
 * 
 * @return true if connected, false otherwise
 */
bool wifi_manager_is_connected(void);

/**
 * Add a WiFi network to the stored list
 * 
 * @param ssid SSID of the access point
 * @param password Password for the access point
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t wifi_manager_add_network(const char *ssid, const char *password);

/**
 * Remove a WiFi network from the stored list
 * 
 * @param ssid SSID of the network to remove
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t wifi_manager_remove_network(const char *ssid);

/**
 * Get list of stored networks
 * 
 * @param networks Array to fill with network information
 * @param max_networks Maximum number of networks to retrieve
 * @param count Pointer to store actual number of networks retrieved
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t wifi_manager_get_stored_networks(wifiman_network_entry_t *networks, size_t max_networks, size_t *count);

/**
 * Clear authentication failure flag for a network
 * 
 * @param ssid SSID of the network
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t wifi_manager_clear_auth_failure(const char *ssid);

/**
 * Clear all authentication failure flags
 * 
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t wifi_manager_clear_all_auth_failures(void);

/**
 * Get the currently connected network SSID
 * 
 * @param ssid Buffer to store the SSID (should be at least 33 bytes)
 * @param len Length of the buffer
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t wifi_manager_get_connected_ssid(char *ssid, size_t len);

/**
 * Clear all WiFi credentials from NVS
 * 
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t wifi_manager_clear_all_networks(void);

/**
 * Legacy function - saves single credential (adds to list)
 * 
 * @param ssid SSID of the access point
 * @param password Password for the access point
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t wifi_manager_save_credentials(const char *ssid, const char *password);

/**
 * Read all WiFi credentials from NVS
 * 
 * @param config Pointer to wifiman_config_t structure to fill
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t wifi_manager_read_credentials(wifiman_config_t *config);

/**
 * Legacy function - clears all networks
 * 
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t wifi_manager_clear_credentials(void);

/**
 * Get IP address as string
 * 
 * @param ip_str Buffer to store IP address string (should be at least 16 bytes)
 * @param len Length of the buffer
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t wifi_manager_get_ip_string(char *ip_str, size_t len);

/**
 * Trigger a reconnection attempt
 * 
 * @return ESP_OK on success, error code otherwise
 */
esp_err_t wifi_manager_reconnect(void);

#endif // WIFI_MANAGER_H
