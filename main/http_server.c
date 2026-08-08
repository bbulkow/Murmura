#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdint.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_http_server.h"
#include "cJSON.h"
#include "http_server.h"
#include "music_files.h"
#include "murmura.h"
#include "wifi_manager.h"
#include "esp_wifi.h"
#include "config_manager.h"
#include "scene_manager.h"
#include "unit_status_manager.h"
#include "mur_listener.h"
#include <sys/stat.h>
#include <unistd.h>
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"

static const char *TAG = "HTTP_SERVER";

// Global variables
static httpd_handle_t server = NULL;
static track_manager_t *g_track_manager = NULL;
static scene_manager_t *g_scene_manager = NULL;

// Shared scratch buffers for HTTP handlers (SPIRAM, allocated once at init).
// Safe to reuse: ESP-IDF HTTP server processes one handler at a time.
#define SCRATCH_PATH_LEN   256
#define SCRATCH_QUERY_LEN  256
#define SCRATCH_NAME_LEN   128
#define SCRATCH_PARAM_LEN  128
static char *scratch_path  = NULL;   // 256 bytes — file paths
static char *scratch_query = NULL;   // 256 bytes — URL query strings
static char *scratch_name  = NULL;   // 128 bytes — filenames
static char *scratch_param = NULL;   // 128 bytes — query params

// Custom cJSON memory hooks for SPIRAM usage
static void* cjson_malloc_spiram(size_t size) {
    void *ptr = heap_caps_malloc(size, MALLOC_CAP_SPIRAM);
    if (ptr == NULL) {
        // Fallback to default if SPIRAM allocation fails
        ptr = malloc(size);
    }
    return ptr;
}

static void cjson_free_spiram(void *ptr) {
    free(ptr);
}

// Initialize cJSON to use SPIRAM
static void init_cjson_spiram(void) {
    static cJSON_Hooks hooks = {
        .malloc_fn = cjson_malloc_spiram,
        .free_fn = cjson_free_spiram
    };
    cJSON_InitHooks(&hooks);
}

// Forward declarations
static esp_err_t files_get_handler(httpd_req_t *req);
static esp_err_t scenes_get_handler(httpd_req_t *req);
static esp_err_t scenes_post_handler(httpd_req_t *req);
static esp_err_t scene_action_handler(httpd_req_t *req);
static esp_err_t root_get_handler(httpd_req_t *req);
// WiFi management handlers
static esp_err_t wifi_add_network_handler(httpd_req_t *req);
static esp_err_t wifi_remove_network_handler(httpd_req_t *req);
// Configuration management handlers
static esp_err_t config_save_handler(httpd_req_t *req);
static esp_err_t config_load_handler(httpd_req_t *req);
static esp_err_t config_delete_handler(httpd_req_t *req);
static esp_err_t config_status_handler(httpd_req_t *req);
// Consolidated device configuration handlers
static esp_err_t device_get_handler(httpd_req_t *req);
static esp_err_t device_post_handler(httpd_req_t *req);
static esp_err_t file_upload_handler(httpd_req_t *req);
static esp_err_t file_delete_handler(httpd_req_t *req);
static esp_err_t file_download_handler(httpd_req_t *req);
static esp_err_t system_reboot_handler(httpd_req_t *req);

/**
 * @brief Send JSON response (uses SPIRAM via cJSON hooks)
 */
static esp_err_t send_json_response(httpd_req_t *req, cJSON *json) {
    // Get formatted JSON string - this allocates from SPIRAM via our custom hooks
    char *json_str = cJSON_Print(json);
    if (json_str == NULL) {
        ESP_LOGE(TAG, "cJSON_Print failed");
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
 * Uses SPIRAM optimization to avoid DMA memory exhaustion
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
            snprintf(scratch_path, SCRATCH_PATH_LEN, "/sdcard/%s", music_files[i]);
            cJSON_AddStringToObject(file_obj, "path", scratch_path);
            
            // Get file size
            struct stat file_stat;
            if (stat(scratch_path, &file_stat) == 0) {
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
 * @brief GET /api/scenes - Return all scene configurations
 */
static esp_err_t scenes_get_handler(httpd_req_t *req) {
    ESP_LOGD(TAG, "GET /api/scenes");

    if (!g_scene_manager) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Scene manager not initialized");
        return ESP_FAIL;
    }

    cJSON *response = scene_build_get_response(g_scene_manager, g_track_manager);
    if (!response) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Failed to build response");
        return ESP_FAIL;
    }

    esp_err_t ret = send_json_response(req, response);
    cJSON_Delete(response);
    return ret;
}

/**
 * @brief POST /api/scenes - Patch-style update to scene configurations.
 * Body keys are scene names, values are partial scene configs.
 * Atomic: validates all changes before applying any.
 */
static esp_err_t scenes_post_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "POST /api/scenes");

    if (!g_scene_manager || !g_track_manager || !g_track_manager->audio_control_queue) {
        cJSON *err = cJSON_CreateObject();
        cJSON_AddBoolToObject(err, "success", false);
        cJSON_AddStringToObject(err, "error", "Scene/audio system not initialized");
        send_json_response(req, err);
        cJSON_Delete(err);
        return ESP_OK;
    }

    if (req->content_len == 0) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Empty request body");
        return ESP_FAIL;
    }

    cJSON *request = parse_json_request(req);
    if (!request) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Invalid JSON");
        return ESP_FAIL;
    }

    // Pass 1: Validate all scene patches atomically
    char error_msg[128] = {0};
    esp_err_t val_ret = scene_validate_patch(g_scene_manager, request, error_msg, sizeof(error_msg));
    if (val_ret != ESP_OK) {
        cJSON *response = cJSON_CreateObject();
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", error_msg[0] ? error_msg : "Validation failed");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }

    // Pass 2: Apply all changes
    esp_err_t apply_ret = scene_apply_patch(g_scene_manager, request,
                                             g_track_manager->audio_control_queue, g_track_manager);

    if (apply_ret == ESP_OK) {
        mur_listener_resubscribe();
    }

    cJSON *response = cJSON_CreateObject();
    if (apply_ret == ESP_OK) {
        cJSON_AddBoolToObject(response, "success", true);
        cJSON_AddStringToObject(response, "message", "Scenes updated");
    } else {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Failed to apply changes");
    }

    esp_err_t ret = send_json_response(req, response);
    cJSON_Delete(response);
    cJSON_Delete(request);
    return ret;
}

