#include "wifi_manager.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include <string.h>
#include <stdio.h>

static const char *TAG = "WIFI_MANAGER";

// #define DEBUG 1

// FreeRTOS event group to signal when we are connected
static EventGroupHandle_t s_wifi_event_group;

// Event bits
#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1
#define WIFI_SCAN_DONE_BIT BIT2
#define WIFI_STOP_BIT      BIT3

// WiFi state
static wifiman_state_t s_wifi_state = WIFIMAN_STATE_DISCONNECTED;
static int s_retry_num = 0;
static esp_netif_t *s_sta_netif = NULL;

// Stored WiFi configuration
static wifiman_config_t s_stored_config;
static int s_current_network_index = -1;
static char s_connected_ssid[33] = {0};
static bool s_auth_failure = false;

// Scan results
static wifi_ap_record_t *s_scan_results = NULL;
static uint16_t s_scan_count = 0;

// Background task handle
static TaskHandle_t s_wifi_task_handle = NULL;
static SemaphoreHandle_t s_wifi_mutex = NULL;

// Reconnection parameters
static uint32_t s_reconnect_interval_ms = 10000;  // Start with 10 seconds
static const uint32_t MAX_RECONNECT_INTERVAL_MS = 120000;  // Max 2 minutes

// Forward declarations
static esp_err_t wifi_manager_try_next_network(void);
static esp_err_t wifi_manager_scan_networks(void);
static int wifi_manager_find_best_network(void);
static void wifi_manager_background_task(void *pvParameters);

// Event handler for WiFi events
static void wifi_event_handler(void* arg, esp_event_base_t event_base,
                                int32_t event_id, void* event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        ESP_LOGI(TAG, "WiFi started");
        xEventGroupSetBits(s_wifi_event_group, WIFI_SCAN_DONE_BIT);
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_SCAN_DONE) {
        ESP_LOGI(TAG, "WiFi scan completed");
        xEventGroupSetBits(s_wifi_event_group, WIFI_SCAN_DONE_BIT);
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t* event = (wifi_event_sta_disconnected_t*) event_data;
        ESP_LOGI(TAG, "Disconnected from AP. Reason: %d", event->reason);
        
        xSemaphoreTake(s_wifi_mutex, portMAX_DELAY);
        
        // Check if this was an authentication failure
        if (event->reason == WIFI_REASON_AUTH_FAIL || 
            event->reason == WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT ||
            event->reason == WIFI_REASON_HANDSHAKE_TIMEOUT) {
            s_auth_failure = true;
            ESP_LOGW(TAG, "Authentication failed for network index %d", s_current_network_index);
            
            // Increment auth failure counter for this network (cap at 10 to prevent NVS wear)
            if (s_current_network_index >= 0 && s_current_network_index < s_stored_config.network_count) {
                uint8_t current_count = s_stored_config.networks[s_current_network_index].auth_fail_count;
                
                // Only increment if below 10 to prevent unbounded NVS writes
                if (current_count < 10) {
                    s_stored_config.networks[s_current_network_index].auth_fail_count++;
                    ESP_LOGI(TAG, "Network %s auth fail count now: %d", 
                             s_stored_config.networks[s_current_network_index].ssid,
                             s_stored_config.networks[s_current_network_index].auth_fail_count);
                    
                    // Save updated auth failure counter to NVS
                    nvs_handle_t nvs_handle;
                    if (nvs_open(WIFI_NVS_NAMESPACE, NVS_READWRITE, &nvs_handle) == ESP_OK) {
                        char key[16];
                        snprintf(key, sizeof(key), "%s%d", WIFI_NVS_AUTH_FAIL_PREFIX, s_current_network_index);
                        nvs_set_u8(nvs_handle, key, s_stored_config.networks[s_current_network_index].auth_fail_count);
                        nvs_commit(nvs_handle);
                        nvs_close(nvs_handle);
                    }
                } else {
                    ESP_LOGI(TAG, "Network %s auth fail count capped at 10 (NVS wear protection)", 
                             s_stored_config.networks[s_current_network_index].ssid);
                }
            }
        }
        
        s_wifi_state = WIFIMAN_STATE_DISCONNECTED;
        xEventGroupSetBits(s_wifi_event_group, WIFI_FAIL_BIT);
        
        xSemaphoreGive(s_wifi_mutex);
        
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t* event = (ip_event_got_ip_t*) event_data;
        ESP_LOGI(TAG, "Got IP address: " IPSTR, IP2STR(&event->ip_info.ip));
        
        xSemaphoreTake(s_wifi_mutex, portMAX_DELAY);
        ESP_LOGI(TAG, "Connected to SSID: %s", s_connected_ssid);
        s_retry_num = 0;
        s_auth_failure = false;
        s_wifi_state = WIFIMAN_STATE_CONNECTED;
        s_reconnect_interval_ms = 10000;  // Reset reconnect interval
        
        // Clear auth failure counter on successful connection
        if (s_current_network_index >= 0 && s_current_network_index < s_stored_config.network_count) {
            if (s_stored_config.networks[s_current_network_index].auth_fail_count > 0) {
                ESP_LOGI(TAG, "Clearing auth failure counter for %s (was %d)", 
                         s_stored_config.networks[s_current_network_index].ssid,
                         s_stored_config.networks[s_current_network_index].auth_fail_count);
                s_stored_config.networks[s_current_network_index].auth_fail_count = 0;
                
                // Update NVS to clear the counter
                nvs_handle_t nvs_handle;
                if (nvs_open(WIFI_NVS_NAMESPACE, NVS_READWRITE, &nvs_handle) == ESP_OK) {
                    char key[16];
                    snprintf(key, sizeof(key), "%s%d", WIFI_NVS_AUTH_FAIL_PREFIX, s_current_network_index);
                    nvs_erase_key(nvs_handle, key);  // Erase the key (0 is default)
                    nvs_commit(nvs_handle);
                    nvs_close(nvs_handle);
                }
            }
        }
        
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
        xSemaphoreGive(s_wifi_mutex);
    }
}

