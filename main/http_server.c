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
#include "config_manager.h"
#include "unit_status_manager.h"
#include <sys/stat.h>
#include "esp_system.h"
#include "esp_timer.h"

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
static esp_err_t api_docs_handler(httpd_req_t *req);
// WiFi management handlers
static esp_err_t wifi_status_handler(httpd_req_t *req);
static esp_err_t wifi_networks_handler(httpd_req_t *req);
static esp_err_t wifi_add_network_handler(httpd_req_t *req);
static esp_err_t wifi_remove_network_handler(httpd_req_t *req);
// Configuration management handlers
static esp_err_t config_save_handler(httpd_req_t *req);
static esp_err_t config_load_handler(httpd_req_t *req);
static esp_err_t config_delete_handler(httpd_req_t *req);
static esp_err_t config_status_handler(httpd_req_t *req);
// Unit status handlers
static esp_err_t unit_status_handler(httpd_req_t *req);
static esp_err_t id_get_handler(httpd_req_t *req);
static esp_err_t id_set_handler(httpd_req_t *req);
static esp_err_t file_upload_handler(httpd_req_t *req);
static esp_err_t file_delete_handler(httpd_req_t *req);
static esp_err_t system_reboot_handler(httpd_req_t *req);

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
 * @brief GET /api/files - List all audio files in root directory with file sizes
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
            
            // Get file size
            struct stat file_stat;
            if (stat(full_path, &file_stat) == 0) {
                cJSON_AddNumberToObject(file_obj, "size", file_stat.st_size);
            } else {
                cJSON_AddNumberToObject(file_obj, "size", 0);
            }
            
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
 * Body: { "track": 0, "file_index": 0 } or { "track": 0, "file_path": "/sdcard/track1.wav" } or { "track": 0, "filename": "track1.wav" }
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
    
    // Get file path, filename, or index
    char file_path[256] = {0};
    cJSON *file_path_json = cJSON_GetObjectItem(request, "file_path");
    cJSON *filename_json = cJSON_GetObjectItem(request, "filename");
    cJSON *file_index_json = cJSON_GetObjectItem(request, "file_index");
    
    if (cJSON_IsString(file_path_json)) {
        strncpy(file_path, file_path_json->valuestring, sizeof(file_path) - 1);
    } else if (cJSON_IsString(filename_json)) {
        // Handle filename parameter - just add /sdcard/ prefix
        const char *filename = filename_json->valuestring;
        
        // Security check: ensure filename doesn't contain path separators
        if (strchr(filename, '/') != NULL || strchr(filename, '\\') != NULL) {
            cJSON_AddBoolToObject(response, "success", false);
            cJSON_AddStringToObject(response, "error", "Invalid filename - path separators not allowed");
            send_json_response(req, response);
            cJSON_Delete(response);
            cJSON_Delete(request);
            return ESP_OK;
        }
        
        snprintf(file_path, sizeof(file_path), "/sdcard/%s", filename);
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
            cJSON_AddNumberToObject(network_obj, "auth_fail_count", networks[i].auth_fail_count);
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
 * @brief GET /api/config/status - Get configuration status
 */
static esp_err_t config_status_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "GET /api/config/status");
    
    cJSON *response = cJSON_CreateObject();
    
    // Check if configuration file exists
    bool config_exists_flag = config_exists();
    cJSON_AddBoolToObject(response, "config_exists", config_exists_flag);
    cJSON_AddStringToObject(response, "config_path", CONFIG_FILE_PATH);
    
    // If configuration exists, show current vs saved
    if (config_exists_flag && g_loop_manager) {
        // Load saved configuration
        loop_config_t saved_config;
        if (config_load(&saved_config) == ESP_OK) {
            // Compare current with saved
            cJSON *current = cJSON_CreateObject();
            cJSON *saved = cJSON_CreateObject();
            
            // Add current state
            cJSON_AddNumberToObject(current, "global_volume", g_loop_manager->global_volume_percent);
            cJSON *current_loops = cJSON_CreateArray();
            for (int i = 0; i < MAX_TRACKS; i++) {
                cJSON *loop = cJSON_CreateObject();
                cJSON_AddNumberToObject(loop, "track", i);
                cJSON_AddBoolToObject(loop, "playing", g_loop_manager->loops[i].is_playing);
                cJSON_AddStringToObject(loop, "file", g_loop_manager->loops[i].file_path);
                cJSON_AddNumberToObject(loop, "volume", g_loop_manager->loops[i].volume_percent);
                cJSON_AddItemToArray(current_loops, loop);
            }
            cJSON_AddItemToObject(current, "loops", current_loops);
            
            // Add saved state
            cJSON_AddNumberToObject(saved, "global_volume", saved_config.global_volume_percent);
            cJSON *saved_loops = cJSON_CreateArray();
            for (int i = 0; i < MAX_TRACKS; i++) {
                cJSON *loop = cJSON_CreateObject();
                cJSON_AddNumberToObject(loop, "track", i);
                cJSON_AddBoolToObject(loop, "playing", saved_config.loops[i].is_playing);
                cJSON_AddStringToObject(loop, "file", saved_config.loops[i].file_path);
                cJSON_AddNumberToObject(loop, "volume", saved_config.loops[i].volume_percent);
                cJSON_AddItemToArray(saved_loops, loop);
            }
            cJSON_AddItemToObject(saved, "loops", saved_loops);
            
            cJSON_AddItemToObject(response, "current_config", current);
            cJSON_AddItemToObject(response, "saved_config", saved);
            
            // Check if configs match
            bool configs_match = (g_loop_manager->global_volume_percent == saved_config.global_volume_percent);
            for (int i = 0; i < MAX_TRACKS && configs_match; i++) {
                if (g_loop_manager->loops[i].is_playing != saved_config.loops[i].is_playing ||
                    strcmp(g_loop_manager->loops[i].file_path, saved_config.loops[i].file_path) != 0 ||
                    g_loop_manager->loops[i].volume_percent != saved_config.loops[i].volume_percent) {
                    configs_match = false;
                }
            }
            cJSON_AddBoolToObject(response, "configs_match", configs_match);
        }
    }
    
    esp_err_t ret = send_json_response(req, response);
    cJSON_Delete(response);
    
    return ret;
}

/**
 * @brief POST /api/config/save - Save current configuration
 */
static esp_err_t config_save_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "POST /api/config/save");
    
    cJSON *response = cJSON_CreateObject();
    
    if (!g_loop_manager) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Loop manager not initialized");
        send_json_response(req, response);
        cJSON_Delete(response);
        return ESP_OK;
    }
    
    // Save current configuration
    esp_err_t ret = config_save(g_loop_manager);
    
    if (ret == ESP_OK) {
        cJSON_AddBoolToObject(response, "success", true);
        cJSON_AddStringToObject(response, "message", "Configuration saved successfully");
        cJSON_AddStringToObject(response, "path", CONFIG_FILE_PATH);
    } else {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Failed to save configuration");
    }
    
    esp_err_t send_ret = send_json_response(req, response);
    cJSON_Delete(response);
    
    return send_ret;
}

/**
 * @brief POST /api/config/load - Load and apply saved configuration
 */
