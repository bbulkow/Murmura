#include "config_manager.h"
#include "cJSON.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static const char *TAG = "CONFIG_MANAGER";

// Default configuration — track 0 active looping, tracks 1+2 inactive, all with default filenames
static const char *DEFAULT_CONFIG_JSON =
"{\n"
"  \"global_volume\": 75,\n"
"  \"tracks\": [\n"
"    { \"track\": 0, \"mode\": \"loop\", \"active\": true,  \"file_path\": \"/sdcard/track1.wav\", \"volume\": 100 },\n"
"    { \"track\": 1, \"mode\": \"loop\", \"active\": false, \"file_path\": \"/sdcard/track2.wav\", \"volume\": 100 },\n"
"    { \"track\": 2, \"mode\": \"loop\", \"active\": false, \"file_path\": \"/sdcard/track3.wav\", \"volume\": 100 }\n"
"  ]\n"
"}";

// Helper: convert mode enum to string
static const char *mode_to_str(track_mode_t mode) {
    return (mode == TRACK_MODE_TRIGGER) ? "trigger" : "loop";
}

// Helper: parse mode string to enum
static track_mode_t str_to_mode(const char *s) {
    if (s && strcmp(s, "trigger") == 0) return TRACK_MODE_TRIGGER;
    return TRACK_MODE_LOOP;
}

esp_err_t config_save(const track_manager_t *manager) {
    if (!manager) {
        ESP_LOGE(TAG, "Invalid manager pointer");
        return ESP_ERR_INVALID_ARG;
    }

    cJSON *root = cJSON_CreateObject();
    if (!root) return ESP_ERR_NO_MEM;

    cJSON_AddNumberToObject(root, "global_volume", manager->global_volume_percent);

    cJSON *tracks_arr = cJSON_CreateArray();
    for (int i = 0; i < MAX_TRACKS; i++) {
        cJSON *t = cJSON_CreateObject();
        cJSON_AddNumberToObject(t, "track", i);
        cJSON_AddStringToObject(t, "mode", mode_to_str(manager->tracks[i].mode));
        cJSON_AddBoolToObject(t, "active", manager->tracks[i].active);
        cJSON_AddStringToObject(t, "file_path", manager->tracks[i].file_path);
        cJSON_AddNumberToObject(t, "volume", manager->tracks[i].volume_percent);
        cJSON_AddItemToArray(tracks_arr, t);
    }
    cJSON_AddItemToObject(root, "tracks", tracks_arr);
    cJSON_AddNumberToObject(root, "timestamp", (double)esp_timer_get_time() / 1000000.0);

    char *json_str = cJSON_Print(root);
    cJSON_Delete(root);
    if (!json_str) return ESP_ERR_NO_MEM;

    FILE *f = fopen(CONFIG_FILE_PATH, "w");
    if (!f) {
        ESP_LOGE(TAG, "Failed to open config file for writing: %s", CONFIG_FILE_PATH);
        free(json_str);
        return ESP_FAIL;
    }

    size_t len = strlen(json_str);
    size_t written = fwrite(json_str, 1, len, f);
    int close_result = fclose(f);
    free(json_str);

    if (close_result != 0) {
        ESP_LOGE(TAG, "Failed to close config file");
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "Configuration saved to %s (%d bytes)", CONFIG_FILE_PATH, (int)written);
    return ESP_OK;
}

esp_err_t config_load(track_config_t *config) {
    if (!config) return ESP_ERR_INVALID_ARG;

    struct stat st;
    if (stat(CONFIG_FILE_PATH, &st) != 0) {
        ESP_LOGW(TAG, "Configuration file not found: %s", CONFIG_FILE_PATH);
        return ESP_ERR_NOT_FOUND;
    }

    FILE *f = fopen(CONFIG_FILE_PATH, "r");
    if (!f) {
        ESP_LOGE(TAG, "Failed to open config file: %s", CONFIG_FILE_PATH);
        return ESP_FAIL;
    }

    char *buffer = heap_caps_malloc(st.st_size + 1, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!buffer) {
        fclose(f);
        return ESP_ERR_NO_MEM;
    }

    size_t read_size = fread(buffer, 1, st.st_size, f);
    fclose(f);

    if (read_size != (size_t)st.st_size) {
        free(buffer);
        ESP_LOGE(TAG, "Failed to read complete config file");
        return ESP_FAIL;
    }
    buffer[st.st_size] = '\0';

    esp_err_t ret = config_from_json_string(buffer, config);
    free(buffer);

    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "Configuration loaded from %s", CONFIG_FILE_PATH);
    }
    return ret;
}