static esp_err_t wifi_manager_scan_networks(void)
{
    ESP_LOGI(TAG, "Starting WiFi scan...");
    
    wifi_scan_config_t scan_config = {
        .ssid = NULL,
        .bssid = NULL,
        .channel = 0,
        .show_hidden = false,
        .scan_type = WIFI_SCAN_TYPE_ACTIVE,
        .scan_time.active.min = 100,
        .scan_time.active.max = 300
    };
    
    xEventGroupClearBits(s_wifi_event_group, WIFI_SCAN_DONE_BIT);
    
    esp_err_t ret = esp_wifi_scan_start(&scan_config, false);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start WiFi scan: %s", esp_err_to_name(ret));
        return ret;
    }
    
    return ESP_OK;
}

static int wifi_manager_find_best_network(void)
{
    int best_index = -1;
    uint8_t lowest_fail_count = 255;
    int8_t best_rssi = -127;

#ifdef DEBUG
    // Print all stored networks for debugging
    ESP_LOGI(TAG, "=== STORED NETWORKS ===");
    for (int i = 0; i < s_stored_config.network_count; i++) {
        ESP_LOGI(TAG, "Stored[%d]: SSID='%s', Auth_fail_count=%d", 
                 i, s_stored_config.networks[i].ssid, 
                 s_stored_config.networks[i].auth_fail_count);
    }
    
    // Print all scanned networks for debugging
    ESP_LOGI(TAG, "=== SCANNED NETWORKS ===");
    for (int i = 0; i < s_scan_count && i < 20; i++) {
        ESP_LOGI(TAG, "Scan[%d]: SSID='%s', RSSI=%d, Channel=%d", 
                 i, (char *)s_scan_results[i].ssid, 
                 s_scan_results[i].rssi, s_scan_results[i].primary);
    }
#endif    

    // First, mark all stored networks as not available
    for (int i = 0; i < s_stored_config.network_count; i++) {
        s_stored_config.networks[i].available = false;
        s_stored_config.networks[i].rssi = -127;
        // Add small delay to yield CPU to audio tasks
        vTaskDelay(pdMS_TO_TICKS(1));
    }
    
    // Match scan results with stored networks
#ifdef DEBUG
    ESP_LOGI(TAG, "=== MATCHING PROCESS ===");
#endif
    for (int i = 0; i < s_scan_count; i++) {
        for (int j = 0; j < s_stored_config.network_count; j++) {
            if (strcmp((char *)s_scan_results[i].ssid, s_stored_config.networks[j].ssid) == 0) {
#ifdef DEBUG
                ESP_LOGI(TAG, "MATCH FOUND! Network: %s (RSSI: %d, Auth fail count: %d)", 
                         s_stored_config.networks[j].ssid, 
                         s_scan_results[i].rssi,
                         s_stored_config.networks[j].auth_fail_count);
#endif
                
                s_stored_config.networks[j].available = true;
                s_stored_config.networks[j].rssi = s_scan_results[i].rssi;
            }
            // Add small delay after each comparison to yield CPU
            vTaskDelay(pdMS_TO_TICKS(1));
        }
        // Add delay every few iterations to yield more CPU to audio
        if (i % 3 == 0) {
            vTaskDelay(pdMS_TO_TICKS(2));
        }
    }
    
    // Find the best available network (lowest failure count, then strongest signal)
    // Networks are prioritized by:
    // 1. Lowest auth failure count
    // 2. Strongest RSSI signal
    for (int i = 0; i < s_stored_config.network_count; i++) {
        if (!s_stored_config.networks[i].available) {
            continue;  // Skip unavailable networks
        }
        
        // Select network if it has fewer failures, or same failures but better signal
        if (s_stored_config.networks[i].auth_fail_count < lowest_fail_count ||
            (s_stored_config.networks[i].auth_fail_count == lowest_fail_count && 
             s_stored_config.networks[i].rssi > best_rssi)) {
            
            lowest_fail_count = s_stored_config.networks[i].auth_fail_count;
            best_rssi = s_stored_config.networks[i].rssi;
            best_index = i;
#ifdef DEBUG            
            ESP_LOGI(TAG, "New best network candidate: %s (index=%d, fail_count=%d, RSSI=%d)",
                     s_stored_config.networks[i].ssid, i, lowest_fail_count, best_rssi);
#endif
        }
    }
    
#ifdef DEBUG
    if (best_index >= 0) {
        ESP_LOGI(TAG, "=== RESULT: Best network: %s (index=%d, fail_count=%d, RSSI=%d) ===", 
                 s_stored_config.networks[best_index].ssid, best_index, 
                 s_stored_config.networks[best_index].auth_fail_count, best_rssi);
    } else {
        ESP_LOGI(TAG, "=== RESULT: No suitable network found ===");
    }
#endif
    
    return best_index;
}