static esp_err_t config_load_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "POST /api/config/load");
    
    cJSON *response = cJSON_CreateObject();
    
    if (!g_loop_manager || !g_loop_manager->audio_control_queue) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Audio system not initialized");
        send_json_response(req, response);
        cJSON_Delete(response);
        return ESP_OK;
    }
    
    // Load configuration from file
    loop_config_t config;
    esp_err_t ret = config_load(&config);
    
    if (ret == ESP_OK) {
        // Apply the configuration
        ret = config_apply(&config, g_loop_manager->audio_control_queue, g_loop_manager);
        
        if (ret == ESP_OK) {
            cJSON_AddBoolToObject(response, "success", true);
            cJSON_AddStringToObject(response, "message", "Configuration loaded and applied successfully");
            
            // Return what was loaded
            cJSON *loaded_config = cJSON_CreateObject();
            cJSON_AddNumberToObject(loaded_config, "global_volume", config.global_volume_percent);
            cJSON *loops = cJSON_CreateArray();
            for (int i = 0; i < MAX_TRACKS; i++) {
                cJSON *loop = cJSON_CreateObject();
                cJSON_AddNumberToObject(loop, "track", i);
                cJSON_AddBoolToObject(loop, "playing", config.loops[i].is_playing);
                cJSON_AddStringToObject(loop, "file", config.loops[i].file_path);
                cJSON_AddNumberToObject(loop, "volume", config.loops[i].volume_percent);
                cJSON_AddItemToArray(loops, loop);
            }
            cJSON_AddItemToObject(loaded_config, "loops", loops);
            cJSON_AddItemToObject(response, "loaded_config", loaded_config);
        } else {
            cJSON_AddBoolToObject(response, "success", false);
            cJSON_AddStringToObject(response, "error", "Failed to apply configuration");
        }
    } else if (ret == ESP_ERR_NOT_FOUND) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "No saved configuration found");
    } else {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Failed to load configuration");
    }
    
    esp_err_t send_ret = send_json_response(req, response);
    cJSON_Delete(response);
    
    return send_ret;
}

/**
 * @brief DELETE /api/config/delete - Delete saved configuration
 */
static esp_err_t config_delete_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "DELETE /api/config/delete");
    
    cJSON *response = cJSON_CreateObject();
    
    esp_err_t ret = config_delete();
    
    if (ret == ESP_OK) {
        cJSON_AddBoolToObject(response, "success", true);
        cJSON_AddStringToObject(response, "message", "Configuration deleted successfully");
    } else {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Failed to delete configuration");
    }
    
    esp_err_t send_ret = send_json_response(req, response);
    cJSON_Delete(response);
    
    return send_ret;
}

/**
 * @brief GET /api/status - Get unit status including MAC, IP, unit ID, uptime
 */
static esp_err_t unit_status_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "GET /api/status");
    
    cJSON *response = cJSON_CreateObject();
    
    // Get unit status
    unit_status_t status;
    esp_err_t ret = unit_status_get(&status);
    
    if (ret == ESP_OK) {
        cJSON_AddStringToObject(response, "mac_address", status.mac_address);
        cJSON_AddStringToObject(response, "id", status.id);
        cJSON_AddStringToObject(response, "ip_address", status.ip_address);
        cJSON_AddBoolToObject(response, "wifi_connected", status.wifi_connected);
        cJSON_AddStringToObject(response, "firmware_version", status.firmware_version);
        cJSON_AddNumberToObject(response, "uptime_seconds", status.uptime_seconds);
        
        // Add human-readable uptime
        int days = status.uptime_seconds / 86400;
        int hours = (status.uptime_seconds % 86400) / 3600;
        int minutes = (status.uptime_seconds % 3600) / 60;
        int seconds = status.uptime_seconds % 60;
        
        char uptime_str[64];
        snprintf(uptime_str, sizeof(uptime_str), "%02d %02d:%02d:%02d", days, hours, minutes, seconds);
        cJSON_AddStringToObject(response, "uptime_formatted", uptime_str);
    } else {
        cJSON_AddBoolToObject(response, "error", true);
        cJSON_AddStringToObject(response, "message", "Failed to get unit status");
    }
    
    esp_err_t send_ret = send_json_response(req, response);
    cJSON_Delete(response);
    
    return send_ret;
}

/**
 * @brief GET /api/id - Get the current ID
 */
static esp_err_t id_get_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "GET /api/id");
    
    cJSON *response = cJSON_CreateObject();
    
    char id[MAX_UNIT_ID_LEN];
    esp_err_t ret = unit_status_get_id(id, sizeof(id));
    
    if (ret == ESP_OK) {
        cJSON_AddStringToObject(response, "id", id);
        cJSON_AddBoolToObject(response, "success", true);
    } else {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Failed to get unit ID");
    }
    
    esp_err_t send_ret = send_json_response(req, response);
    cJSON_Delete(response);
    
    return send_ret;
}

/**
 * @brief POST /api/id - Set the ID
 * Body: { "id": "LOUDFRAME-001" }
 */
static esp_err_t id_set_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "POST /api/id");
    
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
    
    // Get unit ID from request
    cJSON *id_json = cJSON_GetObjectItem(request, "id");
    if (!cJSON_IsString(id_json) || strlen(id_json->valuestring) == 0) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Missing or invalid id");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    // Set the unit ID
    esp_err_t ret = unit_status_set_id(id_json->valuestring);
    
    if (ret == ESP_OK) {
        cJSON_AddBoolToObject(response, "success", true);
        cJSON_AddStringToObject(response, "message", "Unit ID updated successfully");
        cJSON_AddStringToObject(response, "id", id_json->valuestring);
    } else {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Failed to set unit ID");
    }
    
    esp_err_t send_ret = send_json_response(req, response);
    cJSON_Delete(response);
    cJSON_Delete(request);
    
    return send_ret;
}

/**
 * @brief POST /api/upload - Upload audio file to SD card
 * Handles large file uploads by streaming directly to SD card
 */
