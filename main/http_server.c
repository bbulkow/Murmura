#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_http_server.h"
#include "cJSON.h"
#include "http_server.h"
#include "music_files.h"
#include "play_sdcard.h"
#include "wifi_manager.h"
#include "esp_wifi.h"

static const char *TAG = "HTTP_SERVER";

// Global variables
static httpd_handle_t server = NULL;
static loop_manager_t *g_loop_manager = NULL;

// Forward declarations
static esp_err_t files_get_handler(httpd_req_t *req);
static esp_err_t loops_get_handler(httpd_req_t *req);
static esp_err_t loop_file_handler(httpd_req_t *req);
static esp_err_t loop_start_handler(httpd_req_t *req);
static esp_err_t loop_stop_handler(httpd_req_t *req);
static esp_err_t loop_volume_handler(httpd_req_t *req);
static esp_err_t global_volume_handler(httpd_req_t *req);
static esp_err_t root_get_handler(httpd_req_t *req);
// WiFi management handlers
static esp_err_t wifi_status_handler(httpd_req_t *req);
static esp_err_t wifi_networks_handler(httpd_req_t *req);
static esp_err_t wifi_add_network_handler(httpd_req_t *req);
static esp_err_t wifi_remove_network_handler(httpd_req_t *req);

/**
 * @brief Send JSON response
 */
static esp_err_t send_json_response(httpd_req_t *req, cJSON *json) {
    char *json_str = cJSON_Print(json);
    if (json_str == NULL) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Failed to generate JSON");
        return ESP_FAIL;
    }
    
    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    esp_err_t ret = httpd_resp_send(req, json_str, strlen(json_str));
    
    free(json_str);
    return ret;
}

/**
 * @brief Parse JSON from request body
 */
static cJSON* parse_json_request(httpd_req_t *req) {
    char *buf = heap_caps_malloc(req->content_len + 1, MALLOC_CAP_SPIRAM);
    if (!buf) {
        ESP_LOGE(TAG, "Failed to allocate memory for request buffer");
        return NULL;
    }
    
    int ret = httpd_req_recv(req, buf, req->content_len);
    if (ret <= 0) {
        ESP_LOGE(TAG, "Failed to receive request data");
        free(buf);
        return NULL;
    }
    
    buf[req->content_len] = '\0';
    cJSON *json = cJSON_Parse(buf);
    free(buf);
    
    if (!json) {
        ESP_LOGE(TAG, "Failed to parse JSON: %s", cJSON_GetErrorPtr());
    }
    
    return json;
}

/**
 * @brief GET /api/files - List all audio files in root directory
 */
static esp_err_t files_get_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "GET /api/files");
    
    cJSON *response = cJSON_CreateObject();
    cJSON *files_array = cJSON_CreateArray();
    
    // Get list of music files
    char **music_files = NULL;
    esp_err_t ret = music_filenames_get(&music_files);
    
    if (ret == ESP_OK && music_files != NULL) {
        for (int i = 0; music_files[i] != NULL; i++) {
            cJSON *file_obj = cJSON_CreateObject();
            cJSON_AddNumberToObject(file_obj, "index", i);
            cJSON_AddStringToObject(file_obj, "name", music_files[i]);
            
            // Determine file type
            enum FILETYPE_ENUM filetype;
            music_determine_filetype(music_files[i], &filetype);
            const char *type_str = (filetype == FILETYPE_MP3) ? "mp3" : 
                                  (filetype == FILETYPE_WAV) ? "wav" : "unknown";
            cJSON_AddStringToObject(file_obj, "type", type_str);
            
            // Add full path
            char full_path[256];
            snprintf(full_path, sizeof(full_path), "/sdcard/%s", music_files[i]);
            cJSON_AddStringToObject(file_obj, "path", full_path);
            
            cJSON_AddItemToArray(files_array, file_obj);
        }
        
        // Free the music files array
        for (int i = 0; music_files[i] != NULL; i++) {
            free(music_files[i]);
        }
        free(music_files);
    }
    
    cJSON_AddItemToObject(response, "files", files_array);
    cJSON_AddNumberToObject(response, "count", cJSON_GetArraySize(files_array));
    
    esp_err_t send_ret = send_json_response(req, response);
    cJSON_Delete(response);
    
    return send_ret;
}