/**
 * @brief POST /api/scene - Scene management actions (create, delete, activate, set_default)
 * Body: { "action": "create|delete|activate|set_default", "name": "scene_name", ... }
 */
static esp_err_t scene_action_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "POST /api/scene");

    if (!g_scene_manager) {
        cJSON *err = cJSON_CreateObject();
        cJSON_AddBoolToObject(err, "success", false);
        cJSON_AddStringToObject(err, "error", "Scene manager not initialized");
        send_json_response(req, err);
        cJSON_Delete(err);
        return ESP_OK;
    }

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

    cJSON *action_json = cJSON_GetObjectItem(request, "action");
    cJSON *name_json = cJSON_GetObjectItem(request, "name");

    if (!cJSON_IsString(action_json) || !action_json->valuestring[0]) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Missing or invalid 'action' field");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }

    const char *action = action_json->valuestring;
    const char *name = (cJSON_IsString(name_json) && name_json->valuestring) ? name_json->valuestring : "";

    if (strcmp(action, "create") == 0) {
        if (!scene_name_valid(name)) {
            cJSON_AddBoolToObject(response, "success", false);
            cJSON_AddStringToObject(response, "error", "Invalid scene name (1-31 chars, alphanumeric/hyphen/underscore)");
        } else {
            esp_err_t ret = scene_create(g_scene_manager, name);
            if (ret == ESP_OK) {
                // Optionally apply initial config from the request body
                cJSON *gv = cJSON_GetObjectItem(request, "global_volume");
                cJSON *tracks = cJSON_GetObjectItem(request, "tracks");
                if (gv || tracks) {
                    // Build a mini-patch and apply it (no hardware effect since it's not active)
                    cJSON *patch = cJSON_CreateObject();
                    cJSON *scene_data = cJSON_CreateObject();
                    if (gv) cJSON_AddNumberToObject(scene_data, "global_volume", gv->valueint);
                    if (tracks) cJSON_AddItemReferenceToObject(scene_data, "tracks", tracks);
                    cJSON_AddItemToObject(patch, name, scene_data);
                    scene_apply_patch(g_scene_manager, patch, NULL, NULL);
                    cJSON_Delete(patch);
                }
                cJSON_AddBoolToObject(response, "success", true);
                char msg[96];
                snprintf(msg, sizeof(msg), "Scene '%s' created", name);
                cJSON_AddStringToObject(response, "message", msg);
            } else if (ret == ESP_ERR_INVALID_STATE) {
                cJSON_AddBoolToObject(response, "success", false);
                cJSON_AddStringToObject(response, "error", "Scene already exists");
            } else if (ret == ESP_ERR_NO_MEM) {
                cJSON_AddBoolToObject(response, "success", false);
                cJSON_AddStringToObject(response, "error", "Maximum number of scenes reached");
            } else {
                cJSON_AddBoolToObject(response, "success", false);
                cJSON_AddStringToObject(response, "error", "Failed to create scene");
            }
        }
    } else if (strcmp(action, "delete") == 0) {
        esp_err_t ret = scene_delete(g_scene_manager, name);
        if (ret == ESP_OK) {
            cJSON_AddBoolToObject(response, "success", true);
            char msg[96];
            snprintf(msg, sizeof(msg), "Scene '%s' deleted", name);
            cJSON_AddStringToObject(response, "message", msg);
        } else if (ret == ESP_ERR_INVALID_STATE) {
            cJSON_AddBoolToObject(response, "success", false);
            cJSON_AddStringToObject(response, "error", "Cannot delete the active scene");
        } else if (ret == ESP_ERR_NOT_FOUND) {
            cJSON_AddBoolToObject(response, "success", false);
            cJSON_AddStringToObject(response, "error", "Scene not found");
        } else {
            cJSON_AddBoolToObject(response, "success", false);
            cJSON_AddStringToObject(response, "error", "Failed to delete scene");
        }
    } else if (strcmp(action, "activate") == 0) {
        if (!g_track_manager || !g_track_manager->audio_control_queue) {
            cJSON_AddBoolToObject(response, "success", false);
            cJSON_AddStringToObject(response, "error", "Audio system not initialized");
        } else {
            esp_err_t ret = scene_activate(g_scene_manager, name,
                                            g_track_manager->audio_control_queue, g_track_manager,
                                            SCENE_ACTIVATE_DIRECT);
            if (ret == ESP_OK) {
                mur_listener_resubscribe();
                cJSON_AddBoolToObject(response, "success", true);
                char msg[96];
                snprintf(msg, sizeof(msg), "Scene '%s' activated", name);
                cJSON_AddStringToObject(response, "message", msg);
            } else if (ret == ESP_ERR_NOT_FOUND) {
                cJSON_AddBoolToObject(response, "success", false);
                cJSON_AddStringToObject(response, "error", "Scene not found");
            } else if (ret == ESP_ERR_INVALID_STATE) {
                /* Synchronized scene refused — see SYNC_DESIGN.md. */
                cJSON_AddBoolToObject(response, "success", false);
                cJSON_AddStringToObject(response, "error",
                    "Scene is synchronized; activate via SceneChange trigger");
                httpd_resp_set_status(req, "409 Conflict");
            } else {
                cJSON_AddBoolToObject(response, "success", false);
                cJSON_AddStringToObject(response, "error", "Failed to activate scene");
            }
        }
    } else if (strcmp(action, "set_default") == 0) {
        if (name[0] != '\0' && !scene_find(g_scene_manager, name)) {
            cJSON_AddBoolToObject(response, "success", false);
            cJSON_AddStringToObject(response, "error", "Scene not found");
        } else {
            strncpy(g_scene_manager->default_scene, name, MAX_SCENE_NAME_LEN - 1);
            g_scene_manager->default_scene[MAX_SCENE_NAME_LEN - 1] = '\0';
            cJSON_AddBoolToObject(response, "success", true);
            cJSON_AddStringToObject(response, "default_scene", g_scene_manager->default_scene);
        }
    } else {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Unknown action (use: create, delete, activate, set_default)");
    }

    esp_err_t ret = send_json_response(req, response);
    cJSON_Delete(response);
    cJSON_Delete(request);
    return ret;
}