static esp_err_t file_upload_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "POST /api/upload");
    
    // Buffer for reading chunks - keep small to avoid memory issues
    #define UPLOAD_CHUNK_SIZE 4096
    char *chunk_buf = heap_caps_malloc(UPLOAD_CHUNK_SIZE, MALLOC_CAP_SPIRAM);
    if (!chunk_buf) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Failed to allocate buffer");
        return ESP_FAIL;
    }
    
    // Parse query string to get filename
    char query_str[256] = {0};
    char filename[128] = {0};
    size_t query_len = httpd_req_get_url_query_len(req);
    
    if (query_len > 0 && query_len < sizeof(query_str)) {
        httpd_req_get_url_query_str(req, query_str, sizeof(query_str));
        
        // Extract filename from query string (e.g., ?filename=track.wav)
        char param_buf[128] = {0};
        if (httpd_query_key_value(query_str, "filename", param_buf, sizeof(param_buf)) == ESP_OK) {
            // URL decode the filename
            size_t decoded_len = 0;
            for (size_t i = 0, j = 0; i < strlen(param_buf) && j < sizeof(filename) - 1; i++, j++) {
                if (param_buf[i] == '%' && i + 2 < strlen(param_buf)) {
                    char hex[3] = {param_buf[i+1], param_buf[i+2], '\0'};
                    filename[j] = (char)strtol(hex, NULL, 16);
                    i += 2;
                } else if (param_buf[i] == '+') {
                    filename[j] = ' ';
                } else {
                    filename[j] = param_buf[i];
                }
                decoded_len = j + 1;
            }
            filename[decoded_len] = '\0';
        }
    }
    
    // If no filename provided, generate one based on timestamp
    if (strlen(filename) == 0) {
        snprintf(filename, sizeof(filename), "upload_%ld.wav", (long)esp_timer_get_time() / 1000000);
    }
    
    // Ensure filename doesn't contain path separators (security)
    // Remove any path components, keeping only the filename
    char *base_name = strrchr(filename, '/');
    if (base_name) {
        memmove(filename, base_name + 1, strlen(base_name));
    }
    base_name = strrchr(filename, '\\');
    if (base_name) {
        memmove(filename, base_name + 1, strlen(base_name));
    }
    
    // Build full path
    char filepath[256];
    snprintf(filepath, sizeof(filepath), "/sdcard/%s", filename);
    
    ESP_LOGI(TAG, "Uploading file: %s (size: %d bytes)", filepath, req->content_len);
    
    // Open file for writing
    FILE *file = fopen(filepath, "wb");
    if (!file) {
        ESP_LOGE(TAG, "Failed to open file for writing: %s", filepath);
        free(chunk_buf);
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Failed to create file");
        return ESP_FAIL;
    }
    
    // Read and write data in chunks
    size_t total_received = 0;
    size_t remaining = req->content_len;
    int64_t last_log_time = 0;  // Track last log time for progress updates
    
    while (remaining > 0) {
        // Determine how much to read this iteration
        size_t to_read = (remaining < UPLOAD_CHUNK_SIZE) ? remaining : UPLOAD_CHUNK_SIZE;
        
        // Read chunk from HTTP request
        int received = httpd_req_recv(req, chunk_buf, to_read);
        
        if (received <= 0) {
            if (received == HTTPD_SOCK_ERR_TIMEOUT) {
                // Retry if timeout
                ESP_LOGW(TAG, "Upload timeout, retrying...");
                continue;
            }
            ESP_LOGE(TAG, "Upload failed: error receiving data");
            fclose(file);
            remove(filepath);  // Clean up partial file
            free(chunk_buf);
            httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Upload failed");
            return ESP_FAIL;
        }
        
        // Write chunk to file
        size_t written = fwrite(chunk_buf, 1, received, file);
        if (written != received) {
            ESP_LOGE(TAG, "Failed to write to file: wrote %d of %d bytes", written, received);
            fclose(file);
            remove(filepath);  // Clean up partial file
            free(chunk_buf);
            httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Failed to write file");
            return ESP_FAIL;
        }
        
        total_received += received;
        remaining -= received;
        
        // Log progress for large files (at most once every 10 seconds)
        if (req->content_len > 1024 * 1024) {  // If larger than 1MB
            int64_t current_time = esp_timer_get_time() / 1000000;  // Convert to seconds
            if (current_time - last_log_time >= 10) {  // Log every 10 seconds
                int percent = (total_received * 100) / req->content_len;
                ESP_LOGI(TAG, "Upload progress: %d%% (%d/%d bytes)", 
                         percent, total_received, req->content_len);
                last_log_time = current_time;
            }
        }
    }
    
    // Close file
    fclose(file);
    free(chunk_buf);
    
    ESP_LOGI(TAG, "File uploaded successfully: %s (%d bytes)", filename, total_received);
    
    // Send success response
    cJSON *response = cJSON_CreateObject();
    cJSON_AddBoolToObject(response, "success", true);
    cJSON_AddStringToObject(response, "filename", filename);
    cJSON_AddStringToObject(response, "path", filepath);
    cJSON_AddNumberToObject(response, "size", total_received);
    cJSON_AddStringToObject(response, "message", "File uploaded successfully");
    
    esp_err_t ret = send_json_response(req, response);
    cJSON_Delete(response);
    
    return ret;
}

/**
 * @brief DELETE /api/file/delete - Delete an audio file from SD card
 * Body: { "filename": "track.wav" }
 */
static esp_err_t file_delete_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "DELETE /api/file/delete");
    
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
    
    // Get filename from request
    cJSON *filename_json = cJSON_GetObjectItem(request, "filename");
    if (!cJSON_IsString(filename_json) || strlen(filename_json->valuestring) == 0) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Missing or invalid filename");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    char *filename = filename_json->valuestring;
    
    // Security check: ensure filename doesn't contain path separators
    if (strchr(filename, '/') != NULL || strchr(filename, '\\') != NULL) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Invalid filename - path separators not allowed");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    // Build full path
    char filepath[256];
    snprintf(filepath, sizeof(filepath), "/sdcard/%s", filename);
    
    // Check if file exists
    struct stat file_stat;
    if (stat(filepath, &file_stat) != 0) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "File not found");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    // Check if it's a regular file (not a directory)
    if (!S_ISREG(file_stat.st_mode)) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Not a regular file");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    // Delete the file
    if (remove(filepath) == 0) {
        ESP_LOGI(TAG, "File deleted successfully: %s", filename);
        cJSON_AddBoolToObject(response, "success", true);
        cJSON_AddStringToObject(response, "filename", filename);
        cJSON_AddStringToObject(response, "message", "File deleted successfully");
    } else {
        ESP_LOGE(TAG, "Failed to delete file: %s", filename);
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Failed to delete file");
    }
    
    esp_err_t ret = send_json_response(req, response);
    cJSON_Delete(response);
    cJSON_Delete(request);
    
    return ret;
}

/**
 * @brief POST /api/system/reboot - Reboot the system
 * Body: { "delay_ms": 1000 } (optional, defaults to 1000ms)
 */
static esp_err_t system_reboot_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "POST /api/system/reboot");
    
    int delay_ms = 1000; // Default delay
    
    // Parse request body if present
    if (req->content_len > 0) {
        cJSON *request = parse_json_request(req);
        if (request) {
            cJSON *delay_json = cJSON_GetObjectItem(request, "delay_ms");
            if (cJSON_IsNumber(delay_json)) {
                delay_ms = delay_json->valueint;
                // Clamp delay to reasonable range (100ms to 10s)
                if (delay_ms < 100) delay_ms = 100;
                if (delay_ms > 10000) delay_ms = 10000;
            }
            cJSON_Delete(request);
        }
    }
    
    // Send response before rebooting
    cJSON *response = cJSON_CreateObject();
    cJSON_AddBoolToObject(response, "success", true);
    cJSON_AddStringToObject(response, "message", "System will reboot");
    cJSON_AddNumberToObject(response, "delay_ms", delay_ms);
    
    esp_err_t ret = send_json_response(req, response);
    cJSON_Delete(response);
    
    // Give time for response to be sent
    vTaskDelay(pdMS_TO_TICKS(100));
    
    ESP_LOGI(TAG, "Rebooting system in %d ms...", delay_ms);
    
    // Delay before reboot
    vTaskDelay(pdMS_TO_TICKS(delay_ms));
    
    // Perform system restart
    esp_restart();
    
    // This line will never be reached
    return ret;
}

/**
 * @brief GET /api-docs - API documentation handler
 */
