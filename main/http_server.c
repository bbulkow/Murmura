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

static const char *TAG = "HTTP_SERVER";

// Global variables
static httpd_handle_t server = NULL;
static loop_manager_t *g_loop_manager = NULL;

// Forward declarations
static esp_err_t files_get_handler(httpd_req_t *req);
static esp_err_t loops_get_handler(httpd_req_t *req);
static esp_err_t loop_start_handler(httpd_req_t *req);
static esp_err_t loop_stop_handler(httpd_req_t *req);
static esp_err_t loop_gain_handler(httpd_req_t *req);
static esp_err_t root_get_handler(httpd_req_t *req);

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
    char *buf = malloc(req->content_len + 1);
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
        for (int i = 0; i < MAX_TRACKS; i++) {
            if (g_loop_manager->loops[i].is_playing) {
                cJSON *loop_obj = cJSON_CreateObject();
                cJSON_AddNumberToObject(loop_obj, "track", i);
                cJSON_AddStringToObject(loop_obj, "file", g_loop_manager->loops[i].file_path);
                cJSON_AddNumberToObject(loop_obj, "gain_db", g_loop_manager->loops[i].gain_db);
                cJSON_AddBoolToObject(loop_obj, "playing", true);
                cJSON_AddItemToArray(loops_array, loop_obj);
            }
        }
    }
    
    cJSON_AddItemToObject(response, "loops", loops_array);
    cJSON_AddNumberToObject(response, "active_count", cJSON_GetArraySize(loops_array));
    cJSON_AddNumberToObject(response, "max_tracks", MAX_TRACKS);
    
    esp_err_t ret = send_json_response(req, response);
    cJSON_Delete(response);
    
    return ret;
}

/**
 * @brief POST /api/loop/start - Start a loop on a specific track
 * Body: { "track": 0, "file_index": 0 } or { "track": 0, "file_path": "/sdcard/track1.wav" }
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
            // Update loop manager state
            g_loop_manager->loops[track].is_playing = true;
            strncpy(g_loop_manager->loops[track].file_path, file_path, sizeof(g_loop_manager->loops[track].file_path) - 1);
            g_loop_manager->loops[track].track_index = track;
            
            cJSON_AddBoolToObject(response, "success", true);
            cJSON_AddNumberToObject(response, "track", track);
            cJSON_AddStringToObject(response, "file", file_path);
            cJSON_AddStringToObject(response, "message", "Loop start command sent");
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
            // Update loop manager state
            g_loop_manager->loops[track].is_playing = false;
            memset(g_loop_manager->loops[track].file_path, 0, sizeof(g_loop_manager->loops[track].file_path));
            
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
 * @brief POST /api/loop/gain - Set gain for a specific loop
 * Body: { "track": 0, "gain_db": -6.0 }
 */
static esp_err_t loop_gain_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "POST /api/loop/gain");
    
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
    
    // Get gain value
    cJSON *gain_json = cJSON_GetObjectItem(request, "gain_db");
    if (!cJSON_IsNumber(gain_json)) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Missing or invalid gain value");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }
    
    float gain_db = (float)gain_json->valuedouble;
    
    // Clamp gain to reasonable range
    if (gain_db < -60.0f) gain_db = -60.0f;
    if (gain_db > 12.0f) gain_db = 12.0f;
    
    // Send message to audio control task to set the gain
    if (g_loop_manager && g_loop_manager->audio_control_queue) {
        audio_control_msg_t control_msg;
        control_msg.type = AUDIO_ACTION_SET_GAIN;
        control_msg.data.set_gain.track_index = track;
        control_msg.data.set_gain.gain_db = gain_db;
        
        // Send message with timeout
        if (xQueueSend(g_loop_manager->audio_control_queue, &control_msg, pdMS_TO_TICKS(100)) == pdPASS) {
            // Update loop manager state
            g_loop_manager->loops[track].gain_db = gain_db;
            
            cJSON_AddBoolToObject(response, "success", true);
            cJSON_AddNumberToObject(response, "track", track);
            cJSON_AddNumberToObject(response, "gain_db", gain_db);
            cJSON_AddStringToObject(response, "message", "Gain adjustment command sent");
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
        "    { \"track\": 0, \"file\": \"/sdcard/track1.wav\", \"gain_db\": 0.0, \"playing\": true }\n"
        "  ],\n"
        "  \"active_count\": 1,\n"
        "  \"max_tracks\": 3\n"
        "}</pre>"
        "</div>"
        
        "<div class='endpoint'>"
        "<span class='method'>POST</span> <span class='path'>/api/loop/start</span>"
        "<p>Start a loop on a specific track</p>"
        "<pre>Request:\n"
        "{\n"
        "  \"track\": 0,\n"
        "  \"file_index\": 0  // OR \"file_path\": \"/sdcard/track1.wav\"\n"
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
        "<span class='method'>POST</span> <span class='path'>/api/loop/gain</span>"
        "<p>Set gain for a specific loop (range: -60 to +12 dB)</p>"
        "<pre>Request:\n"
        "{\n"
        "  \"track\": 0,\n"
        "  \"gain_db\": -6.0\n"
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
    
    // Allocate and initialize loop manager
    g_loop_manager = calloc(1, sizeof(loop_manager_t));
    if (!g_loop_manager) {
        ESP_LOGE(TAG, "Failed to allocate loop manager");
        return ESP_FAIL;
    }
    
    g_loop_manager->audio_stream = audio_stream;
    g_loop_manager->audio_control_queue = audio_control_queue;
    
    // Initialize loop states
    for (int i = 0; i < MAX_TRACKS; i++) {
        g_loop_manager->loops[i].is_playing = false;
        g_loop_manager->loops[i].gain_db = 0.0f;
        g_loop_manager->loops[i].track_index = i;
        memset(g_loop_manager->loops[i].file_path, 0, sizeof(g_loop_manager->loops[i].file_path));
    }
    
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = HTTP_SERVER_PORT;
    config.stack_size = 8192;
    config.max_uri_handlers = 10;
    config.recv_wait_timeout = 10;
    config.send_wait_timeout = 10;
    
    ESP_LOGI(TAG, "Starting HTTP server on port %d", config.server_port);
    
    if (httpd_start(&server, &config) != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start HTTP server");
        free(g_loop_manager);
        g_loop_manager = NULL;
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
    
    httpd_uri_t gain_uri = {
        .uri = "/api/loop/gain",
        .method = HTTP_POST,
        .handler = loop_gain_handler,
        .user_ctx = NULL
    };
    httpd_register_uri_handler(server, &gain_uri);
    
    ESP_LOGI(TAG, "HTTP server started successfully");
    ESP_LOGI(TAG, "API available at http://<device-ip>/");
    
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
    
    if (g_loop_manager) {
        free(g_loop_manager);
        g_loop_manager = NULL;
    }
    
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
