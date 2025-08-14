#ifndef WIFI_CONFIG_UTIL_H
#define WIFI_CONFIG_UTIL_H

#include "wifi_manager.h"

/**
 * Example utility function to configure WiFi credentials.
 * This can be called from a button handler or serial console command
 * to set up initial WiFi configuration.
 * 
 * Example usage in your code:
 * 
 * // To set WiFi credentials programmatically:
 * wifi_config_util_set_credentials("YourWiFiSSID", "YourWiFiPassword");
 * 
 * // Then restart or reinitialize WiFi:
 * wifi_manager_reconnect();
 */
static inline esp_err_t wifi_config_util_set_credentials(const char *ssid, const char *password)
{
    esp_err_t ret = wifi_manager_save_credentials(ssid, password);
    if (ret == ESP_OK) {
        ESP_LOGI("WIFI_CONFIG", "WiFi credentials saved successfully");
        ESP_LOGI("WIFI_CONFIG", "SSID: %s", ssid);
        ESP_LOGI("WIFI_CONFIG", "Restart the device or call wifi_manager_reconnect() to connect");
    } else {
        ESP_LOGE("WIFI_CONFIG", "Failed to save WiFi credentials: %s", esp_err_to_name(ret));
    }
    return ret;
}

/**
 * Example function to handle WiFi configuration via serial input or button press.
 * This demonstrates how you might implement a configuration mode.
 */
static inline void wifi_config_util_example_handler(void)
{
    // Example: Configure WiFi with hardcoded credentials
    // In a real implementation, you might:
    // - Read from serial console
    // - Use a web server in AP mode
    // - Use Bluetooth for configuration
    // - Read from a configuration file on SD card
    
    const char *example_ssid = "YourWiFiNetwork";
    const char *example_password = "YourWiFiPassword";
    
    ESP_LOGI("WIFI_CONFIG", "Example: Setting WiFi credentials...");
    wifi_config_util_set_credentials(example_ssid, example_password);
}

#endif // WIFI_CONFIG_UTIL_H