static esp_err_t api_docs_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "GET /api-docs");
    
    const char *html = 
        "<!DOCTYPE html>"
        "<html>"
        "<head>"
        "<title>Loudframe API Documentation</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
        "<style>"
        "* { box-sizing: border-box; margin: 0; padding: 0; }"
        "body { "
        "  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif; "
        "  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); "
        "  min-height: 100vh; "
        "  padding: 10px; "
        "}"
        ".container { max-width: 800px; margin: 0 auto; }"
        ".header { "
        "  text-align: center; "
        "  color: white; "
        "  margin: 20px 0; "
        "}"
        ".header h1 { font-size: 24px; margin-bottom: 10px; }"
        ".header a { "
        "  color: white; "
        "  text-decoration: none; "
        "  opacity: 0.9; "
        "  font-size: 14px; "
        "}"
        ".header a:hover { opacity: 1; text-decoration: underline; }"
        ".card { "
        "  background: white; "
        "  border-radius: 12px; "
        "  padding: 20px; "
        "  margin: 10px 0; "
        "  box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1); "
        "}"
        "h2 { "
        "  color: #333; "
        "  font-size: 20px; "
        "  margin: 20px 0 15px 0; "
        "  padding-bottom: 10px; "
        "  border-bottom: 2px solid #667eea; "
        "}"
        ".endpoint { "
        "  background: #f8f9fa; "
        "  border-radius: 8px; "
        "  padding: 15px; "
        "  margin: 15px 0; "
        "  border-left: 4px solid #667eea; "
        "}"
        ".method { "
        "  display: inline-block; "
        "  font-weight: 600; "
        "  padding: 3px 8px; "
        "  border-radius: 4px; "
        "  margin-right: 10px; "
        "  font-size: 12px; "
        "}"
        ".method-get { background: #4caf50; color: white; }"
        ".method-post { background: #2196f3; color: white; }"
        ".method-delete { background: #f44336; color: white; }"
        ".path { "
        "  font-family: 'Courier New', monospace; "
        "  color: #333; "
        "  font-weight: 600; "
        "}"
        ".description { "
        "  color: #666; "
        "  margin: 10px 0; "
        "  font-size: 14px; "
        "}"
        "pre { "
        "  background: #2d2d2d; "
        "  color: #f8f8f2; "
        "  padding: 12px; "
        "  border-radius: 6px; "
        "  overflow-x: auto; "
        "  margin: 10px 0; "
        "  font-size: 12px; "
        "  line-height: 1.4; "
        "}"
        "@media (max-width: 600px) { "
        "  .container { padding: 0 10px; } "
        "  h1 { font-size: 20px; } "
        "  .card { padding: 15px; } "
        "  pre { font-size: 11px; } "
        "}"
        "</style>"
        "</head>"
        "<body>"
        "<div class='container'>"
        "<div class='header'>"
        "<h1>Loudframe API Documentation</h1>"
        "<a href='/'>Back to Controller</a>"
        "</div>"
        
        "<div class='card'>"
        "<h2>Audio Control Endpoints</h2>"
        
        "<div class='endpoint'>"
        "<span class='method method-get'>GET</span>"
        "<span class='path'>/api/files</span>"
        "<p class='description'>List all audio files on the SD card</p>"
        "<pre>"
        "Response:\n"
        "{\n"
        "  \"files\": [\n"
        "    {\n"
        "      \"index\": 0,\n"
        "      \"name\": \"track1.wav\",\n"
        "      \"type\": \"wav\",\n"
        "      \"path\": \"/sdcard/track1.wav\"\n"
        "    }\n"
        "  ],\n"
        "  \"count\": 1\n"
        "}</pre>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method method-get'>GET</span>"
        "<span class='path'>/api/loops</span>"
        "<p class='description'>Get status of all loop tracks</p>"
        "<pre>"
        "Response:\n"
        "{\n"
        "  \"loops\": [\n"
        "    {\n"
        "      \"track\": 0,\n"
        "      \"file\": \"/sdcard/track1.wav\",\n"
        "      \"volume\": 100,\n"
        "      \"playing\": true\n"
        "    }\n"
        "  ],\n"
        "  \"active_count\": 1,\n"
        "  \"max_tracks\": 3,\n"
        "  \"global_volume\": 75\n"
        "}</pre>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method method-post'>POST</span>"
        "<span class='path'>/api/loop/file</span>"
        "<p class='description'>Set file for a track and start playing</p>"
        "<pre>"
        "Request:\n"
        "{\n"
        "  \"track\": 0,\n"
        "  \"file_index\": 0  // OR \"file_path\": \"/sdcard/track1.wav\"\n"
        "}</pre>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method method-post'>POST</span>"
        "<span class='path'>/api/loop/stop</span>"
        "<p class='description'>Stop a specific track</p>"
        "<pre>"
        "Request:\n"
        "{\n"
        "  \"track\": 0\n"
        "}</pre>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method method-post'>POST</span>"
        "<span class='path'>/api/loop/volume</span>"
        "<p class='description'>Set volume for a specific track (0-100%)</p>"
        "<pre>"
        "Request:\n"
        "{\n"
        "  \"track\": 0,\n"
        "  \"volume\": 75\n"
        "}</pre>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method method-post'>POST</span>"
        "<span class='path'>/api/global/volume</span>"
        "<p class='description'>Set global/master volume (0-100%)</p>"
        "<pre>"
        "Request:\n"
        "{\n"
        "  \"volume\": 85\n"
        "}</pre>"
        "</div>"
        "</div>"
        
        "<div class='card'>"
        "<h2>System Status Endpoints</h2>"
        
        "<div class='endpoint'>"
        "<span class='method method-get'>GET</span>"
        "<span class='path'>/api/status</span>"
        "<p class='description'>Get unit status information</p>"
        "<pre>"
        "Response:\n"
        "{\n"
        "  \"mac_address\": \"AA:BB:CC:DD:EE:FF\",\n"
        "  \"id\": \"LOUDFRAME-001\",\n"
        "  \"ip_address\": \"192.168.1.100\",\n"
        "  \"wifi_connected\": true,\n"
        "  \"firmware_version\": \"1.0.0\",\n"
        "  \"uptime_seconds\": 3600,\n"
        "  \"uptime_formatted\": \"00 01:00:00\"\n"
        "}</pre>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method method-get'>GET</span>"
        "<span class='path'>/api/id</span>"
        "<p class='description'>Get the device ID</p>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method method-post'>POST</span>"
        "<span class='path'>/api/id</span>"
        "<p class='description'>Set the device ID</p>"
        "<pre>"
        "Request:\n"
        "{\n"
        "  \"id\": \"LOUDFRAME-001\"\n"
        "}</pre>"
        "</div>"
        "</div>"
        
        "<div class='card'>"
        "<h2>WiFi Management Endpoints</h2>"
        
        "<div class='endpoint'>"
        "<span class='method method-get'>GET</span>"
        "<span class='path'>/api/wifi/status</span>"
        "<p class='description'>Get WiFi connection status</p>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method method-get'>GET</span>"
        "<span class='path'>/api/wifi/networks</span>"
        "<p class='description'>List configured WiFi networks</p>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method method-post'>POST</span>"
        "<span class='path'>/api/wifi/add</span>"
        "<p class='description'>Add a new WiFi network</p>"
        "<pre>"
        "Request:\n"
        "{\n"
        "  \"ssid\": \"NetworkName\",\n"
        "  \"password\": \"NetworkPassword\"\n"
        "}</pre>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method method-post'>POST</span>"
        "<span class='path'>/api/wifi/remove</span>"
        "<p class='description'>Remove a WiFi network</p>"
        "<pre>"
        "Request:\n"
        "{\n"
        "  \"ssid\": \"NetworkName\"\n"
        "}</pre>"
        "</div>"
        "</div>"
        
        "<div class='card'>"
        "<h2>Configuration Management</h2>"
        
        "<div class='endpoint'>"
        "<span class='method method-get'>GET</span>"
        "<span class='path'>/api/config/status</span>"
        "<p class='description'>Get configuration status</p>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method method-post'>POST</span>"
        "<span class='path'>/api/config/save</span>"
        "<p class='description'>Save current configuration</p>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method method-post'>POST</span>"
        "<span class='path'>/api/config/load</span>"
        "<p class='description'>Load and apply saved configuration</p>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method method-delete'>DELETE</span>"
        "<span class='path'>/api/config/delete</span>"
        "<p class='description'>Delete saved configuration</p>"
        "</div>"
        "</div>"
        
        "<div class='card'>"
        "<h2>File Management</h2>"
        
        "<div class='endpoint'>"
        "<span class='method method-post'>POST</span>"
        "<span class='path'>/api/upload?filename=track.wav</span>"
        "<p class='description'>Upload an audio file to the SD card. Supports large files (100+ MB) via streaming.</p>"
        "<pre>"
        "Upload using curl:\n"
        "curl -X POST \"http://&lt;device-ip&gt;/api/upload?filename=track.wav\" \\\n"
        "     -H \"Content-Type: application/octet-stream\" \\\n"
        "     --data-binary @localfile.wav\n"
        "\n"
        "Response:\n"
        "{\n"
        "  \"success\": true,\n"
        "  \"filename\": \"track.wav\",\n"
        "  \"path\": \"/sdcard/track.wav\",\n"
        "  \"size\": 1048576\n"
        "}</pre>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method method-delete'>DELETE</span>"
        "<span class='path'>/api/file/delete</span>"
        "<p class='description'>Delete an audio file from the SD card by name.</p>"
        "<pre>"
        "Request:\n"
        "{\n"
        "  \"filename\": \"track.wav\"\n"
        "}\n"
        "\n"
        "Response:\n"
        "{\n"
        "  \"success\": true,\n"
        "  \"filename\": \"track.wav\",\n"
        "  \"message\": \"File deleted successfully\"\n"
        "}</pre>"
        "</div>"
        "</div>"
        
        "</div>"
        "</body>"
        "</html>";
    
    httpd_resp_set_type(req, "text/html");
    return httpd_resp_send(req, html, strlen(html));
}