esp_err_t config_apply(const track_config_t *config, QueueHandle_t audio_control_queue, track_manager_t *track_manager) {
    if (!config || !audio_control_queue || !track_manager) return ESP_ERR_INVALID_ARG;

    ESP_LOGI(TAG, "Applying configuration...");

    // Set global volume
    audio_control_msg_t vol_msg = { .type = AUDIO_ACTION_SET_GLOBAL_VOLUME, .data = {} };
    vol_msg.data.set_global_volume.volume_percent = config->global_volume_percent;
    xQueueSend(audio_control_queue, &vol_msg, pdMS_TO_TICKS(100));

    for (int i = 0; i < MAX_TRACKS; i++) {
        // Set track volume
        audio_control_msg_t vol_track = { .type = AUDIO_ACTION_SET_VOLUME, .data = {} };
        vol_track.data.set_volume.track_index = i;
        vol_track.data.set_volume.volume_percent = config->tracks[i].volume_percent;
        xQueueSend(audio_control_queue, &vol_track, pdMS_TO_TICKS(100));

        // Update manager state
        track_manager->tracks[i].volume_percent = config->tracks[i].volume_percent;
        track_manager->tracks[i].mode = config->tracks[i].mode;
        strncpy(track_manager->tracks[i].file_path, config->tracks[i].file_path,
                sizeof(track_manager->tracks[i].file_path) - 1);

        // Start or stop based on active flag
        if (config->tracks[i].active && strlen(config->tracks[i].file_path) > 0) {
            audio_control_msg_t start_msg = { .type = AUDIO_ACTION_START_TRACK, .data = {} };
            start_msg.data.start_track.track_index = i;
            strncpy(start_msg.data.start_track.file_path, config->tracks[i].file_path,
                    sizeof(start_msg.data.start_track.file_path) - 1);
            if (xQueueSend(audio_control_queue, &start_msg, pdMS_TO_TICKS(100)) == pdPASS) {
                track_manager->tracks[i].active = true;
                ESP_LOGI(TAG, "Started track %d: %s", i, config->tracks[i].file_path);
            }
        } else if (!config->tracks[i].active && track_manager->tracks[i].active) {
            audio_control_msg_t stop_msg = { .type = AUDIO_ACTION_STOP_TRACK, .data = {} };
            stop_msg.data.stop_track.track_index = i;
            if (xQueueSend(audio_control_queue, &stop_msg, pdMS_TO_TICKS(100)) == pdPASS) {
                track_manager->tracks[i].active = false;
            }
        }
    }

    track_manager->global_volume_percent = config->global_volume_percent;
    ESP_LOGI(TAG, "Configuration applied successfully");
    return ESP_OK;
}

bool config_exists(void) {
    struct stat st;
    return (stat(CONFIG_FILE_PATH, &st) == 0);
}

esp_err_t config_delete(void) {
    if (unlink(CONFIG_FILE_PATH) == 0) {
        ESP_LOGI(TAG, "Configuration file deleted");
        return ESP_OK;
    }
    ESP_LOGE(TAG, "Failed to delete configuration file");
    return ESP_FAIL;
}

esp_err_t config_backup(void) {
    if (!config_exists()) return ESP_ERR_NOT_FOUND;

    struct stat st;
    if (stat(CONFIG_FILE_PATH, &st) != 0) return ESP_FAIL;

    FILE *src = fopen(CONFIG_FILE_PATH, "r");
    if (!src) return ESP_FAIL;

    char *buffer = heap_caps_malloc(st.st_size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!buffer) { fclose(src); return ESP_ERR_NO_MEM; }

    size_t read_size = fread(buffer, 1, st.st_size, src);
    fclose(src);
    if (read_size != (size_t)st.st_size) { free(buffer); return ESP_FAIL; }

    FILE *dst = fopen(CONFIG_BACKUP_PATH, "w");
    if (!dst) { free(buffer); return ESP_FAIL; }

    size_t written = fwrite(buffer, 1, st.st_size, dst);
    fclose(dst);
    free(buffer);

    if (written != (size_t)st.st_size) return ESP_FAIL;
    ESP_LOGI(TAG, "Configuration backed up to %s", CONFIG_BACKUP_PATH);
    return ESP_OK;
}

esp_err_t config_restore_backup(void) {
    struct stat st;
    if (stat(CONFIG_BACKUP_PATH, &st) != 0) return ESP_ERR_NOT_FOUND;

    FILE *src = fopen(CONFIG_BACKUP_PATH, "r");
    if (!src) return ESP_FAIL;

    char *buffer = heap_caps_malloc(st.st_size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!buffer) { fclose(src); return ESP_ERR_NO_MEM; }

    size_t read_size = fread(buffer, 1, st.st_size, src);
    fclose(src);
    if (read_size != (size_t)st.st_size) { free(buffer); return ESP_FAIL; }

    FILE *dst = fopen(CONFIG_FILE_PATH, "w");
    if (!dst) { free(buffer); return ESP_FAIL; }

    size_t written = fwrite(buffer, 1, st.st_size, dst);
    fclose(dst);
    free(buffer);

    if (written != (size_t)st.st_size) return ESP_FAIL;
    ESP_LOGI(TAG, "Configuration restored from backup");
    return ESP_OK;
}