/**
 * @brief GET /api/loops - List currently playing loops
 */
static esp_err_t loops_get_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "GET /api/loops");
    
    cJSON *response = cJSON_CreateObject();
    cJSON *loops_array = cJSON_CreateArray();
    
    if (g_loop_manager) {
        // Always return all tracks with their complete state
        for (int i = 0; i < MAX_TRACKS; i++) {
            cJSON *loop_obj = cJSON_CreateObject();
            cJSON_AddNumberToObject(loop_obj, "track", i);
            
            // Return file path or empty string if no file set
            const char *file_path = g_loop_manager->loops[i].file_path;
            cJSON_AddStringToObject(loop_obj, "file", (file_path[0] != '\0') ? file_path : "");
            
            cJSON_AddNumberToObject(loop_obj, "volume", g_loop_manager->loops[i].volume_percent);
            cJSON_AddBoolToObject(loop_obj, "playing", g_loop_manager->loops[i].is_playing);
            
            cJSON_AddItemToArray(loops_array, loop_obj);
            
            ESP_LOGI(TAG, "Track %d: playing=%d, file=%s, volume=%d%%", 
                     i, g_loop_manager->loops[i].is_playing, 
                     (file_path[0] != '\0') ? file_path : "(none)",
                     g_loop_manager->loops[i].volume_percent);
        }
    }
    
    // Count how many tracks are actually playing
    int active_count = 0;
    if (g_loop_manager) {
        for (int i = 0; i < MAX_TRACKS; i++) {
            if (g_loop_manager->loops[i].is_playing) {
                active_count++;
            }
        }
    }
    
    cJSON_AddItemToObject(response, "loops", loops_array);
    cJSON_AddNumberToObject(response, "active_count", active_count);
    cJSON_AddNumberToObject(response, "max_tracks", MAX_TRACKS);
    cJSON_AddNumberToObject(response, "global_volume", g_loop_manager ? g_loop_manager->global_volume_percent : 75);
    
    esp_err_t ret = send_json_response(req, response);
    cJSON_Delete(response);
    
    return ret;
}

/**
 * @brief POST /api/loop/file - Set the file for a specific track
 * Body: { "track": 0, "file_index": 0 } or { "track": 0, "file_path": "/sdcard/track1.wav" }
 */
static esp_err_t loop_file_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "POST /api/loop/file");
    
    if (req->content_len == 0) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Empty request body");
        return ESP_FAIL;
    }
    
    cJSON *request = parse_json_request(req);
    if (!request) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Invalid JSON");
        return ESP_FAIL;
    }
    
    cJSON *response = cJSON_CreateObject();
    
    // Get track number
    cJSON *track_json = cJSON_GetObjectItem(request, "track");
    if (!cJSON_IsNumber(track_json)) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Missing or invalid track number");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    int track = track_json->valueint;
    if (track < 0 || track >= MAX_TRACKS) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Track index out of range");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    // Get file path or index
    char file_path[256] = {0};
    cJSON *file_path_json = cJSON_GetObjectItem(request, "file_path");
    cJSON *file_index_json = cJSON_GetObjectItem(request, "file_index");
    
    if (cJSON_IsString(file_path_json)) {
        strncpy(file_path, file_path_json->valuestring, sizeof(file_path) - 1);
    } else if (cJSON_IsNumber(file_index_json)) {
        // Get file by index
        char **music_files = NULL;
        esp_err_t ret = music_filenames_get(&music_files);
        if (ret == ESP_OK && music_files != NULL) {
            int index = file_index_json->valueint;
            int count = 0;
            while (music_files[count] != NULL) count++;
            
            if (index >= 0 && index < count) {
                snprintf(file_path, sizeof(file_path), "/sdcard/%s", music_files[index]);
            }
            
            // Free the music files array
            for (int i = 0; music_files[i] != NULL; i++) {
                free(music_files[i]);
            }
            free(music_files);
        }
    }
    
    if (strlen(file_path) == 0) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "No valid file specified");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    // Send message to audio control task to start the track
    if (g_loop_manager && g_loop_manager->audio_control_queue) {
        audio_control_msg_t control_msg;
        control_msg.type = AUDIO_ACTION_START_TRACK;
        control_msg.data.start_track.track_index = track;
        strncpy(control_msg.data.start_track.file_path, file_path, sizeof(control_msg.data.start_track.file_path) - 1);
        control_msg.data.start_track.file_path[sizeof(control_msg.data.start_track.file_path) - 1] = '\0';
        
        // Send message with timeout
        if (xQueueSend(g_loop_manager->audio_control_queue, &control_msg, pdMS_TO_TICKS(100)) == pdPASS) {
            // Note: Loop state is now managed by audio control task
            // We don't update it here anymore
            
            cJSON_AddBoolToObject(response, "success", true);
            cJSON_AddNumberToObject(response, "track", track);
            cJSON_AddStringToObject(response, "file", file_path);
            cJSON_AddStringToObject(response, "message", "File set and loop started");
        } else {
            cJSON_AddBoolToObject(response, "success", false);
            cJSON_AddStringToObject(response, "error", "Failed to send command to audio task");
        }
    } else {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Audio system not initialized");
    }
    
    esp_err_t ret = send_json_response(req, response);
    cJSON_Delete(response);
    cJSON_Delete(request);
    
    return ret;
}