static esp_err_t wifi_manager_try_next_network(void)
{
    // Find next available network
    int next_index = -1;
    int8_t best_rssi = -127;
    
    for (int i = 0; i < s_stored_config.network_count; i++) {
        // Skip current network and networks with high auth failure count or not available
        if (i == s_current_network_index || 
            s_stored_config.networks[i].auth_fail_count >= 10 ||
            !s_stored_config.networks[i].available) {
            continue;
        }
        
        // Find the strongest available network
        if (s_stored_config.networks[i].rssi > best_rssi) {
            best_rssi = s_stored_config.networks[i].rssi;
            next_index = i;
        }
    }
    
    if (next_index < 0) {
        ESP_LOGW(TAG, "No more networks to try");
        return ESP_FAIL;
    }
    
    // Configure and connect to the next network
    s_current_network_index = next_index;
    wifiman_network_entry_t *network = &s_stored_config.networks[next_index];
    
    ESP_LOGI(TAG, "Trying to connect to network: %s (index: %d)", 
             network->ssid, next_index);
    
    wifi_config_t wifi_config = {
        .sta = {
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
            .pmf_cfg = {
                .capable = true,
                .required = false
            },
        },
    };
    
    strcpy((char *)wifi_config.sta.ssid, network->ssid);
    strcpy((char *)wifi_config.sta.password, network->password);
    strcpy(s_connected_ssid, network->ssid);
    
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    
    s_wifi_state = WIFIMAN_STATE_CONNECTING;
    s_retry_num = 0;
    s_auth_failure = false;
    
    return esp_wifi_connect();
}