/**
 * @brief GET /api/device - Consolidated device configuration and status
 * Returns device identity, network status, mur gateway config, and wifi info
 */
static esp_err_t device_get_handler(httpd_req_t *req) {
    ESP_LOGD(TAG, "GET /api/device");

    cJSON *response = cJSON_CreateObject();

    // --- Device identity and status (from unit_status_manager) ---
    unit_status_t status;
    esp_err_t ret = unit_status_get(&status);
    if (ret == ESP_OK) {
        cJSON_AddStringToObject(response, "id", status.id);
        cJSON_AddStringToObject(response, "mac_address", status.mac_address);
        cJSON_AddStringToObject(response, "ip_address", status.ip_address);
        cJSON_AddStringToObject(response, "firmware_version", status.firmware_version);
        cJSON_AddNumberToObject(response, "uptime_seconds", status.uptime_seconds);
    }

    // --- Mur Gateway config + device volume ---
    if (g_track_manager) {
        cJSON_AddStringToObject(response, "mur_gateway_ip", g_track_manager->mur_gateway_ip);
        cJSON_AddNumberToObject(response, "mur_gateway_port", g_track_manager->mur_gateway_port);
        cJSON_AddStringToObject(response, "scene_trigger_name", g_track_manager->scene_trigger_name);
        cJSON_AddNumberToObject(response, "device_volume", g_track_manager->device_volume_percent);
        cJSON_AddStringToObject(response, "late_policy",
                                config_late_policy_to_str(g_track_manager->late_policy));
        cJSON_AddNumberToObject(response, "playback_offset_us",
                                g_track_manager->playback_offset_us);
    } else {
        cJSON_AddStringToObject(response, "mur_gateway_ip", "");
        cJSON_AddNumberToObject(response, "mur_gateway_port", MUR_GATEWAY_DEFAULT_PORT);
        cJSON_AddStringToObject(response, "scene_trigger_name", "");
        cJSON_AddNumberToObject(response, "device_volume", 100);
        cJSON_AddStringToObject(response, "late_policy", "play");
        cJSON_AddNumberToObject(response, "playback_offset_us", 0);
    }

    // --- WiFi status and networks ---
    cJSON *wifi = cJSON_CreateObject();

    bool is_connected = wifi_manager_is_connected();
    cJSON_AddBoolToObject(wifi, "connected", is_connected);

    if (is_connected) {
        char ssid[33] = {0};
        wifi_manager_get_connected_ssid(ssid, sizeof(ssid));
        cJSON_AddStringToObject(wifi, "ssid", ssid);

        wifi_ap_record_t ap_info;
        if (esp_wifi_sta_get_ap_info(&ap_info) == ESP_OK) {
            cJSON_AddNumberToObject(wifi, "rssi", ap_info.rssi);

            int signal_percent = 0;
            if (ap_info.rssi >= -50) signal_percent = 100;
            else if (ap_info.rssi >= -60) signal_percent = 90;
            else if (ap_info.rssi >= -67) signal_percent = 75;
            else if (ap_info.rssi >= -70) signal_percent = 60;
            else if (ap_info.rssi >= -80) signal_percent = 40;
            else if (ap_info.rssi >= -90) signal_percent = 20;
            else signal_percent = 10;
            cJSON_AddNumberToObject(wifi, "signal_strength", signal_percent);
        }
    }

    // WiFi networks list
    cJSON *networks_array = cJSON_CreateArray();
    wifiman_network_entry_t networks[WIFI_MAX_NETWORKS];
    size_t count = 0;
    ret = wifi_manager_get_stored_networks(networks, WIFI_MAX_NETWORKS, &count);
    if (ret == ESP_OK) {
        for (size_t i = 0; i < count; i++) {
            cJSON *net = cJSON_CreateObject();
            cJSON_AddNumberToObject(net, "index", i);
            cJSON_AddStringToObject(net, "ssid", networks[i].ssid);
            cJSON_AddBoolToObject(net, "has_password", strlen(networks[i].password) > 0);
            cJSON_AddBoolToObject(net, "available", networks[i].available);
            cJSON_AddNumberToObject(net, "rssi", networks[i].rssi);
            cJSON_AddItemToArray(networks_array, net);
        }
    }
    cJSON_AddItemToObject(wifi, "networks", networks_array);

    cJSON_AddItemToObject(response, "wifi", wifi);

    esp_err_t send_ret = send_json_response(req, response);
    cJSON_Delete(response);
    return send_ret;
}

/**
 * @brief POST /api/device - Patch-style update of settable device fields
 * All fields optional: id, mur_gateway_ip, mur_gateway_port
 */