/**
 * @brief POST /api/loop/start - Start a loop on a specific track (simplified version)
 * Body: { "track": 0 }
 */
static esp_err_t loop_start_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "POST /api/loop/start");
    
    if (req->content_len == 0) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Empty request body");
        return ESP_FAIL;
    }
    
    cJSON *request = parse_json_request(req);
    if (!request) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Invalid JSON");
        return ESP_FAIL;
    }
    
    cJSON *response = cJSON_CreateObject();
    
    // Get track number
    cJSON *track_json = cJSON_GetObjectItem(request, "track");
    if (!cJSON_IsNumber(track_json)) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Missing or invalid track number");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    int track = track_json->valueint;
    if (track < 0 || track >= MAX_TRACKS) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Track index out of range");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    // Check if there's a file already configured for this track
    if (g_loop_manager && strlen(g_loop_manager->loops[track].file_path) == 0) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "No file configured for this track. Use /api/loop/file first.");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    // Just restart the track with its current file
    if (g_loop_manager && g_loop_manager->audio_control_queue) {
        audio_control_msg_t control_msg;
        control_msg.type = AUDIO_ACTION_START_TRACK;
        control_msg.data.start_track.track_index = track;
        strncpy(control_msg.data.start_track.file_path, 
                g_loop_manager->loops[track].file_path, 
                sizeof(control_msg.data.start_track.file_path) - 1);
        control_msg.data.start_track.file_path[sizeof(control_msg.data.start_track.file_path) - 1] = '\0';
        
        // Send message with timeout
        if (xQueueSend(g_loop_manager->audio_control_queue, &control_msg, pdMS_TO_TICKS(100)) == pdPASS) {
            cJSON_AddBoolToObject(response, "success", true);
            cJSON_AddNumberToObject(response, "track", track);
            cJSON_AddStringToObject(response, "file", g_loop_manager->loops[track].file_path);
            cJSON_AddStringToObject(response, "message", "Loop started");
        } else {
            cJSON_AddBoolToObject(response, "success", false);
            cJSON_AddStringToObject(response, "error", "Failed to send command to audio task");
        }
    } else {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Audio system not initialized");
    }
    
    esp_err_t ret = send_json_response(req, response);
    cJSON_Delete(response);
    cJSON_Delete(request);
    
    return ret;
}

/**
 * @brief POST /api/loop/stop - Stop a loop on a specific track
 * Body: { "track": 0 }
 */