static void wifi_manager_background_task(void *pvParameters)
{
    ESP_LOGI(TAG, "WiFi background task started on core %d", xPortGetCoreID());
    EventBits_t bits;
    
    // Add initial delay to let audio stabilize before starting WiFi operations
    vTaskDelay(pdMS_TO_TICKS(3000));  // 3 second delay
    ESP_LOGI(TAG, "Starting WiFi operations after startup delay");
    
    while (1) {
        // Check if we should stop
        bits = xEventGroupGetBits(s_wifi_event_group);
        if (bits & WIFI_STOP_BIT) {
            ESP_LOGI(TAG, "WiFi background task stopping");
            break;
        }
        
        xSemaphoreTake(s_wifi_mutex, portMAX_DELAY);
        wifiman_state_t current_state = s_wifi_state;
        xSemaphoreGive(s_wifi_mutex);
        
        switch (current_state) {
            case WIFIMAN_STATE_DISCONNECTED:
            case WIFIMAN_STATE_CONNECTION_FAILED:
                // Start scanning
                ESP_LOGI(TAG, "Starting WiFi scan from background task");
                xSemaphoreTake(s_wifi_mutex, portMAX_DELAY);
                s_wifi_state = WIFIMAN_STATE_SCANNING;
                xSemaphoreGive(s_wifi_mutex);
                
                wifi_manager_scan_networks();
                
                // Wait for scan to complete
                bits = xEventGroupWaitBits(s_wifi_event_group,
                                          WIFI_SCAN_DONE_BIT,
                                          pdTRUE,
                                          pdFALSE,
                                          pdMS_TO_TICKS(5000));
                
                if (bits & WIFI_SCAN_DONE_BIT) {
                    // Get scan results
                    uint16_t ap_count = 0;
                    esp_wifi_scan_get_ap_num(&ap_count);
                    
                    if (ap_count > 0) {
                        // Free previous scan results
                        if (s_scan_results) {
                            free(s_scan_results);
                        }
                        
                        s_scan_results = heap_caps_malloc(ap_count * sizeof(wifi_ap_record_t), MALLOC_CAP_SPIRAM);
                        if (s_scan_results) {
                            s_scan_count = ap_count;
                            esp_wifi_scan_get_ap_records(&s_scan_count, s_scan_results);
                            
                            ESP_LOGI(TAG, "Found %d access points", s_scan_count);
                            
                            // Find best network to connect
                            int best_index = wifi_manager_find_best_network();
                            
                            if (best_index >= 0) {
                                xSemaphoreTake(s_wifi_mutex, portMAX_DELAY);
                                s_current_network_index = best_index;
                                xSemaphoreGive(s_wifi_mutex);
                                
                                // Try to connect
                                wifiman_network_entry_t *network = &s_stored_config.networks[best_index];
                                
                                wifi_config_t wifi_config = {
                                    .sta = {
                                        .threshold.authmode = WIFI_AUTH_WPA2_PSK,
                                        .pmf_cfg = {
                                            .capable = true,
                                            .required = false
                                        },
                                    },
                                };
                                
                                strcpy((char *)wifi_config.sta.ssid, network->ssid);
                                strcpy((char *)wifi_config.sta.password, network->password);
                                strcpy(s_connected_ssid, network->ssid);
                                
                                ESP_LOGI(TAG, "Connecting to: %s", network->ssid);
                                ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
                                
                                xSemaphoreTake(s_wifi_mutex, portMAX_DELAY);
                                s_wifi_state = WIFIMAN_STATE_CONNECTING;
                                xSemaphoreGive(s_wifi_mutex);
                                
                                esp_wifi_connect();
                                
                                // Wait for connection result
                                bits = xEventGroupWaitBits(s_wifi_event_group,
                                                          WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
                                                          pdTRUE,
                                                          pdFALSE,
                                                          pdMS_TO_TICKS(30000));
                                
                                if (bits & WIFI_CONNECTED_BIT) {
                                    ESP_LOGI(TAG, "Connected successfully");
                                } else {
                                    ESP_LOGW(TAG, "Connection failed, will retry");
                                    xSemaphoreTake(s_wifi_mutex, portMAX_DELAY);
                                    s_wifi_state = WIFIMAN_STATE_CONNECTION_FAILED;
                                    xSemaphoreGive(s_wifi_mutex);
                                }
                            } else {
                                ESP_LOGW(TAG, "No suitable network found");
                                xSemaphoreTake(s_wifi_mutex, portMAX_DELAY);
                                s_wifi_state = WIFIMAN_STATE_CONNECTION_FAILED;
                                xSemaphoreGive(s_wifi_mutex);
                            }
                            
                            free(s_scan_results);
                            s_scan_results = NULL;
                        }
                    }
                }
                
                // If still not connected, wait before retrying
                xSemaphoreTake(s_wifi_mutex, portMAX_DELAY);
                current_state = s_wifi_state;
                xSemaphoreGive(s_wifi_mutex);
                
                if (current_state != WIFIMAN_STATE_CONNECTED) {
                    ESP_LOGI(TAG, "Waiting %lu seconds before next connection attempt", 
                             (unsigned long)(s_reconnect_interval_ms / 1000));
                    vTaskDelay(pdMS_TO_TICKS(s_reconnect_interval_ms));
                    
                    // Increase reconnect interval with exponential backoff
                    s_reconnect_interval_ms = s_reconnect_interval_ms * 2;
                    if (s_reconnect_interval_ms > MAX_RECONNECT_INTERVAL_MS) {
                        s_reconnect_interval_ms = MAX_RECONNECT_INTERVAL_MS;
                    }
                }
                break;
                
            case WIFIMAN_STATE_CONNECTED:
                // Just wait, nothing to do while connected
                vTaskDelay(pdMS_TO_TICKS(5000));
                break;
                
            case WIFIMAN_STATE_SCANNING:
            case WIFIMAN_STATE_CONNECTING:
                // Wait for state to change
                vTaskDelay(pdMS_TO_TICKS(1000));
                break;
                
            default:
                vTaskDelay(pdMS_TO_TICKS(1000));
                break;
        }
    }
    
    s_wifi_task_handle = NULL;
    vTaskDelete(NULL);
}