static esp_err_t device_post_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "POST /api/device");

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
    bool any_update = false;

    // --- Device ID ---
    cJSON *id_json = cJSON_GetObjectItem(request, "id");
    if (cJSON_IsString(id_json) && strlen(id_json->valuestring) > 0) {
        esp_err_t ret = unit_status_set_id(id_json->valuestring);
        if (ret != ESP_OK) {
            cJSON_AddBoolToObject(response, "success", false);
            cJSON_AddStringToObject(response, "error", "Failed to set unit ID");
            send_json_response(req, response);
            cJSON_Delete(response);
            cJSON_Delete(request);
            return ESP_OK;
        }
        any_update = true;
    }

    // --- Mur Gateway config ---
    if (g_track_manager) {
        cJSON *ip_json = cJSON_GetObjectItem(request, "mur_gateway_ip");
        if (cJSON_IsString(ip_json)) {
            strncpy(g_track_manager->mur_gateway_ip, ip_json->valuestring,
                    sizeof(g_track_manager->mur_gateway_ip) - 1);
            g_track_manager->mur_gateway_ip[sizeof(g_track_manager->mur_gateway_ip) - 1] = '\0';
            any_update = true;
        }

        cJSON *port_json = cJSON_GetObjectItem(request, "mur_gateway_port");
        if (cJSON_IsNumber(port_json)) {
            g_track_manager->mur_gateway_port = port_json->valueint;
            any_update = true;
        }

        // --- Scene trigger name ---
        cJSON *stn_json = cJSON_GetObjectItem(request, "scene_trigger_name");
        if (cJSON_IsString(stn_json)) {
            strncpy(g_track_manager->scene_trigger_name, stn_json->valuestring,
                    sizeof(g_track_manager->scene_trigger_name) - 1);
            g_track_manager->scene_trigger_name[sizeof(g_track_manager->scene_trigger_name) - 1] = '\0';
            any_update = true;
            mur_listener_resubscribe();
        }

        // --- Late policy (play | drop) ---
        cJSON *lp_json = cJSON_GetObjectItem(request, "late_policy");
        if (cJSON_IsString(lp_json) && lp_json->valuestring) {
            const char *s = lp_json->valuestring;
            if (strcmp(s, "play") == 0 || strcmp(s, "drop") == 0) {
                g_track_manager->late_policy = config_str_to_late_policy(s);
                any_update = true;
            } else {
                cJSON_AddBoolToObject(response, "success", false);
                cJSON_AddStringToObject(response, "error",
                                        "late_policy must be 'play' or 'drop'");
                send_json_response(req, response);
                cJSON_Delete(response);
                cJSON_Delete(request);
                return ESP_OK;
            }
        }

        // --- Playback offset (signed µs, +delays / -advances; see SYNC_DESIGN.md) ---
        cJSON *off_json = cJSON_GetObjectItem(request, "playback_offset_us");
        if (cJSON_IsNumber(off_json)) {
            /* valuedouble preserves the full int32 range; valueint is 32-bit. */
            double d = off_json->valuedouble;
            if (d < INT32_MIN || d > INT32_MAX) {
                cJSON_AddBoolToObject(response, "success", false);
                cJSON_AddStringToObject(response, "error",
                                        "playback_offset_us out of int32 range");
                send_json_response(req, response);
                cJSON_Delete(response);
                cJSON_Delete(request);
                return ESP_OK;
            }
            g_track_manager->playback_offset_us = (int32_t)d;
            any_update = true;
        }

        // --- Device volume (per-device master, composes with scene global) ---
        cJSON *dv_json = cJSON_GetObjectItem(request, "device_volume");
        if (cJSON_IsNumber(dv_json)) {
            int dv = dv_json->valueint;
            if (dv < 0) dv = 0;
            if (dv > 100) dv = 100;

            audio_control_msg_t vmsg = { .type = AUDIO_ACTION_SET_DEVICE_VOLUME, .data = {} };
            vmsg.data.set_device_volume.volume_percent = dv;
            if (g_track_manager->audio_control_queue &&
                xQueueSend(g_track_manager->audio_control_queue, &vmsg, pdMS_TO_TICKS(100)) == pdPASS) {
                any_update = true;
            } else {
                ESP_LOGE(TAG, "audio_control_queue full; device_volume not applied");
                cJSON_AddBoolToObject(response, "success", false);
                cJSON_AddStringToObject(response, "error", "audio queue full");
                httpd_resp_set_status(req, "503 Service Unavailable");
                send_json_response(req, response);
                cJSON_Delete(response);
                cJSON_Delete(request);
                return ESP_OK;
            }
        }

    }

    if (!any_update) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "No valid fields to update");
        send_json_response(req, response);
        cJSON_Delete(response);
        cJSON_Delete(request);
        return ESP_OK;
    }

    // Return current state after update
    cJSON_AddBoolToObject(response, "success", true);

    char id[MAX_UNIT_ID_LEN];
    if (unit_status_get_id(id, sizeof(id)) == ESP_OK) {
        cJSON_AddStringToObject(response, "id", id);
    }
    if (g_track_manager) {
        cJSON_AddStringToObject(response, "mur_gateway_ip", g_track_manager->mur_gateway_ip);
        cJSON_AddNumberToObject(response, "mur_gateway_port", g_track_manager->mur_gateway_port);
        cJSON_AddStringToObject(response, "scene_trigger_name", g_track_manager->scene_trigger_name);
        cJSON_AddNumberToObject(response, "device_volume", g_track_manager->device_volume_percent);
        cJSON_AddStringToObject(response, "late_policy",
                                config_late_policy_to_str(g_track_manager->late_policy));
        cJSON_AddNumberToObject(response, "playback_offset_us",
                                g_track_manager->playback_offset_us);
    }

    esp_err_t send_ret = send_json_response(req, response);
    cJSON_Delete(response);
    cJSON_Delete(request);
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

    bool scenes_exist = scene_file_exists();
    cJSON_AddBoolToObject(response, "config_exists", scenes_exist);
    cJSON_AddStringToObject(response, "config_path", SCENES_FILE_PATH);

    if (g_scene_manager) {
        cJSON_AddNumberToObject(response, "scene_count", g_scene_manager->scene_count);
        cJSON_AddStringToObject(response, "default_scene", g_scene_manager->default_scene);
        cJSON_AddStringToObject(response, "active_scene", g_scene_manager->active_scene);
    }

    esp_err_t ret = send_json_response(req, response);
    cJSON_Delete(response);

    return ret;
}

/**
 * @brief POST /api/config/save - Save scenes and gateway config to SD card
 */
static esp_err_t config_save_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "POST /api/config/save");

    cJSON *response = cJSON_CreateObject();

    if (!g_scene_manager || !g_track_manager) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "System not initialized");
        send_json_response(req, response);
        cJSON_Delete(response);
        return ESP_OK;
    }

    // Save scenes to scenes.json
    esp_err_t ret = scene_manager_save(g_scene_manager);

    // Also save gateway config to track_config.json
    if (ret == ESP_OK) {
        ret = config_save(g_track_manager);
    }

    if (ret == ESP_OK) {
        cJSON_AddBoolToObject(response, "success", true);
        cJSON_AddStringToObject(response, "message", "Configuration saved successfully");
        cJSON_AddStringToObject(response, "path", SCENES_FILE_PATH);
    } else {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "Failed to save configuration");
    }

    esp_err_t send_ret = send_json_response(req, response);
    cJSON_Delete(response);

    return send_ret;
}

/**
 * @brief POST /api/config/load - Load scenes from SD card and activate default
 */