esp_err_t config_to_json_string(const track_manager_t *manager, char **json_str) {
    if (!manager || !json_str) return ESP_ERR_INVALID_ARG;

    cJSON *root = cJSON_CreateObject();
    if (!root) return ESP_ERR_NO_MEM;

    cJSON_AddNumberToObject(root, "global_volume", manager->global_volume_percent);

    cJSON *tracks_arr = cJSON_CreateArray();
    for (int i = 0; i < MAX_TRACKS; i++) {
        cJSON *t = cJSON_CreateObject();
        cJSON_AddNumberToObject(t, "track", i);
        cJSON_AddStringToObject(t, "mode", mode_to_str(manager->tracks[i].mode));
        cJSON_AddBoolToObject(t, "active", manager->tracks[i].active);
        cJSON_AddStringToObject(t, "file_path", manager->tracks[i].file_path);
        cJSON_AddNumberToObject(t, "volume", manager->tracks[i].volume_percent);
        cJSON_AddItemToArray(tracks_arr, t);
    }
    cJSON_AddItemToObject(root, "tracks", tracks_arr);

    *json_str = cJSON_Print(root);
    cJSON_Delete(root);
    if (!*json_str) return ESP_ERR_NO_MEM;
    return ESP_OK;
}

esp_err_t config_from_json_string(const char *json_str, track_config_t *config) {
    if (!json_str || !config) return ESP_ERR_INVALID_ARG;

    cJSON *root = cJSON_Parse(json_str);
    if (!root) {
        ESP_LOGE(TAG, "Failed to parse JSON configuration");
        return ESP_FAIL;
    }

    // Defaults
    memset(config, 0, sizeof(track_config_t));
    config->global_volume_percent = 75;
    for (int i = 0; i < MAX_TRACKS; i++) {
        config->tracks[i].mode = TRACK_MODE_LOOP;
        config->tracks[i].active = false;
        config->tracks[i].volume_percent = 100;
        config->tracks[i].file_path[0] = '\0';
    }

    cJSON *global_vol = cJSON_GetObjectItem(root, "global_volume");
    if (cJSON_IsNumber(global_vol)) {
        config->global_volume_percent = global_vol->valueint;
    }

    cJSON *tracks_arr = cJSON_GetObjectItem(root, "tracks");
    if (cJSON_IsArray(tracks_arr)) {
        int n = cJSON_GetArraySize(tracks_arr);
        for (int i = 0; i < n && i < MAX_TRACKS; i++) {
            cJSON *t = cJSON_GetArrayItem(tracks_arr, i);
            if (!cJSON_IsObject(t)) continue;

            cJSON *track_idx = cJSON_GetObjectItem(t, "track");
            int idx = cJSON_IsNumber(track_idx) ? track_idx->valueint : i;
            if (idx < 0 || idx >= MAX_TRACKS) continue;

            cJSON *mode = cJSON_GetObjectItem(t, "mode");
            if (cJSON_IsString(mode)) {
                config->tracks[idx].mode = str_to_mode(mode->valuestring);
            }

            cJSON *active = cJSON_GetObjectItem(t, "active");
            if (cJSON_IsBool(active)) {
                config->tracks[idx].active = cJSON_IsTrue(active);
            }

            cJSON *file_path = cJSON_GetObjectItem(t, "file_path");
            if (cJSON_IsString(file_path) && file_path->valuestring) {
                strncpy(config->tracks[idx].file_path, file_path->valuestring,
                        sizeof(config->tracks[idx].file_path) - 1);
            }

            cJSON *volume = cJSON_GetObjectItem(t, "volume");
            if (cJSON_IsNumber(volume)) {
                config->tracks[idx].volume_percent = volume->valueint;
            }
        }
    }

    cJSON_Delete(root);
    ESP_LOGI(TAG, "Configuration parsed successfully");
    return ESP_OK;
}

esp_err_t config_get_default(track_config_t *config) {
    if (!config) return ESP_ERR_INVALID_ARG;
    ESP_LOGI(TAG, "Loading default configuration");
    esp_err_t ret = config_from_json_string(DEFAULT_CONFIG_JSON, config);
    if (ret != ESP_OK) {
        // Hardcoded fallback
        static const char *default_files[MAX_TRACKS] = {
            "/sdcard/track1.wav", "/sdcard/track2.wav", "/sdcard/track3.wav"
        };
        memset(config, 0, sizeof(track_config_t));
        config->global_volume_percent = 75;
        for (int i = 0; i < MAX_TRACKS; i++) {
            config->tracks[i].mode = TRACK_MODE_LOOP;
            config->tracks[i].active = (i == 0);
            config->tracks[i].volume_percent = 100;
            strncpy(config->tracks[i].file_path, default_files[i],
                    sizeof(config->tracks[i].file_path) - 1);
        }
    }
    return ESP_OK;
}

esp_err_t config_load_or_default(track_config_t *config) {
    if (!config) return ESP_ERR_INVALID_ARG;

    esp_err_t ret = config_load(config);
    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "Configuration loaded from file: %s", CONFIG_FILE_PATH);
        return ESP_OK;
    }

    ESP_LOGI(TAG, "No saved config found, using defaults");
    return config_get_default(config);
}