static esp_err_t loop_stop_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "POST /api/loop/stop");
    
    if (req->content_len == 0) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Empty request body");
        return ESP_FAIL;
    }
    
    cJSON *request = parse_json_request(req);
    if (!request) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Invalid JSON");
        return ESP_FAIL;
    }
    
    cJSON *response = cJSON_CreateObject();
    
    // Get track number
    cJSON *track_json = cJSON_GetObjectItem(request, "track");
    if (!cJSON_IsNumber(track_json)) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Missing or invalid track number");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    int track = track_json->valueint;
    if (track < 0 || track >= MAX_TRACKS) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Track index out of range");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    // Send message to audio control task to stop the track
    if (g_loop_manager && g_loop_manager->audio_control_queue) {
        audio_control_msg_t control_msg;
        control_msg.type = AUDIO_ACTION_STOP_TRACK;
        control_msg.data.stop_track.track_index = track;
        
        // Send message with timeout
        if (xQueueSend(g_loop_manager->audio_control_queue, &control_msg, pdMS_TO_TICKS(100)) == pdPASS) {
            // Note: Loop state is now managed by audio control task
            // We don't update it here anymore
            
            cJSON_AddBoolToObject(response, "success", true);
            cJSON_AddNumberToObject(response, "track", track);
            cJSON_AddStringToObject(response, "message", "Loop stop command sent");
        } else {
            cJSON_AddBoolToObject(response, "success", false);
            cJSON_AddStringToObject(response, "error", "Failed to send command to audio task");
        }
    } else {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Audio system not initialized");
    }
    
    esp_err_t ret = send_json_response(req, response);
    cJSON_Delete(response);
    cJSON_Delete(request);
    
    return ret;
}

/**
 * @brief POST /api/loop/volume - Set volume for a specific loop
 * Body: { "track": 0, "volume": 75 }  // 0-100%
 */
static esp_err_t loop_volume_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "POST /api/loop/volume");
    
    if (req->content_len == 0) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Empty request body");
        return ESP_FAIL;
    }
    
    cJSON *request = parse_json_request(req);
    if (!request) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Invalid JSON");
        return ESP_FAIL;
    }
    
    cJSON *response = cJSON_CreateObject();
    
    // Get track number
    cJSON *track_json = cJSON_GetObjectItem(request, "track");
    if (!cJSON_IsNumber(track_json)) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Missing or invalid track number");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    int track = track_json->valueint;
    if (track < 0 || track >= MAX_TRACKS) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Track index out of range");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    // Get volume value
    cJSON *volume_json = cJSON_GetObjectItem(request, "volume");
    if (!cJSON_IsNumber(volume_json)) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Missing or invalid volume value");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    int volume = volume_json->valueint;
    
    // Clamp volume to 0-100 range
    if (volume < 0) volume = 0;
    if (volume > 100) volume = 100;
    
    // Send message to audio control task to set the volume
    if (g_loop_manager && g_loop_manager->audio_control_queue) {
        audio_control_msg_t control_msg;
        control_msg.type = AUDIO_ACTION_SET_VOLUME;
        control_msg.data.set_volume.track_index = track;
        control_msg.data.set_volume.volume_percent = volume;
        
        // Send message with timeout
        if (xQueueSend(g_loop_manager->audio_control_queue, &control_msg, pdMS_TO_TICKS(100)) == pdPASS) {
            // Note: Loop state is now managed by audio control task
            // We don't update it here anymore
            
            cJSON_AddBoolToObject(response, "success", true);
            cJSON_AddNumberToObject(response, "track", track);
            cJSON_AddNumberToObject(response, "volume", volume);
            cJSON_AddStringToObject(response, "message", "Volume adjustment command sent");
        } else {
            cJSON_AddBoolToObject(response, "success", false);
            cJSON_AddStringToObject(response, "error", "Failed to send command to audio task");
        }
    } else {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Audio system not initialized");
    }
    
    esp_err_t ret = send_json_response(req, response);
    cJSON_Delete(response);
    cJSON_Delete(request);
    
    return ret;
}

/**
 * @brief POST /api/global/volume - Set global volume
 * Body: { "volume": 75 }  // 0-100%
 */
static esp_err_t global_volume_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "POST /api/global/volume");
    
    if (req->content_len == 0) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Empty request body");
        return ESP_FAIL;
    }
    
    cJSON *request = parse_json_request(req);
    if (!request) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Invalid JSON");
        return ESP_FAIL;
    }
    
    cJSON *response = cJSON_CreateObject();
    
    // Get volume value
    cJSON *volume_json = cJSON_GetObjectItem(request, "volume");
    if (!cJSON_IsNumber(volume_json)) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Missing or invalid volume value");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    int volume = volume_json->valueint;
    
    // Clamp volume to 0-100 range
    if (volume < 0) volume = 0;
    if (volume > 100) volume = 100;
    
    // Send message to audio control task to set global volume
    if (g_loop_manager && g_loop_manager->audio_control_queue) {
        audio_control_msg_t control_msg;
        control_msg.type = AUDIO_ACTION_SET_GLOBAL_VOLUME;
        control_msg.data.set_global_volume.volume_percent = volume;
        
        // Send message with timeout
        if (xQueueSend(g_loop_manager->audio_control_queue, &control_msg, pdMS_TO_TICKS(100)) == pdPASS) {
            cJSON_AddBoolToObject(response, "success", true);
            cJSON_AddNumberToObject(response, "volume", volume);
            cJSON_AddStringToObject(response, "message", "Global volume adjustment command sent");
        } else {
            cJSON_AddBoolToObject(response, "success", false);
            cJSON_AddStringToObject(response, "error", "Failed to send command to audio task");
        }
    } else {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Audio system not initialized");
    }
    
    esp_err_t ret = send_json_response(req, response);
    cJSON_Delete(response);
    cJSON_Delete(request);
    
    return ret;
}

