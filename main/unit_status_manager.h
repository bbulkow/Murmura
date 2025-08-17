#ifndef UNIT_STATUS_MANAGER_H
#define UNIT_STATUS_MANAGER_H

#include "esp_err.h"
#include <stdbool.h>

// Maximum length for unit ID string
#define MAX_UNIT_ID_LEN 64

// Path to the unit ID file on SD card
#define UNIT_ID_FILE_PATH "/sdcard/unit_id.txt"

// Unit status structure
typedef struct {
    char mac_address[18];      // MAC address in format XX:XX:XX:XX:XX:XX
    char id[MAX_UNIT_ID_LEN];
    char ip_address[16];       // IP address in format XXX.XXX.XXX.XXX
    bool wifi_connected;
    char firmware_version[32];
    int uptime_seconds;
} unit_status_t;

/**
 * @brief Initialize the unit status manager
 * 
 * @return esp_err_t ESP_OK on success
 */
esp_err_t unit_status_init(void);

/**
 * @brief Get the current unit status
 * 
 * @param status Pointer to unit_status_t structure to populate
 * @return esp_err_t ESP_OK on success
 */
esp_err_t unit_status_get(unit_status_t *status);

/**
 * @brief Set the unit ID
 * 
 * @param unit_id The unit ID string to set (will be truncated if too long)
 * @return esp_err_t ESP_OK on success
 */
esp_err_t unit_status_set_id(const char *id);

/**
 * @brief Get the unit ID
 * 
 * @param unit_id Buffer to store the unit ID
 * @param max_len Maximum length of the buffer
 * @return esp_err_t ESP_OK on success
 */
esp_err_t unit_status_get_id(char *id, size_t max_len);

/**
 * @brief Load unit ID from SD card
 * 
 * @return esp_err_t ESP_OK on success, ESP_ERR_NOT_FOUND if file doesn't exist
 */
esp_err_t unit_status_load_from_sd(void);

/**
 * @brief Save unit ID to SD card
 * 
 * @return esp_err_t ESP_OK on success
 */
esp_err_t unit_status_save_to_sd(void);

/**
 * @brief Get the WiFi MAC address
 * 
 * @param mac_str Buffer to store MAC address string (min 18 bytes)
 * @return esp_err_t ESP_OK on success
 */
esp_err_t unit_status_get_mac_address(char *mac_str);

/**
 * @brief Get system uptime in seconds
 * 
 * @return int Uptime in seconds
 */
int unit_status_get_uptime(void);

#endif // UNIT_STATUS_MANAGER_H
