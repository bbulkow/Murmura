#include "unit_status_manager.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <string.h>
#include <stdio.h>
#include <sys/stat.h>

static const char *TAG = "UNIT_STATUS";

// Firmware version - update this with each release
#define FIRMWARE_VERSION "1.0.1"

// Static storage for unit ID
static char g_id[MAX_UNIT_ID_LEN] = "LOUDFRAME-001";  // Default unit ID
static bool g_initialized = false;

esp_err_t unit_status_init(void)
{
    if (g_initialized) {
        return ESP_OK;
    }

    // Try to load unit ID from SD card
    esp_err_t ret = unit_status_load_from_sd();
    if (ret == ESP_ERR_NOT_FOUND) {
        ESP_LOGI(TAG, "No unit ID file found, using default: %s", g_id);
        // Optionally save the default to SD card
        unit_status_save_to_sd();
    } else if (ret != ESP_OK) {
        ESP_LOGW(TAG, "Failed to load unit ID from SD card, using default");
    }

    g_initialized = true;
    ESP_LOGI(TAG, "Unit status manager initialized with ID: %s", g_id);
    
    return ESP_OK;
}

esp_err_t unit_status_get_mac_address(char *mac_str)
{
    if (!mac_str) {
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t mac[6];
    esp_err_t ret = esp_wifi_get_mac(WIFI_IF_STA, mac);
    if (ret != ESP_OK) {
        // If WiFi is not initialized, try to get base MAC
        ret = esp_efuse_mac_get_default(mac);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "Failed to get MAC address");
            return ret;
        }
    }

    sprintf(mac_str, "%02X:%02X:%02X:%02X:%02X:%02X",
            mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    
    return ESP_OK;
}

int unit_status_get_uptime(void)
{
    // Get system uptime in milliseconds and convert to seconds
    return (int)(esp_timer_get_time() / 1000000);
}

esp_err_t unit_status_get(unit_status_t *status)
{
    if (!status) {
        return ESP_ERR_INVALID_ARG;
    }

    memset(status, 0, sizeof(unit_status_t));

    // Get MAC address
    unit_status_get_mac_address(status->mac_address);

    // Copy unit ID
    strncpy(status->id, g_id, MAX_UNIT_ID_LEN - 1);
    status->id[MAX_UNIT_ID_LEN - 1] = '\0';

    // Get firmware version
    strncpy(status->firmware_version, FIRMWARE_VERSION, sizeof(status->firmware_version) - 1);

    // Get uptime
    status->uptime_seconds = unit_status_get_uptime();

    // Get WiFi status and IP address
    wifi_ap_record_t ap_info;
    esp_err_t ret = esp_wifi_sta_get_ap_info(&ap_info);
    if (ret == ESP_OK) {
        status->wifi_connected = true;
        
        // Get IP address
        esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
        if (netif) {
            esp_netif_ip_info_t ip_info;
            if (esp_netif_get_ip_info(netif, &ip_info) == ESP_OK) {
                sprintf(status->ip_address, IPSTR, IP2STR(&ip_info.ip));
            } else {
                strcpy(status->ip_address, "0.0.0.0");
            }
        }
    } else {
        status->wifi_connected = false;
        strcpy(status->ip_address, "0.0.0.0");
    }

    return ESP_OK;
}

esp_err_t unit_status_set_id(const char *id)
{
    if (!id) {
        return ESP_ERR_INVALID_ARG;
    }

    // Copy and truncate if necessary
    strncpy(g_id, id, MAX_UNIT_ID_LEN - 1);
    g_id[MAX_UNIT_ID_LEN - 1] = '\0';

    ESP_LOGI(TAG, "Unit ID set to: %s", g_id);

    // Save to SD card
    return unit_status_save_to_sd();
}

esp_err_t unit_status_get_id(char *id, size_t max_len)
{
    if (!id || max_len == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    strncpy(id, g_id, max_len - 1);
    id[max_len - 1] = '\0';

    return ESP_OK;
}

esp_err_t unit_status_load_from_sd(void)
{
    FILE *file = fopen(UNIT_ID_FILE_PATH, "r");
    if (!file) {
        ESP_LOGW(TAG, "Unit ID file not found: %s", UNIT_ID_FILE_PATH);
        return ESP_ERR_NOT_FOUND;
    }

    char buffer[MAX_UNIT_ID_LEN];
    memset(buffer, 0, sizeof(buffer));
    
    size_t read = fread(buffer, 1, sizeof(buffer) - 1, file);
    fclose(file);

    if (read == 0) {
        ESP_LOGW(TAG, "Unit ID file is empty");
        return ESP_ERR_INVALID_SIZE;
    }

    // Remove any trailing newline or whitespace
    for (int i = read - 1; i >= 0; i--) {
        if (buffer[i] == '\n' || buffer[i] == '\r' || buffer[i] == ' ' || buffer[i] == '\t') {
            buffer[i] = '\0';
        } else {
            break;
        }
    }

    if (strlen(buffer) > 0) {
        strncpy(g_id, buffer, MAX_UNIT_ID_LEN - 1);
        g_id[MAX_UNIT_ID_LEN - 1] = '\0';
        ESP_LOGI(TAG, "Loaded unit ID from SD card: %s", g_id);
        return ESP_OK;
    }

    return ESP_ERR_INVALID_SIZE;
}

esp_err_t unit_status_save_to_sd(void)
{
    // Check if SD card is mounted
    struct stat st;
    if (stat("/sdcard", &st) != 0) {
        ESP_LOGE(TAG, "SD card not mounted");
        return ESP_ERR_NOT_FOUND;
    }

    FILE *file = fopen(UNIT_ID_FILE_PATH, "w");
    if (!file) {
        ESP_LOGE(TAG, "Failed to open unit ID file for writing: %s", UNIT_ID_FILE_PATH);
        return ESP_FAIL;
    }

    size_t written = fwrite(g_id, 1, strlen(g_id), file);
    fclose(file);

    if (written != strlen(g_id)) {
        ESP_LOGE(TAG, "Failed to write complete unit ID to file");
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "Unit ID saved to SD card: %s", g_id);
    return ESP_OK;
}