esp_err_t wifi_manager_init_async(void)
{
    ESP_LOGI(TAG, "Initializing WiFi manager with async multi-network support");
    
    // Create mutex
    s_wifi_mutex = xSemaphoreCreateMutex();
    if (s_wifi_mutex == NULL) {
        ESP_LOGE(TAG, "Failed to create mutex");
        return ESP_FAIL;
    }
    
    // Create event group
    s_wifi_event_group = xEventGroupCreate();
    if (s_wifi_event_group == NULL) {
        ESP_LOGE(TAG, "Failed to create event group");
        vSemaphoreDelete(s_wifi_mutex);
        return ESP_FAIL;
    }

    // Initialize TCP/IP stack
    ESP_ERROR_CHECK(esp_netif_init());

    // Create default event loop if not already created
    esp_err_t ret = esp_event_loop_create_default();
    if (ret != ESP_OK && ret != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "Failed to create default event loop: %s", esp_err_to_name(ret));
        vEventGroupDelete(s_wifi_event_group);
        vSemaphoreDelete(s_wifi_mutex);
        return ret;
    }
    
    // Create default WiFi station
    s_sta_netif = esp_netif_create_default_wifi_sta();
    if (s_sta_netif == NULL) {
        ESP_LOGE(TAG, "Failed to create default WiFi station");
        vEventGroupDelete(s_wifi_event_group);
        vSemaphoreDelete(s_wifi_mutex);
        return ESP_FAIL;
    }

    // Initialize WiFi with default config
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    // Register event handlers
    esp_event_handler_instance_t instance_any_id;
    esp_event_handler_instance_t instance_got_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT,
                                                        ESP_EVENT_ANY_ID,
                                                        &wifi_event_handler,
                                                        NULL,
                                                        &instance_any_id));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT,
                                                        IP_EVENT_STA_GOT_IP,
                                                        &wifi_event_handler,
                                                        NULL,
                                                        &instance_got_ip));

    // Read WiFi credentials from NVS
    ret = wifi_manager_read_credentials(&s_stored_config);
    
    if (ret == ESP_OK && s_stored_config.network_count > 0) {
        ESP_LOGI(TAG, "Found %d stored networks", s_stored_config.network_count);
        
        // Set WiFi mode
        ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
        
        // Start WiFi
        ESP_ERROR_CHECK(esp_wifi_start());
        
        // Create background task for connection management
        // Pin to Core 0 (PRO CPU) with low priority to avoid interfering with audio on Core 1
        BaseType_t task_ret = xTaskCreatePinnedToCore(wifi_manager_background_task, 
                                          "wifi_mgr", 
                                          4096, 
                                          NULL, 
                                          2,  // Lower priority (was 5)
                                          &s_wifi_task_handle,
                                          0);  // Pin to Core 0 (PRO CPU)
        
        if (task_ret != pdPASS) {
            ESP_LOGE(TAG, "Failed to create WiFi background task");
            esp_wifi_stop();
            esp_wifi_deinit();
            esp_netif_destroy_default_wifi(s_sta_netif);
            vEventGroupDelete(s_wifi_event_group);
            vSemaphoreDelete(s_wifi_mutex);
            return ESP_FAIL;
        }
        
        ESP_LOGI(TAG, "WiFi manager initialized successfully (async mode)");
        ESP_LOGI(TAG, "WiFi connection attempts will continue in background");
        return ESP_OK;
        
    } else {
        ESP_LOGW(TAG, "No WiFi credentials found in NVS");
        ESP_LOGI(TAG, "To configure WiFi, use wifi_manager_add_network()");
        s_wifi_state = WIFIMAN_STATE_DISCONNECTED;
        
        // Still initialize WiFi but don't start it
        ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
        return ESP_ERR_NOT_FOUND;
    }
}

// Make the original init function use async version
esp_err_t wifi_manager_init(void)
{
    return wifi_manager_init_async();
}