/**
 * @brief GET /favicon.ico - Favicon handler (returns empty icon to avoid 404)
 */
static esp_err_t favicon_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "GET /favicon.ico");
    
    // Return a minimal valid ICO file (1x1 transparent pixel)
    static const uint8_t favicon_data[] = {
        0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x01, 0x01, 0x00, 0x00,
        0x01, 0x00, 0x18, 0x00, 0x30, 0x00, 0x00, 0x00, 0x16, 0x00,
        0x00, 0x00, 0x28, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00,
        0x02, 0x00, 0x00, 0x00, 0x01, 0x00, 0x18, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
    };
    
    httpd_resp_set_type(req, "image/x-icon");
    httpd_resp_set_hdr(req, "Cache-Control", "public, max-age=31536000");
    return httpd_resp_send(req, (const char *)favicon_data, sizeof(favicon_data));
}

/**
 * @brief GET /settings - Settings page handler
 */
static esp_err_t settings_get_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "GET /settings");
    
    const char *html = 
        "<!DOCTYPE html>"
        "<html>"
        "<head>"
        "<title>Loudframe Settings</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
        "<style>"
        "* { box-sizing: border-box; margin: 0; padding: 0; }"
        "body { "
        "  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif; "
        "  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); "
        "  min-height: 100vh; "
        "  padding: 10px; "
        "}"
        ".container { max-width: 600px; margin: 0 auto; }"
        ".card { "
        "  background: white; "
        "  border-radius: 12px; "
        "  padding: 20px; "
        "  margin: 10px 0; "
        "  box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1); "
        "}"
        "h1 { "
        "  color: white; "
        "  text-align: center; "
        "  margin: 20px 0; "
        "  font-size: 24px; "
        "}"
        "h2 { "
        "  color: #333; "
        "  font-size: 18px; "
        "  margin-bottom: 15px; "
        "  padding-bottom: 10px; "
        "  border-bottom: 2px solid #667eea; "
        "}"
        ".menu-bar { "
        "  background: rgba(255, 255, 255, 0.1); "
        "  border-radius: 8px; "
        "  padding: 10px; "
        "  margin-bottom: 20px; "
        "  display: flex; "
        "  gap: 10px; "
        "  justify-content: center; "
        "  flex-wrap: wrap; "
        "}"
        ".menu-btn { "
        "  background: white; "
        "  color: #667eea; "
        "  border: none; "
        "  padding: 8px 16px; "
        "  border-radius: 6px; "
        "  font-size: 14px; "
        "  font-weight: 600; "
        "  cursor: pointer; "
        "  text-decoration: none; "
        "  display: inline-block; "
        "  transition: all 0.3s ease; "
        "}"
        ".menu-btn:hover { "
        "  background: #667eea; "
        "  color: white; "
        "  transform: translateY(-2px); "
        "}"
        ".menu-btn.active { "
        "  background: #667eea; "
        "  color: white; "
        "}"
        ".form-group { "
        "  margin: 20px 0; "
        "}"
        "label { "
        "  display: block; "
        "  color: #666; "
        "  font-weight: 500; "
        "  margin-bottom: 8px; "
        "}"
        "input[type='text'] { "
        "  width: 100%; "
        "  padding: 10px; "
        "  border: 2px solid #e0e0e0; "
        "  border-radius: 6px; "
        "  font-size: 14px; "
        "  transition: border-color 0.3s ease; "
        "}"
        "input[type='text']:focus { "
        "  outline: none; "
        "  border-color: #667eea; "
        "}"
        ".btn-primary { "
        "  background: #667eea; "
        "  color: white; "
        "  border: none; "
        "  padding: 10px 20px; "
        "  border-radius: 8px; "
        "  font-size: 14px; "
        "  font-weight: 600; "
        "  cursor: pointer; "
        "  margin-right: 10px; "
        "}"
        ".btn-primary:hover { background: #5a67d8; }"
        ".btn-secondary { "
        "  background: #e0e0e0; "
        "  color: #333; "
        "  border: none; "
        "  padding: 10px 20px; "
        "  border-radius: 8px; "
        "  font-size: 14px; "
        "  font-weight: 600; "
        "  cursor: pointer; "
        "}"
        ".btn-secondary:hover { background: #d0d0d0; }"
        ".status-message { "
        "  padding: 12px; "
        "  border-radius: 6px; "
        "  margin: 15px 0; "
        "  font-size: 14px; "
        "  display: none; "
        "}"
        ".status-message.success { "
        "  background: #e8f5e9; "
        "  color: #2e7d32; "
        "  border: 1px solid #4caf50; "
        "  display: block; "
        "}"
        ".status-message.error { "
        "  background: #ffebee; "
        "  color: #c62828; "
        "  border: 1px solid #f44336; "
        "  display: block; "
        "}"
        ".current-value { "
        "  background: #f5f5f5; "
        "  padding: 8px 12px; "
        "  border-radius: 6px; "
        "  margin-bottom: 10px; "
        "  color: #666; "
        "  font-size: 14px; "
        "}"
        "@media (max-width: 480px) { "
        "  h1 { font-size: 20px; } "
        "  .card { padding: 15px; } "
        "}"
        "</style>"
        "</head>"
        "<body>"
        "<div class='container'>"
        "<h1>Loudframe Settings</h1>"
        
        "<div class='menu-bar'>"
        "<a href='/' class='menu-btn'>Status</a>"
        "<a href='/settings' class='menu-btn active'>Settings</a>"
        "<a href='/api-docs' class='menu-btn'>API Docs</a>"
        "</div>"
        
        "<div class='card'>"
        "<h2>Device ID</h2>"
        "<div id='status-message' class='status-message'></div>"
        "<div class='current-value'>Current ID: <span id='current-id'>Loading...</span></div>"
        "<div class='form-group'>"
        "<label for='device-id'>ID:</label>"
        "<input type='text' id='device-id' placeholder='Enter device ID (e.g., LOUDFRAME-001)' maxlength='32'>"
        "</div>"
        "<button class='btn-primary' onclick='updateDeviceId()'>Update ID</button>"
        "<button class='btn-secondary' onclick='loadCurrentId()'>Refresh</button>"
        "</div>"
        "</div>"
        
        "<script>"
        "console.log('[S1] Script start');"
        "function loadCurrentId() {"
        "  console.log('[S2] loadCurrentId called');"
        "  fetch('/api/id')"
        "    .then(function(r) {"
        "      console.log('[S3] Got resp:', r.status);"
        "      if (!r.ok) throw new Error('HTTP err');"
        "      return r.json();"
        "    })"
        "    .then(function(d) {"
        "      console.log('[S4] Data:', d);"
        "      if (d.success && d.id) {"
        "        document.getElementById('current-id').textContent = d.id;"
        "        document.getElementById('device-id').value = d.id;"
        "      } else {"
        "        document.getElementById('current-id').textContent = 'Not Set';"
        "      }"
        "    })"
        "    .catch(function(e) {"
        "      console.error('[S5] Err:', e);"
        "      document.getElementById('current-id').textContent = 'Error';"
        "    });"
        "}"
        ""
        "function updateDeviceId() {"
        "  var id = document.getElementById('device-id').value.trim();"
        "  var msg = document.getElementById('status-message');"
        "  if (!id) {"
        "    msg.className = 'status-message error';"
        "    msg.textContent = 'Please enter a device ID';"
        "    return;"
        "  }"
        "  fetch('/api/id', {"
        "    method: 'POST',"
        "    headers: {'Content-Type': 'application/json'},"
        "    body: JSON.stringify({id: id})"
        "  })"
        "  .then(function(r) { return r.json(); })"
        "  .then(function(d) {"
        "    if (d.success) {"
        "      msg.className = 'status-message success';"
        "      msg.textContent = 'ID updated!';"
        "      document.getElementById('current-id').textContent = id;"
        "      setTimeout(function() { msg.style.display = 'none'; }, 3000);"
        "    } else {"
        "      msg.className = 'status-message error';"
        "      msg.textContent = d.error || 'Failed';"
        "    }"
        "  })"
        "  .catch(function(e) {"
        "    msg.className = 'status-message error';"
        "    msg.textContent = 'Network error';"
        "  });"
        "}"
        ""
        "console.log('[S6] Funcs defined');"
        "if (document.readyState === 'loading') {"
        "  console.log('[S7] Wait for DOM');"
        "  document.addEventListener('DOMContentLoaded', function() {"
        "    console.log('[S8] DOM ready');"
        "    loadCurrentId();"
        "  });"
        "} else {"
        "  console.log('[S9] Direct call');"
        "  loadCurrentId();"
        "}"
        "console.log('[S10] Script end');"
        "</script>"
        "<!-- END -->"
        "</body>"
        "</html>";
    
    // Check response size for potential truncation
    size_t html_size = strlen(html);
    ESP_LOGD(TAG, "Settings page HTML size: %d bytes", html_size);
    
    // ESP32 HTTP server typically has a limit around 16KB for single response
    const size_t MAX_RESPONSE_SIZE = 16384;  // 16KB typical limit
    const size_t WARNING_THRESHOLD = 14336;  // 14KB warning threshold (87.5% of max)
    
    if (html_size >= MAX_RESPONSE_SIZE) {
        ESP_LOGE(TAG, "WARNING: Settings page HTML exceeds maximum response size!");
        ESP_LOGE(TAG, "HTML size: %d bytes, Max size: %d bytes", html_size, MAX_RESPONSE_SIZE);
        ESP_LOGE(TAG, "Response will likely be truncated!");
    } else if (html_size >= WARNING_THRESHOLD) {
        ESP_LOGW(TAG, "Settings page HTML approaching maximum response size");
        ESP_LOGW(TAG, "HTML size: %d bytes, Warning at: %d bytes, Max: %d bytes", 
                 html_size, WARNING_THRESHOLD, MAX_RESPONSE_SIZE);
    }
    
    httpd_resp_set_type(req, "text/html");
    esp_err_t ret = httpd_resp_send(req, html, html_size);
    
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to send settings page response: %s", esp_err_to_name(ret));
    } else {
        ESP_LOGD(TAG, "Settings page sent successfully (%d bytes)", html_size);
    }
    
    return ret;
}