static esp_err_t config_load_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "POST /api/config/load");

    cJSON *response = cJSON_CreateObject();

    if (!g_scene_manager || !g_track_manager || !g_track_manager->audio_control_queue) {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "System not initialized");
        send_json_response(req, response);
        cJSON_Delete(response);
        return ESP_OK;
    }

    esp_err_t ret = scene_manager_load(g_scene_manager);

    if (ret == ESP_OK) {
        // Also reload gateway + scene trigger config
        // Heap-allocated: track_config_t is too large for the task stack
        track_config_t *gw_config = heap_caps_calloc(1, sizeof(track_config_t),
                                                      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (gw_config && config_load(gw_config) == ESP_OK) {
            strncpy(g_track_manager->mur_gateway_ip, gw_config->mur_gateway_ip,
                    sizeof(g_track_manager->mur_gateway_ip) - 1);
            g_track_manager->mur_gateway_port = gw_config->mur_gateway_port;
            strncpy(g_track_manager->scene_trigger_name, gw_config->scene_trigger_name,
                    sizeof(g_track_manager->scene_trigger_name) - 1);
            g_track_manager->device_volume_percent = gw_config->device_volume_percent;
        }
        free(gw_config);

        // Activate default scene
        if (g_scene_manager->default_scene[0] != '\0') {
            ret = scene_activate(g_scene_manager, g_scene_manager->default_scene,
                                  g_track_manager->audio_control_queue, g_track_manager,
                                  SCENE_ACTIVATE_BOOT);
        } else if (g_scene_manager->scene_count > 0) {
            ret = scene_activate(g_scene_manager, g_scene_manager->scenes[0].name,
                                  g_track_manager->audio_control_queue, g_track_manager,
                                  SCENE_ACTIVATE_BOOT);
        }

        if (ret == ESP_OK) {
            mur_listener_resubscribe();
            cJSON_AddBoolToObject(response, "success", true);
            cJSON_AddStringToObject(response, "message", "Configuration loaded and applied");
            cJSON_AddStringToObject(response, "active_scene", g_scene_manager->active_scene);
        } else {
            cJSON_AddBoolToObject(response, "success", false);
            cJSON_AddStringToObject(response, "error", "Loaded but failed to activate scene");
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

    // Delete scenes file
    struct stat st;
    bool deleted = false;
    if (stat(SCENES_FILE_PATH, &st) == 0) {
        if (unlink(SCENES_FILE_PATH) == 0) deleted = true;
    }
    // Also delete old config file
    config_delete();

    if (deleted) {
        cJSON_AddBoolToObject(response, "success", true);
        cJSON_AddStringToObject(response, "message", "Configuration deleted successfully");
    } else {
        cJSON_AddBoolToObject(response, "success", false);
        cJSON_AddStringToObject(response, "error", "No configuration file found");
    }

    esp_err_t send_ret = send_json_response(req, response);
    cJSON_Delete(response);

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
    
    // Parse query string to get filename (using SPIRAM scratch buffers)
    memset(scratch_query, 0, SCRATCH_QUERY_LEN);
    memset(scratch_name, 0, SCRATCH_NAME_LEN);
    size_t query_len = httpd_req_get_url_query_len(req);

    if (query_len > 0 && query_len < SCRATCH_QUERY_LEN) {
        httpd_req_get_url_query_str(req, scratch_query, SCRATCH_QUERY_LEN);

        // Extract filename from query string (e.g., ?filename=track.wav)
        memset(scratch_param, 0, SCRATCH_PARAM_LEN);
        if (httpd_query_key_value(scratch_query, "filename", scratch_param, SCRATCH_PARAM_LEN) == ESP_OK) {
            // URL decode the filename
            size_t decoded_len = 0;
            for (size_t i = 0, j = 0; i < strlen(scratch_param) && j < SCRATCH_NAME_LEN - 1; i++, j++) {
                if (scratch_param[i] == '%' && i + 2 < strlen(scratch_param)) {
                    char hex[3] = {scratch_param[i+1], scratch_param[i+2], '\0'};
                    scratch_name[j] = (char)strtol(hex, NULL, 16);
                    i += 2;
                } else if (scratch_param[i] == '+') {
                    scratch_name[j] = ' ';
                } else {
                    scratch_name[j] = scratch_param[i];
                }
                decoded_len = j + 1;
            }
            scratch_name[decoded_len] = '\0';
        }
    }

    // If no filename provided, generate one based on timestamp
    if (strlen(scratch_name) == 0) {
        snprintf(scratch_name, SCRATCH_NAME_LEN, "upload_%ld.wav", (long)esp_timer_get_time() / 1000000);
    }

    // Ensure filename doesn't contain path separators (security)
    // Remove any path components, keeping only the filename
    char *base_name = strrchr(scratch_name, '/');
    if (base_name) {
        memmove(scratch_name, base_name + 1, strlen(base_name));
    }
    base_name = strrchr(scratch_name, '\\');
    if (base_name) {
        memmove(scratch_name, base_name + 1, strlen(base_name));
    }
    
    // Build full path
    snprintf(scratch_path, SCRATCH_PATH_LEN, "/sdcard/%s", scratch_name);

    ESP_LOGI(TAG, "Uploading file: %s (size: %d bytes)", scratch_path, req->content_len);

    // Open file for writing
    FILE *file = fopen(scratch_path, "wb");
    if (!file) {
        ESP_LOGE(TAG, "Failed to open file for writing: %s", scratch_path);
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
            remove(scratch_path);  // Clean up partial file
            free(chunk_buf);
            httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Upload failed");
            return ESP_FAIL;
        }
        
        // Write chunk to file
        size_t written = fwrite(chunk_buf, 1, received, file);
        if (written != received) {
            ESP_LOGE(TAG, "Failed to write to file: wrote %d of %d bytes", written, received);
            fclose(file);
            remove(scratch_path);  // Clean up partial file
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
    
    ESP_LOGI(TAG, "File uploaded successfully: %s (%d bytes)", scratch_name, total_received);

    // Send success response
    cJSON *response = cJSON_CreateObject();
    cJSON_AddBoolToObject(response, "success", true);
    cJSON_AddStringToObject(response, "filename", scratch_name);
    cJSON_AddStringToObject(response, "path", scratch_path);
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
    snprintf(scratch_path, SCRATCH_PATH_LEN, "/sdcard/%s", filename);

    // Check if file exists
    struct stat file_stat;
    if (stat(scratch_path, &file_stat) != 0) {
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
    if (remove(scratch_path) == 0) {
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
 * @brief GET /api/file/download?filename=track.wav - Stream a file from the SD card
 *
 * Read-back counterpart to POST /api/upload. Two callers need it: copying audio
 * between devices (every member of an ensemble group must hold the same files),
 * and reading a WAV header to derive a file's duration instead of an operator
 * typing it in.
 *
 * Streamed in chunks, so file size does not bound memory use. All error checks
 * happen before the first chunk goes out — once a chunked response has started,
 * httpd can no longer send an error status.
 */
static esp_err_t file_download_handler(httpd_req_t *req) {
    ESP_LOGI(TAG, "GET /api/file/download");

    memset(scratch_query, 0, SCRATCH_QUERY_LEN);
    memset(scratch_name, 0, SCRATCH_NAME_LEN);

    size_t query_len = httpd_req_get_url_query_len(req);
    if (query_len == 0 || query_len >= SCRATCH_QUERY_LEN) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Missing filename query parameter");
        return ESP_FAIL;
    }
    httpd_req_get_url_query_str(req, scratch_query, SCRATCH_QUERY_LEN);

    memset(scratch_param, 0, SCRATCH_PARAM_LEN);
    if (httpd_query_key_value(scratch_query, "filename", scratch_param,
                              SCRATCH_PARAM_LEN) != ESP_OK) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Missing filename query parameter");
        return ESP_FAIL;
    }

    // URL decode, same handling as the upload handler
    size_t decoded_len = 0;
    size_t param_len = strlen(scratch_param);
    for (size_t i = 0, j = 0; i < param_len && j < SCRATCH_NAME_LEN - 1; i++, j++) {
        if (scratch_param[i] == '%' && i + 2 < param_len) {
            char hex[3] = {scratch_param[i+1], scratch_param[i+2], '\0'};
            scratch_name[j] = (char)strtol(hex, NULL, 16);
            i += 2;
        } else if (scratch_param[i] == '+') {
            scratch_name[j] = ' ';
        } else {
            scratch_name[j] = scratch_param[i];
        }
        decoded_len = j + 1;
    }
    scratch_name[decoded_len] = '\0';

    if (scratch_name[0] == '\0') {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Empty filename");
        return ESP_FAIL;
    }

    // Security check: filename only, no path components
    if (strchr(scratch_name, '/') != NULL || strchr(scratch_name, '\\') != NULL) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST,
                            "Invalid filename - path separators not allowed");
        return ESP_FAIL;
    }

    snprintf(scratch_path, SCRATCH_PATH_LEN, "/sdcard/%s", scratch_name);

    struct stat file_stat;
    if (stat(scratch_path, &file_stat) != 0) {
        httpd_resp_send_err(req, HTTPD_404_NOT_FOUND, "File not found");
        return ESP_FAIL;
    }
    if (!S_ISREG(file_stat.st_mode)) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "Not a regular file");
        return ESP_FAIL;
    }

    FILE *f = fopen(scratch_path, "rb");
    if (!f) {
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Failed to open file");
        return ESP_FAIL;
    }

    #define DOWNLOAD_CHUNK_SIZE 4096
    char *chunk_buf = heap_caps_malloc(DOWNLOAD_CHUNK_SIZE, MALLOC_CAP_SPIRAM);
    if (!chunk_buf) {
        fclose(f);
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "Failed to allocate buffer");
        return ESP_FAIL;
    }

    // Stack-held because httpd_resp_set_hdr stores the pointer rather than
    // copying the string — it must stay valid until the response completes.
    char disposition[SCRATCH_NAME_LEN + 40];
    snprintf(disposition, sizeof(disposition), "attachment; filename=\"%s\"", scratch_name);
    httpd_resp_set_type(req, "application/octet-stream");
    httpd_resp_set_hdr(req, "Content-Disposition", disposition);

    ESP_LOGI(TAG, "Streaming %s (%ld bytes)", scratch_path, (long)file_stat.st_size);

    size_t total = 0;
    esp_err_t ret = ESP_OK;
    while (1) {
        size_t n = fread(chunk_buf, 1, DOWNLOAD_CHUNK_SIZE, f);
        if (n == 0) {
            break;
        }
        if (httpd_resp_send_chunk(req, chunk_buf, n) != ESP_OK) {
            ESP_LOGW(TAG, "Download aborted after %u bytes (client disconnected?)",
                     (unsigned)total);
            ret = ESP_FAIL;
            break;
        }
        total += n;
    }

    fclose(f);
    free(chunk_buf);

    if (ret == ESP_OK) {
        httpd_resp_send_chunk(req, NULL, 0);   // terminate the chunked response
        ESP_LOGI(TAG, "Download complete: %s (%u bytes)", scratch_name, (unsigned)total);
    }
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
        "<title>Murmura Settings</title>"
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
        "<h1>Murmura Settings</h1>"
        
        "<div class='menu-bar'>"
        "<a href='/' class='menu-btn'>Status</a>"
        "<a href='/settings' class='menu-btn active'>Settings</a>"
        "</div>"
        
        "<div class='card'>"
        "<h2>Device ID</h2>"
        "<div id='status-message' class='status-message'></div>"
        "<div class='current-value'>Current ID: <span id='current-id'>Loading...</span></div>"
        "<div class='form-group'>"
        "<label for='device-id'>ID:</label>"
        "<input type='text' id='device-id' placeholder='Enter device ID (e.g., MURMURA-001)' maxlength='32'>"
        "</div>"
        "<button class='btn-primary' onclick='updateDeviceId()'>Update ID</button>"
        "<button class='btn-secondary' onclick='loadCurrentId()'>Refresh</button>"
        "</div>"
        "</div>"
        
        "<script>"
        "function loadCurrentId() {"
        "  fetch('/api/device')"
        "    .then(function(r) { if (!r.ok) throw new Error('HTTP err'); return r.json(); })"
        "    .then(function(d) {"
        "      if (d.id) {"
        "        document.getElementById('current-id').textContent = d.id;"
        "        document.getElementById('device-id').value = d.id;"
        "      } else {"
        "        document.getElementById('current-id').textContent = 'Not Set';"
        "      }"
        "    })"
        "    .catch(function() {"
        "      document.getElementById('current-id').textContent = 'Error';"
        "    });"
        "}"
        "function updateDeviceId() {"
        "  var id = document.getElementById('device-id').value.trim();"
        "  var msg = document.getElementById('status-message');"
        "  if (!id) {"
        "    msg.className = 'status-message error';"
        "    msg.textContent = 'Please enter a device ID';"
        "    return;"
        "  }"
        "  fetch('/api/device', {"
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
        "  .catch(function() {"
        "    msg.className = 'status-message error';"
        "    msg.textContent = 'Network error';"
        "  });"
        "}"
        "if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', loadCurrentId);"
        "else loadCurrentId();"
        "</script>"
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
        "<title>Murmura Controller</title>"
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
        "<h1>Murmura Controller</h1>"
        
        "<div class='menu-bar'>"
        "<a href='/' class='menu-btn active'>Status</a>"
        "<a href='/settings' class='menu-btn'>Settings</a>"
        "</div>"
        
        "<div class='card'>"
        "<h2>Unit Status</h2>"
        "<div id='status-content'>"
        "<div class='loading'>Loading status...</div>"
        "</div>"
        "<div id='device-volume-row' class='status-item' style='align-items:center;'>"
        "<span class='label'>Device Volume <span id='dv-label'>--</span></span>"
        "<input type='range' id='dv-slider' min='0' max='100' value='100' style='flex:1; margin-left:12px;'>"
        "</div>"
        "</div>"

        "<div class='card'>"
        "<h2>Active Scene</h2>"
        "<div id='scene-content'>"
        "<div class='loading'>Loading scene...</div>"
        "</div>"
        "</div>"

        "<div style='display:flex; gap:10px; justify-content:center; margin:20px 0;'>"
        "<button class='refresh-btn' onclick='refreshData()'>Refresh</button>"
        "<button class='refresh-btn' id='save-btn' onclick='saveConfig()'>Save</button>"
        "</div>"
        "<div id='save-status' class='status-item' style='display:none; justify-content:center;'></div>"
        "</div>"
        
        "<script>"
        "function fetchStatus() {"
        "  fetch('/api/device')"
        "    .then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })"
        "    .then(function(data) {"
        "      var c = document.getElementById('status-content');"
        "      if (c) {"
        "        var h = '';"
        "        h += '<div class=\"status-item\"><span class=\"label\">ID</span><span class=\"value\">' + (data.id || 'Not Set') + '</span></div>';"
        "        h += '<div class=\"status-item\"><span class=\"label\">IP Address</span><span class=\"value\">' + (data.ip_address || 'N/A') + '</span></div>';"
        "        h += '<div class=\"status-item\"><span class=\"label\">MAC Address</span><span class=\"value\">' + (data.mac_address || 'N/A') + '</span></div>';"
        "        h += '<div class=\"status-item\"><span class=\"label\">WiFi Status</span><span class=\"value\">' + (data.wifi && data.wifi.connected ? 'Connected' : 'Disconnected') + '</span></div>';"
        "        h += '<div class=\"status-item\"><span class=\"label\">Firmware</span><span class=\"value\">' + (data.firmware_version || 'Unknown') + '</span></div>';"
        "        var secs = data.uptime_seconds || 0; var h2 = Math.floor(secs/3600); var m = Math.floor((secs%3600)/60); var s = secs%60;"
        "        h += '<div class=\"status-item\"><span class=\"label\">Uptime</span><span class=\"value\">' + h2 + 'h ' + m + 'm ' + s + 's</span></div>';"
        "        c.innerHTML = h;"
        "      }"
        "      var dv = (typeof data.device_volume === 'number') ? data.device_volume : 100;"
        "      var slider = document.getElementById('dv-slider');"
        "      var dvLabel = document.getElementById('dv-label');"
        "      if (slider && !slider.dataset.dragging) slider.value = dv;"
        "      if (dvLabel) dvLabel.textContent = dv + '%';"
        "    })"
        "    .catch(function(e) {"
        "      var c = document.getElementById('status-content');"
        "      if (c) c.innerHTML = '<div class=\"error\">Failed to load status: ' + e.message + '</div>';"
        "    });"
        "}"
        "function fetchScenes() {"
        "  fetch('/api/scenes')"
        "    .then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })"
        "    .then(function(data) {"
        "      var c = document.getElementById('scene-content');"
        "      if (!c) return;"
        "      var activeName = data.active_scene || '';"
        "      var scene = data.scenes && data.scenes[activeName];"
        "      if (!scene) { c.innerHTML = '<div class=\"error\">No active scene</div>'; return; }"
        "      var h = '<div class=\"status-item\"><span class=\"label\">Active Scene</span><span class=\"value\">' + activeName + '</span></div>';"
        "      h += '<div class=\"status-item\"><span class=\"label\">Scene Volume</span><span class=\"value\">' + scene.global_volume + '%</span></div>';"
        "      scene.tracks.forEach(function(t) {"
        "        var f = t.file_path ? t.file_path.split('/').pop() : '(no file)';"
        "        var badgeClass, badgeText;"
        "        if (t.playing)     { badgeClass = 'playing-badge'; badgeText = 'PLAYING'; }"
        "        else if (t.active) { badgeClass = 'stopped-badge\" style=\"background:#ff9800'; badgeText = 'ARMED'; }"
        "        else               { badgeClass = 'stopped-badge'; badgeText = 'STOPPED'; }"
        "        h += '<div class=\"track\">';"
        "        h += '<div class=\"track-header\"><span class=\"track-title\">Track ' + (t.track + 1) + ' (' + t.mode + ')</span>';"
        "        h += '<span class=\"' + badgeClass + '\">' + badgeText + '</span></div>';"
        "        h += '<div class=\"track-info\"><div>File: ' + f + '</div><div>Volume: ' + t.volume + '%</div>';"
        "        if (t.trigger_name) h += '<div>Trigger: ' + t.trigger_name + ' (' + t.trigger_type + ')</div>';"
        "        h += '</div>';"
        "        h += '<div class=\"volume-bar\"><div class=\"volume-fill\" style=\"width:' + t.volume + '%\"></div></div>';"
        "        h += '</div>';"
        "      });"
        "      c.innerHTML = h;"
        "    })"
        "    .catch(function(e) {"
        "      var c = document.getElementById('scene-content');"
        "      if (c) c.innerHTML = '<div class=\"error\">Failed to load scene: ' + e.message + '</div>';"
        "    });"
        "}"
        "function saveConfig() {"
        "  var btn = document.getElementById('save-btn');"
        "  var st = document.getElementById('save-status');"
        "  btn.disabled = true;"
        "  st.style.display = 'flex';"
        "  st.innerHTML = '<span class=\"label\">Saving...</span>';"
        "  fetch('/api/config/save', {method: 'POST'})"
        "    .then(function(r) { return r.json(); })"
        "    .then(function(d) {"
        "      if (d.success) st.innerHTML = '<span class=\"value\" style=\"color:#2e7d32\">Saved</span>';"
        "      else st.innerHTML = '<span class=\"value\" style=\"color:#c62828\">Save failed: ' + (d.error || 'unknown') + '</span>';"
        "    })"
        "    .catch(function(e) {"
        "      st.innerHTML = '<span class=\"value\" style=\"color:#c62828\">Save error: ' + e.message + '</span>';"
        "    })"
        "    .finally(function() {"
        "      btn.disabled = false;"
        "      setTimeout(function() { st.style.display = 'none'; }, 4000);"
        "    });"
        "}"
        "(function() {"
        "  var slider = document.getElementById('dv-slider');"
        "  var lbl = document.getElementById('dv-label');"
        "  if (!slider) return;"
        "  slider.addEventListener('input', function() {"
        "    slider.dataset.dragging = '1';"
        "    if (lbl) lbl.textContent = slider.value + '%';"
        "  });"
        "  slider.addEventListener('change', function() {"
        "    fetch('/api/device', {"
        "      method: 'POST',"
        "      headers: {'Content-Type': 'application/json'},"
        "      body: JSON.stringify({device_volume: parseInt(slider.value, 10)})"
        "    }).finally(function() { delete slider.dataset.dragging; });"
        "  });"
        "})();"
        "function refreshData() { fetchStatus(); fetchScenes(); }"
        "refreshData();"
        "setInterval(refreshData, 5000);"
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
    
    // Initialize cJSON to use SPIRAM for all allocations
    init_cjson_spiram();
    ESP_LOGI(TAG, "cJSON configured to use SPIRAM");

    // Allocate shared scratch buffers in SPIRAM (reused across all handlers)
    if (!scratch_path) {
        scratch_path  = heap_caps_malloc(SCRATCH_PATH_LEN,  MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        scratch_query = heap_caps_malloc(SCRATCH_QUERY_LEN, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        scratch_name  = heap_caps_malloc(SCRATCH_NAME_LEN,  MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        scratch_param = heap_caps_malloc(SCRATCH_PARAM_LEN, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        ESP_LOGI(TAG, "HTTP scratch buffers allocated in SPIRAM (768 bytes)");
    }
    
    // Note: loop manager will be set by audio_control_task via http_server_set_track_manager
    // We don't create one here - we'll use the shared one from audio control task
    g_track_manager = NULL;
    
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = HTTP_SERVER_PORT;
    config.stack_size = 8192;
    config.max_uri_handlers = 22;  // Reduced after consolidating device config endpoints
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
    
    httpd_uri_t scenes_get_uri = {
        .uri = "/api/scenes",
        .method = HTTP_GET,
        .handler = scenes_get_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &scenes_get_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for GET /api/scenes: %s", esp_err_to_name(ret));
    }

    httpd_uri_t scenes_post_uri = {
        .uri = "/api/scenes",
        .method = HTTP_POST,
        .handler = scenes_post_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &scenes_post_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for POST /api/scenes: %s", esp_err_to_name(ret));
    }

    httpd_uri_t scene_action_uri = {
        .uri = "/api/scene",
        .method = HTTP_POST,
        .handler = scene_action_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &scene_action_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for POST /api/scene: %s", esp_err_to_name(ret));
    }
    
    // Register consolidated device configuration endpoint
    httpd_uri_t device_get_uri = {
        .uri = "/api/device",
        .method = HTTP_GET,
        .handler = device_get_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &device_get_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for GET /api/device: %s", esp_err_to_name(ret));
    }

    httpd_uri_t device_post_uri = {
        .uri = "/api/device",
        .method = HTTP_POST,
        .handler = device_post_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &device_post_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for POST /api/device: %s", esp_err_to_name(ret));
    }

    // Register WiFi management endpoints
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

    httpd_uri_t file_download_uri = {
        .uri = "/api/file/download",
        .method = HTTP_GET,
        .handler = file_download_handler,
        .user_ctx = NULL
    };
    ret = httpd_register_uri_handler(server, &file_download_uri);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to register handler for /api/file/download: %s", esp_err_to_name(ret));
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
    
    // Note: We don't free g_track_manager here because it's owned by audio_control_task
    g_track_manager = NULL;
    
    return ESP_OK;
}

/**
 * @brief Get current track status (unused - state accessed via g_track_manager directly)
 */
esp_err_t http_server_get_loop_status(track_manager_t *manager) {
    if (!manager || !g_track_manager) {
        return ESP_ERR_INVALID_ARG;
    }
    
    memcpy(manager, g_track_manager, sizeof(track_manager_t));
    return ESP_OK;
}

/**
 * @brief Set the track manager reference
 */
esp_err_t http_server_set_track_manager(track_manager_t *manager) {
    if (!manager) {
        return ESP_ERR_INVALID_ARG;
    }

    g_track_manager = manager;
    ESP_LOGI(TAG, "Track manager reference updated");
    return ESP_OK;
}

esp_err_t http_server_set_scene_manager(scene_manager_t *manager) {
    if (!manager) {
        return ESP_ERR_INVALID_ARG;
    }

    g_scene_manager = manager;
    ESP_LOGI(TAG, "Scene manager reference updated");
    return ESP_OK;
}