esp_err_t wifi_manager_deinit(void)
{
    ESP_LOGI(TAG, "Deinitializing WiFi manager");
    
    // Signal background task to stop
    if (s_wifi_task_handle) {
        xEventGroupSetBits(s_wifi_event_group, WIFI_STOP_BIT);
        // Wait for task to finish
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    
    // Stop WiFi
    esp_wifi_stop();
    
    // Disconnect if connected
    if (s_wifi_state == WIFIMAN_STATE_CONNECTED) {
        esp_wifi_disconnect();
    }
    
    // Free scan results if allocated
    if (s_scan_results) {
        free(s_scan_results);
        s_scan_results = NULL;
    }
    
    // Deinitialize WiFi
    esp_wifi_deinit();
    
    // Destroy default netif
    if (s_sta_netif) {
        esp_netif_destroy_default_wifi(s_sta_netif);
        s_sta_netif = NULL;
    }
    
    // Delete event group
    if (s_wifi_event_group) {
        vEventGroupDelete(s_wifi_event_group);
        s_wifi_event_group = NULL;
    }
    
    // Delete mutex
    if (s_wifi_mutex) {
        vSemaphoreDelete(s_wifi_mutex);
        s_wifi_mutex = NULL;
    }
    
    s_wifi_state = WIFIMAN_STATE_DISCONNECTED;
    
    return ESP_OK;
}

wifiman_state_t wifi_manager_get_state(void)
{
    wifiman_state_t state;
    xSemaphoreTake(s_wifi_mutex, portMAX_DELAY);
    state = s_wifi_state;
    xSemaphoreGive(s_wifi_mutex);
    return state;
}

bool wifi_manager_is_connected(void)
{
    wifiman_state_t state;
    xSemaphoreTake(s_wifi_mutex, portMAX_DELAY);
    state = s_wifi_state;
    xSemaphoreGive(s_wifi_mutex);
    return (state == WIFIMAN_STATE_CONNECTED);
}

esp_err_t wifi_manager_get_ip_string(char *ip_str, size_t len)
{
    if (ip_str == NULL || len < 16) {
        return ESP_ERR_INVALID_ARG;
    }
    
    if (!wifi_manager_is_connected() || s_sta_netif == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    
    esp_netif_ip_info_t ip_info;
    esp_err_t ret = esp_netif_get_ip_info(s_sta_netif, &ip_info);
    if (ret != ESP_OK) {
        return ret;
    }
    
    snprintf(ip_str, len, IPSTR, IP2STR(&ip_info.ip));
    return ESP_OK;
}

esp_err_t wifi_manager_get_connected_ssid(char *ssid, size_t len)
{
    if (ssid == NULL || len == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    if (!wifi_manager_is_connected()) {
        return ESP_ERR_INVALID_STATE;
    }

    xSemaphoreTake(s_wifi_mutex, portMAX_DELAY);
    strncpy(ssid, s_connected_ssid, len - 1);
    ssid[len - 1] = '\0';
    xSemaphoreGive(s_wifi_mutex);
    
    return ESP_OK;
}

// Include remaining functions from original wifi_manager.c that don't need changes
esp_err_t wifi_manager_read_credentials(wifiman_config_t *config)
{
    if (config == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    nvs_handle_t nvs_handle;
    esp_err_t ret;

    // Initialize configuration
    memset(config, 0, sizeof(wifiman_config_t));

    // Open NVS
    ret = nvs_open(WIFI_NVS_NAMESPACE, NVS_READONLY, &nvs_handle);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "Failed to open NVS namespace: %s", esp_err_to_name(ret));
        return ret;
    }

    // Read network count
    uint8_t network_count = 0;
    ret = nvs_get_u8(nvs_handle, WIFI_NVS_COUNT_KEY, &network_count);
    if (ret != ESP_OK || network_count == 0) {
        ESP_LOGW(TAG, "No networks stored in NVS");
        nvs_close(nvs_handle);
        return ESP_ERR_NOT_FOUND;
    }

    config->network_count = (network_count > WIFI_MAX_NETWORKS) ? WIFI_MAX_NETWORKS : network_count;
    ESP_LOGI(TAG, "Found %d networks in NVS", config->network_count);

    // Read each network
    for (int i = 0; i < config->network_count; i++) {
        char key[32];
        size_t len;

        // Read SSID
        snprintf(key, sizeof(key), "%s%d", WIFI_NVS_SSID_PREFIX, i);
        len = sizeof(config->networks[i].ssid);
        ret = nvs_get_str(nvs_handle, key, config->networks[i].ssid, &len);
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "Failed to read SSID %d: %s", i, esp_err_to_name(ret));
            continue;
        }

        // Read password
        snprintf(key, sizeof(key), "%s%d", WIFI_NVS_PASSWORD_PREFIX, i);
        len = sizeof(config->networks[i].password);
        ret = nvs_get_str(nvs_handle, key, config->networks[i].password, &len);
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "Failed to read password %d: %s", i, esp_err_to_name(ret));
            continue;
        }

        // Read auth failure counter
        snprintf(key, sizeof(key), "%s%d", WIFI_NVS_AUTH_FAIL_PREFIX, i);
        uint8_t auth_fail_count = 0;
        ret = nvs_get_u8(nvs_handle, key, &auth_fail_count);
        config->networks[i].auth_fail_count = (ret == ESP_OK) ? auth_fail_count : 0;

        ESP_LOGI(TAG, "Network %d: SSID=%s, Auth fail count=%d", 
                 i, config->networks[i].ssid, config->networks[i].auth_fail_count);
    }

    nvs_close(nvs_handle);
    return ESP_OK;
}

esp_err_t wifi_manager_add_network(const char *ssid, const char *password)
{
    if (ssid == NULL || password == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    nvs_handle_t nvs_handle;
    esp_err_t ret;

    // Open NVS
    ret = nvs_open(WIFI_NVS_NAMESPACE, NVS_READWRITE, &nvs_handle);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to open NVS namespace: %s", esp_err_to_name(ret));
        return ret;
    }

    // Read current network count
    uint8_t network_count = 0;
    nvs_get_u8(nvs_handle, WIFI_NVS_COUNT_KEY, &network_count);

    // Check if network already exists
    for (int i = 0; i < network_count && i < WIFI_MAX_NETWORKS; i++) {
        char key[32];
        char existing_ssid[33];
        size_t len = sizeof(existing_ssid);
        
        snprintf(key, sizeof(key), "%s%d", WIFI_NVS_SSID_PREFIX, i);
        if (nvs_get_str(nvs_handle, key, existing_ssid, &len) == ESP_OK) {
            if (strcmp(existing_ssid, ssid) == 0) {
                ESP_LOGI(TAG, "Network %s already exists, updating password", ssid);
                
                // Update password
                snprintf(key, sizeof(key), "%s%d", WIFI_NVS_PASSWORD_PREFIX, i);
                ret = nvs_set_str(nvs_handle, key, password);
                
                // Clear auth failure flag
                snprintf(key, sizeof(key), "%s%d", WIFI_NVS_AUTH_FAIL_PREFIX, i);
                nvs_erase_key(nvs_handle, key);
                
                nvs_commit(nvs_handle);
                nvs_close(nvs_handle);
                return ret;
            }
        }
    }

    // Add new network if there's space
    if (network_count >= WIFI_MAX_NETWORKS) {
        ESP_LOGE(TAG, "Maximum number of networks reached");
        nvs_close(nvs_handle);
        return ESP_ERR_NO_MEM;
    }

    char key[32];
    
    // Save SSID
    snprintf(key, sizeof(key), "%s%d", WIFI_NVS_SSID_PREFIX, network_count);
    ret = nvs_set_str(nvs_handle, key, ssid);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to save SSID: %s", esp_err_to_name(ret));
        nvs_close(nvs_handle);
        return ret;
    }

    // Save password
    snprintf(key, sizeof(key), "%s%d", WIFI_NVS_PASSWORD_PREFIX, network_count);
    ret = nvs_set_str(nvs_handle, key, password);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to save password: %s", esp_err_to_name(ret));
        nvs_close(nvs_handle);
        return ret;
    }

    // Update network count
    network_count++;
    ret = nvs_set_u8(nvs_handle, WIFI_NVS_COUNT_KEY, network_count);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to update network count: %s", esp_err_to_name(ret));
        nvs_close(nvs_handle);
        return ret;
    }

    // Commit changes
    ret = nvs_commit(nvs_handle);
    nvs_close(nvs_handle);

    ESP_LOGI(TAG, "Added network %s (total: %d)", ssid, network_count);
    return ret;
}