/**
 * @brief GET / - Root handler with status display
 */
static esp_err_t root_get_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "GET /");
    
    const char *html = 
        "<!DOCTYPE html>"
        "<html>"
        "<head>"
        "<title>Loudframe Controller</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
        "<style>"
        "* { box-sizing: border-box; margin: 0; padding: 0; }"
        "body { "
        "  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif; "
        "  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); "
        "  min-height: 100vh; "
        "  padding: 10px; "
        "}"
        ".container { max-width: 600px; margin: 0 auto; }"
        ".card { "
        "  background: white; "
        "  border-radius: 12px; "
        "  padding: 20px; "
        "  margin: 10px 0; "
        "  box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1); "
        "}"
        "h1 { "
        "  color: white; "
        "  text-align: center; "
        "  margin: 20px 0; "
        "  font-size: 24px; "
        "}"
        "h2 { "
        "  color: #333; "
        "  font-size: 18px; "
        "  margin-bottom: 15px; "
        "  padding-bottom: 10px; "
        "  border-bottom: 2px solid #667eea; "
        "}"
        ".menu-bar { "
        "  background: rgba(255, 255, 255, 0.1); "
        "  border-radius: 8px; "
        "  padding: 10px; "
        "  margin-bottom: 20px; "
        "  display: flex; "
        "  gap: 10px; "
        "  justify-content: center; "
        "  flex-wrap: wrap; "
        "}"
        ".menu-btn { "
        "  background: white; "
        "  color: #667eea; "
        "  border: none; "
        "  padding: 8px 16px; "
        "  border-radius: 6px; "
        "  font-size: 14px; "
        "  font-weight: 600; "
        "  cursor: pointer; "
        "  text-decoration: none; "
        "  display: inline-block; "
        "  transition: all 0.3s ease; "
        "}"
        ".menu-btn:hover { "
        "  background: #667eea; "
        "  color: white; "
        "  transform: translateY(-2px); "
        "}"
        ".menu-btn.active { "
        "  background: #667eea; "
        "  color: white; "
        "}"
        ".status-item { "
        "  display: flex; "
        "  justify-content: space-between; "
        "  padding: 8px 0; "
        "  border-bottom: 1px solid #eee; "
        "}"
        ".status-item:last-child { border-bottom: none; }"
        ".label { "
        "  color: #666; "
        "  font-weight: 500; "
        "}"
        ".value { "
        "  color: #333; "
        "  font-weight: 600; "
        "  text-align: right; "
        "  word-break: break-all; "
        "}"
        ".track { "
        "  background: #f8f9fa; "
        "  border-radius: 8px; "
        "  padding: 12px; "
        "  margin: 10px 0; "
        "}"
        ".track-header { "
        "  display: flex; "
        "  justify-content: space-between; "
        "  align-items: center; "
        "  margin-bottom: 8px; "
        "}"
        ".track-title { "
        "  font-weight: 600; "
        "  color: #333; "
        "}"
        ".playing-badge { "
        "  background: #4caf50; "
        "  color: white; "
        "  padding: 2px 8px; "
        "  border-radius: 12px; "
        "  font-size: 12px; "
        "  font-weight: 600; "
        "}"
        ".stopped-badge { "
        "  background: #9e9e9e; "
        "  color: white; "
        "  padding: 2px 8px; "
        "  border-radius: 12px; "
        "  font-size: 12px; "
        "  font-weight: 600; "
        "}"
        ".track-info { "
        "  color: #666; "
        "  font-size: 14px; "
        "}"
        ".volume-bar { "
        "  background: #e0e0e0; "
        "  height: 6px; "
        "  border-radius: 3px; "
        "  margin-top: 8px; "
        "  position: relative; "
        "}"
        ".volume-fill { "
        "  background: #667eea; "
        "  height: 100%; "
        "  border-radius: 3px; "
        "  transition: width 0.3s ease; "
        "}"
        ".loading { "
        "  text-align: center; "
        "  color: #999; "
        "  padding: 20px; "
        "}"
        ".error { "
        "  background: #ffebee; "
        "  color: #c62828; "
        "  padding: 12px; "
        "  border-radius: 8px; "
        "  margin: 10px 0; "
        "}"
        ".refresh-btn { "
        "  background: #667eea; "
        "  color: white; "
        "  border: none; "
        "  padding: 10px 20px; "
        "  border-radius: 8px; "
        "  font-size: 14px; "
        "  font-weight: 600; "
        "  cursor: pointer; "
        "  display: block; "
        "  margin: 20px auto; "
        "}"
        ".refresh-btn:hover { background: #5a67d8; }"
        "@media (max-width: 480px) { "
        "  h1 { font-size: 20px; } "
        "  .card { padding: 15px; } "
        "}"
        "</style>"
        "</head>"
        "<body>"
        "<div class='container'>"
        "<h1>Loudframe Controller</h1>"
        
        "<div class='menu-bar'>"
        "<a href='/' class='menu-btn active'>Status</a>"
        "<a href='/settings' class='menu-btn'>Settings</a>"
        "<a href='/api-docs' class='menu-btn'>API Docs</a>"
        "</div>"
        
        "<div class='card'>"
        "<h2>Unit Status</h2>"
        "<div id='status-content'>"
        "<div class='loading'>Loading status...</div>"
        "</div>"
        "</div>"
        
        "<div class='card'>"
        "<h2>Loop Tracks</h2>"
        "<div id='loops-content'>"
        "<div class='loading'>Loading loops...</div>"
        "</div>"
        "</div>"
        
        "<div class='card'>"
        "<h2>Configuration</h2>"
        "<div style='text-align: center; padding: 10px;'>"
        "<button class='menu-btn' style='background: #667eea; color: white; padding: 12px 24px; font-size: 16px;' "
        "onclick=\"window.location.href='/settings'\">Configure Device ID</button>"
        "<p style='margin-top: 10px; color: #666; font-size: 14px;'>Click to set or change the device ID</p>"
        "</div>"
        "</div>"
        
        "<button class='refresh-btn' onclick='refreshData()'>Refresh</button>"
        "</div>"
        
        "<script>"
        "console.log('[DEBUG] 1. Script tag started executing');"
        "try {"
        "  console.log('[DEBUG] 2. About to define fetchStatus function');"
        "  "
        "  function fetchStatus() {"
        "  console.log('[DEBUG] fetchStatus() called at', new Date().toISOString());"
        "  console.log('[DEBUG] About to fetch /api/status');"
        "  "
        "  fetch('/api/status')"
        "    .then(function(response) {"
        "      console.log('[DEBUG] Status response received:', response.status, response.statusText);"
        "      console.log('[DEBUG] Response headers:', response.headers);"
        "      if (!response.ok) {"
        "        throw new Error('HTTP error! status: ' + response.status);"
        "      }"
        "      return response.json();"
        "    })"
        "    .then(function(data) {"
        "      console.log('[DEBUG] Status data parsed:', data);"
        "      var container = document.getElementById('status-content');"
        "      console.log('[DEBUG] Status container element:', container);"
        "      "
        "      if (container) {"
        "        console.log('[DEBUG] Building status HTML');"
        "        var html = '';"
        "        html += '<div class=\"status-item\">';"
        "        html += '<span class=\"label\">ID</span>';"
        "        html += '<span class=\"value\">' + (data.id || 'Not Set') + '</span>';"
        "        html += '</div>';"
        "        html += '<div class=\"status-item\">';"
        "        html += '<span class=\"label\">IP Address</span>';"
        "        html += '<span class=\"value\">' + (data.ip_address || 'N/A') + '</span>';"
        "        html += '</div>';"
        "        html += '<div class=\"status-item\">';"
        "        html += '<span class=\"label\">MAC Address</span>';"
        "        html += '<span class=\"value\">' + (data.mac_address || 'N/A') + '</span>';"
        "        html += '</div>';"
        "        html += '<div class=\"status-item\">';"
        "        html += '<span class=\"label\">WiFi Status</span>';"
        "        html += '<span class=\"value\">' + (data.wifi_connected ? 'Connected' : 'Disconnected') + '</span>';"
        "        html += '</div>';"
        "        html += '<div class=\"status-item\">';"
        "        html += '<span class=\"label\">Firmware</span>';"
        "        html += '<span class=\"value\">' + (data.firmware_version || 'Unknown') + '</span>';"
        "        html += '</div>';"
        "        html += '<div class=\"status-item\">';"
        "        html += '<span class=\"label\">Uptime</span>';"
        "        html += '<span class=\"value\">' + (data.uptime_formatted || 'N/A') + '</span>';"
        "        html += '</div>';"
        "        console.log('[DEBUG] Setting innerHTML for status');"
        "        container.innerHTML = html;"
        "        console.log('[DEBUG] Status HTML updated successfully');"
        "      } else {"
        "        console.error('[DEBUG] ERROR: status-content element not found!');"
        "      }"
        "    })"
        "    .catch(function(error) {"
        "      console.error('[DEBUG] Status fetch error:', error);"
        "      console.error('[DEBUG] Error stack:', error.stack);"
        "      var container = document.getElementById('status-content');"
        "      if (container) {"
        "        container.innerHTML = '<div class=\"error\">Failed to load status: ' + error.message + '</div>';"
        "      }"
        "    });"
        "}"
        ""
        "function fetchLoops() {"
        "  console.log('[DEBUG] fetchLoops() called at', new Date().toISOString());"
        "  console.log('[DEBUG] About to fetch /api/loops');"
        "  "
        "  fetch('/api/loops')"
        "    .then(function(response) {"
        "      console.log('[DEBUG] Loops response received:', response.status, response.statusText);"
        "      if (!response.ok) {"
        "        throw new Error('HTTP error! status: ' + response.status);"
        "      }"
        "      return response.json();"
        "    })"
        "    .then(function(data) {"
        "      console.log('[DEBUG] Loops data parsed:', data);"
        "      var container = document.getElementById('loops-content');"
        "      console.log('[DEBUG] Loops container element:', container);"
        "      "
        "      if (!container) {"
        "        console.error('[DEBUG] ERROR: loops-content element not found!');"
        "        return;"
        "      }"
        "      "
        "      if (!data.loops || data.loops.length === 0) {"
        "        console.log('[DEBUG] No loops data available');"
        "        container.innerHTML = '<div class=\"error\">No loops data available</div>';"
        "        return;"
        "      }"
        "      "
        "      console.log('[DEBUG] Building loops HTML for', data.loops.length, 'tracks');"
        "      var html = '<div class=\"status-item\">';"
        "      html += '<span class=\"label\">Global Volume</span>';"
        "      html += '<span class=\"value\">' + data.global_volume + '%</span>';"
        "      html += '</div>';"
        "      "
        "      data.loops.forEach(function(loop, index) {"
        "        console.log('[DEBUG] Processing loop', index, ':', loop);"
        "        var fileName = loop.file ? loop.file.split('/').pop() : 'No file';"
        "        html += '<div class=\"track\">';"
        "        html += '<div class=\"track-header\">';"
        "        html += '<span class=\"track-title\">Track ' + (loop.track + 1) + '</span>';"
        "        html += '<span class=\"' + (loop.playing ? 'playing-badge' : 'stopped-badge') + '\">';"
        "        html += (loop.playing ? 'PLAYING' : 'STOPPED');"
        "        html += '</span>';"
        "        html += '</div>';"
        "        html += '<div class=\"track-info\">';"
        "        html += '<div>File: ' + fileName + '</div>';"
        "        html += '<div>Volume: ' + loop.volume + '%</div>';"
        "        html += '</div>';"
        "        html += '<div class=\"volume-bar\">';"
        "        html += '<div class=\"volume-fill\" style=\"width: ' + loop.volume + '%\"></div>';"
        "        html += '</div>';"
        "        html += '</div>';"
        "      });"
        "      "
        "      console.log('[DEBUG] Setting innerHTML for loops');"
        "      container.innerHTML = html;"
        "      console.log('[DEBUG] Loops HTML updated successfully');"
        "    })"
        "    .catch(function(error) {"
        "      console.error('[DEBUG] Loops fetch error:', error);"
        "      console.error('[DEBUG] Error stack:', error.stack);"
        "      var container = document.getElementById('loops-content');"
        "      if (container) {"
        "        container.innerHTML = '<div class=\"error\">Failed to load loops: ' + error.message + '</div>';"
        "      }"
        "    });"
        "}"
        ""
        "function refreshData() {"
        "  console.log('[DEBUG] refreshData() called at', new Date().toISOString());"
        "  console.log('[DEBUG] Document readyState:', document.readyState);"
        "  console.log('[DEBUG] status-content element exists?', !!document.getElementById('status-content'));"
        "  console.log('[DEBUG] loops-content element exists?', !!document.getElementById('loops-content'));"
        "  fetchStatus();"
        "  fetchLoops();"
        "}"
        ""
        "  console.log('[DEBUG] 3. fetchStatus function defined');"
        "  console.log('[DEBUG] 4. About to define fetchLoops function');"
        "  "
        "  console.log('[DEBUG] 5. fetchLoops function defined');"
        "  console.log('[DEBUG] 6. About to define refreshData function');"
        "  "
        "  console.log('[DEBUG] 7. refreshData function defined');"
        "  "
        "  console.log('[DEBUG] 8. About to call refreshData for initial load');"
        "  console.log('[DEBUG] 9. Document readyState:', document.readyState);"
        "  "
        "  refreshData();"
        "  "
        "  console.log('[DEBUG] 10. Initial refreshData() called');"
        "  "
        "  var intervalId = setInterval(refreshData, 5000);"
        "  console.log('[DEBUG] 11. Auto-refresh interval set with ID:', intervalId);"
        "  "
        "  console.log('[DEBUG] 12. Script finished executing');"
        "} catch (error) {"
        "  console.error('[DEBUG] ERROR in script execution:', error);"
        "  console.error('[DEBUG] Error message:', error.message);"
        "  console.error('[DEBUG] Error stack:', error.stack);"
        "}"
        "</script>"
        "</body>"
        "</html>";
    
    // Check response size for potential truncation
    size_t html_size = strlen(html);
    ESP_LOGD(TAG, "Root page HTML size: %d bytes", html_size);
    
    // ESP32 HTTP server typically has a limit around 16KB for single response
    const size_t MAX_RESPONSE_SIZE = 16384;  // 16KB typical limit
    const size_t WARNING_THRESHOLD = 14336;  // 14KB warning threshold (87.5% of max)
    
    if (html_size >= MAX_RESPONSE_SIZE) {
        ESP_LOGE(TAG, "WARNING: Root page HTML exceeds maximum response size!");
        ESP_LOGE(TAG, "HTML size: %d bytes, Max size: %d bytes", html_size, MAX_RESPONSE_SIZE);
        ESP_LOGE(TAG, "Response will likely be truncated!");
    } else if (html_size >= WARNING_THRESHOLD) {
        ESP_LOGW(TAG, "Root page HTML approaching maximum response size");
        ESP_LOGW(TAG, "HTML size: %d bytes, Warning at: %d bytes, Max: %d bytes", 
                 html_size, WARNING_THRESHOLD, MAX_RESPONSE_SIZE);
    }
    
    httpd_resp_set_type(req, "text/html");
    esp_err_t ret = httpd_resp_send(req, html, html_size);
    
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to send root page response: %s", esp_err_to_name(ret));
    } else {
        ESP_LOGD(TAG, "Root page sent successfully (%d bytes)", html_size);
    }
    
    return ret;
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
    config.max_uri_handlers = 27;  // Increased to handle all handlers including reboot endpoint
    config.recv_wait_timeout = 10;
    config.send_wait_timeout = 10;
    
    ESP_LOGI(TAG, "Starting HTTP server on port %d", config.server_port);
    
    if (httpd_start(&server, &config) != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start HTTP server");
        return ESP_FAIL;
    }
    
    // Register URI handlers with error checking
    esp_err_t ret;
    
    httpd_uri_t root_uri = {
        .uri = "/",
        .method = HTTP_GET,
        .handler = root_get_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &root_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t settings_uri = {
        .uri = "/settings",
        .method = HTTP_GET,
        .handler = settings_get_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &settings_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /settings: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t favicon_uri = {
        .uri = "/favicon.ico",
        .method = HTTP_GET,
        .handler = favicon_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &favicon_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /favicon.ico: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t api_docs_uri = {
        .uri = "/api-docs",
        .method = HTTP_GET,
        .handler = api_docs_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &api_docs_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api-docs: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t files_uri = {
        .uri = "/api/files",
        .method = HTTP_GET,
        .handler = files_get_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &files_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/files: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t loops_uri = {
        .uri = "/api/loops",
        .method = HTTP_GET,
        .handler = loops_get_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &loops_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/loops: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t file_uri = {
        .uri = "/api/loop/file",
        .method = HTTP_POST,
        .handler = loop_file_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &file_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/loop/file: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t start_uri = {
        .uri = "/api/loop/start",
        .method = HTTP_POST,
        .handler = loop_start_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &start_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/loop/start: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t stop_uri = {
        .uri = "/api/loop/stop",
        .method = HTTP_POST,
        .handler = loop_stop_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &stop_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/loop/stop: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t volume_uri = {
        .uri = "/api/loop/volume",
        .method = HTTP_POST,
        .handler = loop_volume_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &volume_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/loop/volume: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t global_volume_uri = {
        .uri = "/api/global/volume",
        .method = HTTP_POST,
        .handler = global_volume_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &global_volume_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/global/volume: %s", esp_err_to_name(ret));
    }
    
    // Register WiFi management endpoints
    httpd_uri_t wifi_status_uri = {
        .uri = "/api/wifi/status",
        .method = HTTP_GET,
        .handler = wifi_status_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &wifi_status_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/wifi/status: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t wifi_networks_uri = {
        .uri = "/api/wifi/networks",
        .method = HTTP_GET,
        .handler = wifi_networks_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &wifi_networks_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/wifi/networks: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t wifi_add_uri = {
        .uri = "/api/wifi/add",
        .method = HTTP_POST,
        .handler = wifi_add_network_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &wifi_add_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/wifi/add: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t wifi_remove_uri = {
        .uri = "/api/wifi/remove",
        .method = HTTP_POST,
        .handler = wifi_remove_network_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &wifi_remove_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/wifi/remove: %s", esp_err_to_name(ret));
    }
    
    // Register configuration management endpoints
    httpd_uri_t config_status_uri = {
        .uri = "/api/config/status",
        .method = HTTP_GET,
        .handler = config_status_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &config_status_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/config/status: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t config_save_uri = {
        .uri = "/api/config/save",
        .method = HTTP_POST,
        .handler = config_save_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &config_save_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/config/save: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t config_load_uri = {
        .uri = "/api/config/load",
        .method = HTTP_POST,
        .handler = config_load_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &config_load_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/config/load: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t config_delete_uri = {
        .uri = "/api/config/delete",
        .method = HTTP_DELETE,
        .handler = config_delete_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &config_delete_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/config/delete: %s", esp_err_to_name(ret));
    }
    
    // Register unit status endpoints
    httpd_uri_t unit_status_uri = {
        .uri = "/api/status",
        .method = HTTP_GET,
        .handler = unit_status_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &unit_status_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/status: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t id_get_uri = {
        .uri = "/api/id",
        .method = HTTP_GET,
        .handler = id_get_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &id_get_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for GET /api/id: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t id_set_uri = {
        .uri = "/api/id",
        .method = HTTP_POST,
        .handler = id_set_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &id_set_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for POST /api/id: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t upload_uri = {
        .uri = "/api/upload",
        .method = HTTP_POST,
        .handler = file_upload_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &upload_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/upload: %s", esp_err_to_name(ret));
    }
    
    httpd_uri_t file_delete_uri = {
        .uri = "/api/file/delete",
        .method = HTTP_DELETE,
        .handler = file_delete_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &file_delete_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/file/delete: %s", esp_err_to_name(ret));
    }
    
    // Register system reboot endpoint
    httpd_uri_t system_reboot_uri = {
        .uri = "/api/system/reboot",
        .method = HTTP_POST,
        .handler = system_reboot_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &system_reboot_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/system/reboot: %s", esp_err_to_name(ret));
    }
    
    // Initialize unit status manager
    unit_status_init();
    
    ESP_LOGI(TAG, "HTTP server started successfully");
    ESP_LOGI(TAG, "API available at http://<device-ip>/");
    ESP_LOGI(TAG, "WiFi management available at /api/wifi/*");
    ESP_LOGI(TAG, "Configuration management available at /api/config/*");
    
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