/**
 * @brief GET /api/wifi/status - Get current WiFi connection status
 */
static esp_err_t wifi_status_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "GET /api/wifi/status");
    
    cJSON *response = cJSON_CreateObject();
    
    // Get WiFi connection state
    bool is_connected = wifi_manager_is_connected();
    cJSON_AddBoolToObject(response, "connected", is_connected);
    
    if (is_connected) {
        // Get connected SSID
        char ssid[33] = {0};
        wifi_manager_get_connected_ssid(ssid, sizeof(ssid));
        cJSON_AddStringToObject(response, "ssid", ssid);
        
        // Get IP address
        char ip_str[16] = {0};
        wifi_manager_get_ip_string(ip_str, sizeof(ip_str));
        cJSON_AddStringToObject(response, "ip_address", ip_str);
        
        // Get signal strength (RSSI)
        wifi_ap_record_t ap_info;
        if (esp_wifi_sta_get_ap_info(&ap_info) == ESP_OK) {
            cJSON_AddNumberToObject(response, "rssi", ap_info.rssi);
            
            // Convert RSSI to signal strength percentage (rough approximation)
            int signal_percent = 0;
            if (ap_info.rssi >= -50) {
                signal_percent = 100;
            } else if (ap_info.rssi >= -60) {
                signal_percent = 90;
            } else if (ap_info.rssi >= -67) {
                signal_percent = 75;
            } else if (ap_info.rssi >= -70) {
                signal_percent = 60;
            } else if (ap_info.rssi >= -80) {
                signal_percent = 40;
            } else if (ap_info.rssi >= -90) {
                signal_percent = 20;
            } else {
                signal_percent = 10;
            }
            cJSON_AddNumberToObject(response, "signal_strength", signal_percent);
        }
    } else {
        // Get connection state
        wifiman_state_t state = wifi_manager_get_state();
        const char *state_str = "disconnected";
        switch (state) {
            case WIFIMAN_STATE_SCANNING:
                state_str = "scanning";
                break;
            case WIFIMAN_STATE_CONNECTING:
                state_str = "connecting";
                break;
            case WIFIMAN_STATE_CONNECTION_FAILED:
                state_str = "connection_failed";
                break;
            default:
                state_str = "disconnected";
                break;
        }
        cJSON_AddStringToObject(response, "state", state_str);
    }
    
    esp_err_t ret = send_json_response(req, response);
    cJSON_Delete(response);
    
    return ret;
}

/**
 * @brief GET /api/wifi/networks - Get list of configured networks
 */
static esp_err_t wifi_networks_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "GET /api/wifi/networks");
    
    cJSON *response = cJSON_CreateObject();
    cJSON *networks_array = cJSON_CreateArray();
    
    // Get stored networks
    wifiman_network_entry_t networks[WIFI_MAX_NETWORKS];
    size_t count = 0;
    esp_err_t ret = wifi_manager_get_stored_networks(networks, WIFI_MAX_NETWORKS, &count);
    
    if (ret == ESP_OK) {
        for (size_t i = 0; i < count; i++) {
            cJSON *network_obj = cJSON_CreateObject();
            cJSON_AddNumberToObject(network_obj, "index", i);
            cJSON_AddStringToObject(network_obj, "ssid", networks[i].ssid);
            // Don't expose the password in the response for security
            cJSON_AddBoolToObject(network_obj, "has_password", strlen(networks[i].password) > 0);
            cJSON_AddBoolToObject(network_obj, "auth_failed", networks[i].auth_failed);
            cJSON_AddBoolToObject(network_obj, "available", networks[i].available);
            cJSON_AddNumberToObject(network_obj, "rssi", networks[i].rssi);
            
            cJSON_AddItemToArray(networks_array, network_obj);
        }
    }
    
    cJSON_AddItemToObject(response, "networks", networks_array);
    cJSON_AddNumberToObject(response, "count", count);
    cJSON_AddNumberToObject(response, "max_networks", WIFI_MAX_NETWORKS);
    
    esp_err_t send_ret = send_json_response(req, response);
    cJSON_Delete(response);
    
    return send_ret;
}