esp_err_t wifi_manager_remove_network(const char *ssid)
{
    if (ssid == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    nvs_handle_t nvs_handle;
    esp_err_t ret;

    // Open NVS
    ret = nvs_open(WIFI_NVS_NAMESPACE, NVS_READWRITE, &nvs_handle);
    if (ret != ESP_OK) {
        return ret;
    }

    // Read current network count
    uint8_t network_count = 0;
    nvs_get_u8(nvs_handle, WIFI_NVS_COUNT_KEY, &network_count);

    // Find and remove the network
    int found_index = -1;
    for (int i = 0; i < network_count && i < WIFI_MAX_NETWORKS; i++) {
        char key[32];
        char existing_ssid[33];
        size_t len = sizeof(existing_ssid);
        
        snprintf(key, sizeof(key), "%s%d", WIFI_NVS_SSID_PREFIX, i);
        if (nvs_get_str(nvs_handle, key, existing_ssid, &len) == ESP_OK) {
            if (strcmp(existing_ssid, ssid) == 0) {
                found_index = i;
                break;
            }
        }
    }

    if (found_index < 0) {
        ESP_LOGW(TAG, "Network %s not found", ssid);
        nvs_close(nvs_handle);
        return ESP_ERR_NOT_FOUND;
    }

    // Shift remaining networks down
    for (int i = found_index; i < network_count - 1 && i < WIFI_MAX_NETWORKS - 1; i++) {
        char key_from[32], key_to[32];
        char buffer[65];
        size_t len;

        // Move SSID
        snprintf(key_from, sizeof(key_from), "%s%d", WIFI_NVS_SSID_PREFIX, i + 1);
        snprintf(key_to, sizeof(key_to), "%s%d", WIFI_NVS_SSID_PREFIX, i);
        len = sizeof(buffer);
        if (nvs_get_str(nvs_handle, key_from, buffer, &len) == ESP_OK) {
            nvs_set_str(nvs_handle, key_to, buffer);
        }

        // Move password
        snprintf(key_from, sizeof(key_from), "%s%d", WIFI_NVS_PASSWORD_PREFIX, i + 1);
        snprintf(key_to, sizeof(key_to), "%s%d", WIFI_NVS_PASSWORD_PREFIX, i);
        len = sizeof(buffer);
        if (nvs_get_str(nvs_handle, key_from, buffer, &len) == ESP_OK) {
            nvs_set_str(nvs_handle, key_to, buffer);
        }

        // Move auth failure flag
        snprintf(key_from, sizeof(key_from), "%s%d", WIFI_NVS_AUTH_FAIL_PREFIX, i + 1);
        snprintf(key_to, sizeof(key_to), "%s%d", WIFI_NVS_AUTH_FAIL_PREFIX, i);
        uint8_t fail_val;
        if (nvs_get_u8(nvs_handle, key_from, &fail_val) == ESP_OK) {
            nvs_set_u8(nvs_handle, key_to, fail_val);
        }
    }

    // Erase last entries
    char key[32];
    snprintf(key, sizeof(key), "%s%d", WIFI_NVS_SSID_PREFIX, network_count - 1);
    nvs_erase_key(nvs_handle, key);
    snprintf(key, sizeof(key), "%s%d", WIFI_NVS_PASSWORD_PREFIX, network_count - 1);
    nvs_erase_key(nvs_handle, key);
    snprintf(key, sizeof(key), "%s%d", WIFI_NVS_AUTH_FAIL_PREFIX, network_count - 1);
    nvs_erase_key(nvs_handle, key);

    // Update network count
    network_count--;
    nvs_set_u8(nvs_handle, WIFI_NVS_COUNT_KEY, network_count);

    nvs_commit(nvs_handle);
    nvs_close(nvs_handle);

    ESP_LOGI(TAG, "Removed network %s (remaining: %d)", ssid, network_count);
    return ESP_OK;
}

esp_err_t wifi_manager_clear_auth_failure(const char *ssid)
{
    if (ssid == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    nvs_handle_t nvs_handle;
    esp_err_t ret = nvs_open(WIFI_NVS_NAMESPACE, NVS_READWRITE, &nvs_handle);
    if (ret != ESP_OK) {
        return ret;
    }

    uint8_t network_count = 0;
    nvs_get_u8(nvs_handle, WIFI_NVS_COUNT_KEY, &network_count);

    for (int i = 0; i < network_count && i < WIFI_MAX_NETWORKS; i++) {
        char key[32];
        char existing_ssid[33];
        size_t len = sizeof(existing_ssid);
        
        snprintf(key, sizeof(key), "%s%d", WIFI_NVS_SSID_PREFIX, i);
        if (nvs_get_str(nvs_handle, key, existing_ssid, &len) == ESP_OK) {
            if (strcmp(existing_ssid, ssid) == 0) {
                snprintf(key, sizeof(key), "%s%d", WIFI_NVS_AUTH_FAIL_PREFIX, i);
                nvs_erase_key(nvs_handle, key);
                nvs_commit(nvs_handle);
                nvs_close(nvs_handle);
                ESP_LOGI(TAG, "Cleared auth failure for %s", ssid);
                return ESP_OK;
            }
        }
    }

    nvs_close(nvs_handle);
    return ESP_ERR_NOT_FOUND;
}

esp_err_t wifi_manager_clear_all_auth_failures(void)
{
    nvs_handle_t nvs_handle;
    esp_err_t ret = nvs_open(WIFI_NVS_NAMESPACE, NVS_READWRITE, &nvs_handle);
    if (ret != ESP_OK) {
        return ret;
    }

    uint8_t network_count = 0;
    nvs_get_u8(nvs_handle, WIFI_NVS_COUNT_KEY, &network_count);

    for (int i = 0; i < network_count && i < WIFI_MAX_NETWORKS; i++) {
        char key[32];
        snprintf(key, sizeof(key), "%s%d", WIFI_NVS_AUTH_FAIL_PREFIX, i);
        nvs_erase_key(nvs_handle, key);
    }

    nvs_commit(nvs_handle);
    nvs_close(nvs_handle);
    
    ESP_LOGI(TAG, "Cleared all auth failures");
    return ESP_OK;
}

esp_err_t wifi_manager_clear_all_networks(void)
{
    nvs_handle_t nvs_handle;
    esp_err_t ret = nvs_open(WIFI_NVS_NAMESPACE, NVS_READWRITE, &nvs_handle);
    if (ret != ESP_OK) {
        return ret;
    }

    // Erase all data in namespace
    nvs_erase_all(nvs_handle);
    nvs_commit(nvs_handle);
    nvs_close(nvs_handle);
    
    ESP_LOGI(TAG, "Cleared all networks");
    return ESP_OK;
}

// Legacy functions for backward compatibility
esp_err_t wifi_manager_save_credentials(const char *ssid, const char *password)
{
    return wifi_manager_add_network(ssid, password);
}

esp_err_t wifi_manager_clear_credentials(void)
{
    return wifi_manager_clear_all_networks();
}

esp_err_t wifi_manager_reconnect(void)
{
    ESP_LOGI(TAG, "Triggering WiFi reconnection");
    
    // Reset retry counter and auth failure
    xSemaphoreTake(s_wifi_mutex, portMAX_DELAY);
    s_retry_num = 0;
    s_auth_failure = false;
    s_current_network_index = -1;
    s_reconnect_interval_ms = 10000;  // Reset reconnect interval
    
    // Disconnect if connected
    if (s_wifi_state == WIFIMAN_STATE_CONNECTED) {
        s_wifi_state = WIFIMAN_STATE_DISCONNECTED;
        xSemaphoreGive(s_wifi_mutex);
        esp_wifi_disconnect();
    } else {
        s_wifi_state = WIFIMAN_STATE_DISCONNECTED;
        xSemaphoreGive(s_wifi_mutex);
    }
    
    // The background task will handle reconnection
    return ESP_OK;
}

esp_err_t wifi_manager_get_stored_networks(wifiman_network_entry_t *networks, size_t max_networks, size_t *count)
{
    if (networks == NULL || count == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    
    wifiman_config_t config;
    esp_err_t ret = wifi_manager_read_credentials(&config);
    if (ret != ESP_OK) {
        *count = 0;
        return ret;
    }
    
    size_t copy_count = (config.network_count < max_networks) ? config.network_count : max_networks;
    memcpy(networks, config.networks, copy_count * sizeof(wifiman_network_entry_t));
    *count = copy_count;
    
    return ESP_OK;
}