/**
 * @brief POST /api/wifi/add - Add a new WiFi network
 * Body: { "ssid": "NetworkName", "password": "NetworkPassword" }
 */
static esp_err_t wifi_add_network_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "POST /api/wifi/add");
    
    if (req->content_len == 0) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Empty request body");
        return ESP_FAIL;
    }
    
    cJSON *request = parse_json_request(req);
    if (!request) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Invalid JSON");
        return ESP_FAIL;
    }
    
    cJSON *response = cJSON_CreateObject();
    
    // Get SSID
    cJSON *ssid_json = cJSON_GetObjectItem(request, "ssid");
    if (!cJSON_IsString(ssid_json) || strlen(ssid_json->valuestring) == 0) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Missing or invalid SSID");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    // Get password
    cJSON *password_json = cJSON_GetObjectItem(request, "password");
    if (!cJSON_IsString(password_json)) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Missing or invalid password");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    // Add the network
    esp_err_t ret = wifi_manager_add_network(ssid_json->valuestring, password_json->valuestring);
    
    if (ret == ESP_OK) {
        cJSON_AddBoolToObject(response, "success", true);
        cJSON_AddStringToObject(response, "message", "Network added successfully");
        cJSON_AddStringToObject(response, "ssid", ssid_json->valuestring);
        
        // Trigger reconnection to try the new network
        wifi_manager_reconnect();
    } else {
        cJSON_AddBoolToObject(response, "success", false);
        if (ret == ESP_ERR_NO_MEM) {
            cJSON_AddStringToObject(response, "error", "Maximum number of networks reached");
        } else {
            cJSON_AddStringToObject(response, "error", "Failed to add network");
        }
    }
    
    esp_err_t send_ret = send_json_response(req, response);
    cJSON_Delete(response);
    cJSON_Delete(request);
    
    return send_ret;
}

/**
 * @brief POST /api/wifi/remove - Remove a WiFi network
 * Body: { "ssid": "NetworkName" }
 */
static esp_err_t wifi_remove_network_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "POST /api/wifi/remove");
    
    if (req->content_len == 0) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Empty request body");
        return ESP_FAIL;
    }
    
    cJSON *request = parse_json_request(req);
    if (!request) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Invalid JSON");
        return ESP_FAIL;
    }
    
    cJSON *response = cJSON_CreateObject();
    
    // Get SSID
    cJSON *ssid_json = cJSON_GetObjectItem(request, "ssid");
    if (!cJSON_IsString(ssid_json) || strlen(ssid_json->valuestring) == 0) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Missing or invalid SSID");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    // Remove the network
    esp_err_t ret = wifi_manager_remove_network(ssid_json->valuestring);
    
    if (ret == ESP_OK) {
        cJSON_AddBoolToObject(response, "success", true);
        cJSON_AddStringToObject(response, "message", "Network removed successfully");
        cJSON_AddStringToObject(response, "ssid", ssid_json->valuestring);
    } else if (ret == ESP_ERR_NOT_FOUND) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Network not found");
    } else {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Failed to remove network");
    }
    
    esp_err_t send_ret = send_json_response(req, response);
    cJSON_Delete(response);
    cJSON_Delete(request);
    
    return send_ret;
}

/**
 * @brief GET / - Root handler with API documentation
 */
static esp_err_t root_get_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "GET /");
    
    const char *html = 
        "<!DOCTYPE html>"
        "<html>"
        "<head>"
        "<title>Audio Loop Controller API</title>"
        "<style>"
        "body { font-family: Arial, sans-serif; margin: 40px; }"
        "h1 { color: #333; }"
        "h2 { color: #666; margin-top: 30px; }"
        "pre { background: #f4f4f4; padding: 10px; border-radius: 5px; overflow-x: auto; }"
        ".endpoint { margin: 20px 0; padding: 15px; background: #f9f9f9; border-left: 3px solid #4CAF50; }"
        ".method { font-weight: bold; color: #4CAF50; }"
        ".path { color: #2196F3; font-family: monospace; }"
        "</style>"
        "</head>"
        "<body>"
        "<h1>Audio Loop Controller API</h1>"
        "<p>ESP32 Audio Loop Controller with JSON API</p>"
        
        "<h2>API Endpoints</h2>"
        
        "<div class='endpoint'>"
        "<span class='method'>GET</span> <span class='path'>/api/files</span>"
        "<p>List all audio files in the SD card root directory</p>"
        "<pre>Response:\n"
        "{\n"
        "  \"files\": [\n"
        "    { \"index\": 0, \"name\": \"track1.wav\", \"type\": \"wav\", \"path\": \"/sdcard/track1.wav\" },\n"
        "    { \"index\": 1, \"name\": \"track2.mp3\", \"type\": \"mp3\", \"path\": \"/sdcard/track2.mp3\" }\n"
        "  ],\n"
        "  \"count\": 2\n"
        "}</pre>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method'>GET</span> <span class='path'>/api/loops</span>"
        "<p>List currently playing loops</p>"
        "<pre>Response:\n"
        "{\n"
        "  \"loops\": [\n"
        "    { \"track\": 0, \"file\": \"/sdcard/track1.wav\", \"volume\": 100, \"playing\": true }\n"
        "  ],\n"
        "  \"active_count\": 1,\n"
        "  \"max_tracks\": 3,\n"
        "  \"global_volume\": 75\n"
        "}</pre>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method'>POST</span> <span class='path'>/api/loop/file</span>"
        "<p>Set the file for a specific track (starts playing immediately)</p>"
        "<pre>Request:\n"
        "{\n"
        "  \"track\": 0,\n"
        "  \"file_index\": 0  // OR \"file_path\": \"/sdcard/track1.wav\"\n"
        "}</pre>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method'>POST</span> <span class='path'>/api/loop/start</span>"
        "<p>Start/restart playing a track with its currently set file</p>"
        "<pre>Request:\n"
        "{\n"
        "  \"track\": 0\n"
        "}</pre>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method'>POST</span> <span class='path'>/api/loop/stop</span>"
        "<p>Stop a loop on a specific track</p>"
        "<pre>Request:\n"
        "{\n"
        "  \"track\": 0\n"
        "}</pre>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method'>POST</span> <span class='path'>/api/loop/volume</span>"
        "<p>Set volume for a specific loop (0-100%)</p>"
        "<pre>Request:\n"
        "{\n"
        "  \"track\": 0,\n"
        "  \"volume\": 75\n"
        "}</pre>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method'>POST</span> <span class='path'>/api/global/volume</span>"
        "<p>Set global/master volume (0-100%)</p>"
        "<pre>Request:\n"
        "{\n"
        "  \"volume\": 85\n"
        "}</pre>"
        "</div>"
        
        "</body>"
        "</html>";
    
    httpd_resp_set_type(req, "text/html");
    return httpd_resp_send(req, html, strlen(html));
}

/**
 * @brief Initialize HTTP server
 */
esp_err_t http_server_init(audio_stream_t *audio_stream, QueueHandle_t audio_control_queue) {
    if (server != NULL) {
        ESP_LOGW(TAG, "HTTP server already initialized");
        return ESP_OK;
    }
    
    // Note: loop manager will be set by audio_control_task via http_server_set_loop_manager
    // We don't create one here - we'll use the shared one from audio control task
    g_loop_manager = NULL;
    
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = HTTP_SERVER_PORT;
    config.stack_size = 8192;
    config.max_uri_handlers = 10;
    config.recv_wait_timeout = 10;
    config.send_wait_timeout = 10;
    
    ESP_LOGI(TAG, "Starting HTTP server on port %d", config.server_port);
    
    if (httpd_start(&server, &config) != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start HTTP server");
        return ESP_FAIL;
    }
    
    // Register URI handlers
    httpd_uri_t root_uri = {
        .uri = "/",
        .method = HTTP_GET,
        .handler = root_get_handler,
        .user_ctx = NULL
    };
    httpd_register_uri_handler(server, &root_uri);
    
    httpd_uri_t files_uri = {
        .uri = "/api/files",
        .method = HTTP_GET,
        .handler = files_get_handler,
        .user_ctx = NULL
    };
    httpd_register_uri_handler(server, &files_uri);
    
    httpd_uri_t loops_uri = {
        .uri = "/api/loops",
        .method = HTTP_GET,
        .handler = loops_get_handler,
        .user_ctx = NULL
    };
    httpd_register_uri_handler(server, &loops_uri);
    
    httpd_uri_t file_uri = {
        .uri = "/api/loop/file",
        .method = HTTP_POST,
        .handler = loop_file_handler,
        .user_ctx = NULL
    };
    httpd_register_uri_handler(server, &file_uri);
    
    httpd_uri_t start_uri = {
        .uri = "/api/loop/start",
        .method = HTTP_POST,
        .handler = loop_start_handler,
        .user_ctx = NULL
    };
    httpd_register_uri_handler(server, &start_uri);
    
    httpd_uri_t stop_uri = {
        .uri = "/api/loop/stop",
        .method = HTTP_POST,
        .handler = loop_stop_handler,
        .user_ctx = NULL
    };
    httpd_register_uri_handler(server, &stop_uri);
    
    httpd_uri_t volume_uri = {
        .uri = "/api/loop/volume",
        .method = HTTP_POST,
        .handler = loop_volume_handler,
        .user_ctx = NULL
    };
    httpd_register_uri_handler(server, &volume_uri);
    
    httpd_uri_t global_volume_uri = {
        .uri = "/api/global/volume",
        .method = HTTP_POST,
        .handler = global_volume_handler,
        .user_ctx = NULL
    };
    httpd_register_uri_handler(server, &global_volume_uri);
    
    // Register WiFi management endpoints
    httpd_uri_t wifi_status_uri = {
        .uri = "/api/wifi/status",
        .method = HTTP_GET,
        .handler = wifi_status_handler,
        .user_ctx = NULL
    };
    httpd_register_uri_handler(server, &wifi_status_uri);
    
    httpd_uri_t wifi_networks_uri = {
        .uri = "/api/wifi/networks",
        .method = HTTP_GET,
        .handler = wifi_networks_handler,
        .user_ctx = NULL
    };
    httpd_register_uri_handler(server, &wifi_networks_uri);
    
    httpd_uri_t wifi_add_uri = {
        .uri = "/api/wifi/add",
        .method = HTTP_POST,
        .handler = wifi_add_network_handler,
        .user_ctx = NULL
    };
    httpd_register_uri_handler(server, &wifi_add_uri);
    
    httpd_uri_t wifi_remove_uri = {
        .uri = "/api/wifi/remove",
        .method = HTTP_POST,
        .handler = wifi_remove_network_handler,
        .user_ctx = NULL
    };
    httpd_register_uri_handler(server, &wifi_remove_uri);
    
    ESP_LOGI(TAG, "HTTP server started successfully");
    ESP_LOGI(TAG, "API available at http://<device-ip>/");
    ESP_LOGI(TAG, "WiFi management available at /api/wifi/*");
    
    return ESP_OK;
}

/**
 * @brief Stop HTTP server
 */
esp_err_t http_server_stop(void) {
    if (server == NULL) {
        return ESP_OK;
    }
    
    ESP_LOGI(TAG, "Stopping HTTP server");
    httpd_stop(server);
    server = NULL;
    
    // Note: We don't free g_loop_manager here because it's owned by audio_control_task
    g_loop_manager = NULL;
    
    return ESP_OK;
}

/**
 * @brief Get current loop status
 */
esp_err_t http_server_get_loop_status(loop_manager_t *manager) {
    if (!manager || !g_loop_manager) {
        return ESP_ERR_INVALID_ARG;
    }
    
    memcpy(manager, g_loop_manager, sizeof(loop_manager_t));
    return ESP_OK;
}

/**
 * @brief Set the loop manager reference
 */
esp_err_t http_server_set_loop_manager(loop_manager_t *manager) {
    if (!manager) {
        return ESP_ERR_INVALID_ARG;
    }
    
    g_loop_manager = manager;
    ESP_LOGI(TAG, "Loop manager reference updated");
    return ESP_OK;
}
