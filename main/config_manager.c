#include "config_manager.h"
#include "cJSON.h"
#include "esp_log.h"
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static const char *TAG = "CONFIG_MANAGER";

// Default configuration as JSON string - compiled into binary
// This can be edited to change the default startup configuration
static const char *DEFAULT_CONFIG_JSON = 
"{\n"
"  \"global_volume\": 75,\n"
"  \"loops\": [\n"
"    {\n"
"      \"track\": 0,\n"
"      \"is_playing\": true,\n"
"      \"file_path\": \"/sdcard/track1.wav\",\n"
"      \"volume\": 100\n"
"    },\n"
"    {\n"
"      \"track\": 1,\n"
"      \"is_playing\": true,\n"
"      \"file_path\": \"/sdcard/track2.wav\",\n"
"      \"volume\": 100\n"
"    },\n"
"    {\n"
"      \"track\": 2,\n"
"      \"is_playing\": true,\n"
"      \"file_path\": \"/sdcard/track3.wav\",\n"
"      \"volume\": 100\n"
"    }\n"
"  ]\n"
"}";

esp_err_t config_save(const loop_manager_t *manager) {
    if (!manager) {
        ESP_LOGE(TAG, "Invalid manager pointer");
        return ESP_ERR_INVALID_ARG;
    }
    
    // Create JSON object
    cJSON *root = cJSON_CreateObject();
    if (!root) {
        ESP_LOGE(TAG, "Failed to create JSON object");
        return ESP_ERR_NO_MEM;
    }
    
    // Add global volume
    cJSON_AddNumberToObject(root, "global_volume", manager->global_volume_percent);
    
    // Add loops array
    cJSON *loops = cJSON_CreateArray();
    for (int i = 0; i < MAX_TRACKS; i++) {
        cJSON *loop = cJSON_CreateObject();
        cJSON_AddNumberToObject(loop, "track", i);
        cJSON_AddBoolToObject(loop, "is_playing", manager->loops[i].is_playing);
        cJSON_AddStringToObject(loop, "file_path", manager->loops[i].file_path);
        cJSON_AddNumberToObject(loop, "volume", manager->loops[i].volume_percent);
        cJSON_AddItemToArray(loops, loop);
    }
    cJSON_AddItemToObject(root, "loops", loops);
    
    // Add timestamp
    cJSON_AddNumberToObject(root, "timestamp", (double)esp_timer_get_time() / 1000000.0);
    
    // Convert to string
    char *json_str = cJSON_Print(root);
    cJSON_Delete(root);
    
    if (!json_str) {
        ESP_LOGE(TAG, "Failed to print JSON");
        return ESP_ERR_NO_MEM;
    }
    
    // Write to file
    FILE *f = fopen(CONFIG_FILE_PATH, "w");
    if (!f) {
        ESP_LOGE(TAG, "Failed to open config file for writing: %s", CONFIG_FILE_PATH);
        free(json_str);
        return ESP_FAIL;
    }
    
    size_t json_len = strlen(json_str);
    ESP_LOGI(TAG, "Writing config file: %d bytes", json_len);
    
    size_t written = fwrite(json_str, 1, json_len, f);
    int close_result = fclose(f);
    
    free(json_str);
    
    // Check if close succeeded - this is more reliable than checking fwrite return
    if (close_result != 0) {
        ESP_LOGE(TAG, "Failed to close config file properly");
        return ESP_FAIL;
    }
    
    ESP_LOGI(TAG, "Write completed: %d bytes written", written);
    
    ESP_LOGI(TAG, "Configuration saved to %s", CONFIG_FILE_PATH);
    return ESP_OK;
}

esp_err_t config_load(loop_config_t *config) {
    if (!config) {
        ESP_LOGE(TAG, "Invalid config pointer");
        return ESP_ERR_INVALID_ARG;
    }
    
    // Check if file exists
    struct stat st;
    if (stat(CONFIG_FILE_PATH, &st) != 0) {
        ESP_LOGW(TAG, "Configuration file not found: %s", CONFIG_FILE_PATH);
        return ESP_ERR_NOT_FOUND;
    }
    
    // Read file
    FILE *f = fopen(CONFIG_FILE_PATH, "r");
    if (!f) {
        ESP_LOGE(TAG, "Failed to open config file for reading: %s", CONFIG_FILE_PATH);
        return ESP_FAIL;
    }
    
    // Allocate buffer for file content
    char *buffer = heap_caps_malloc(st.st_size + 1, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!buffer) {
        fclose(f);
        ESP_LOGE(TAG, "Failed to allocate memory for config file");
        return ESP_ERR_NO_MEM;
    }
    
    size_t read_size = fread(buffer, 1, st.st_size, f);
    fclose(f);
    
    if (read_size != st.st_size) {
        free(buffer);
        ESP_LOGE(TAG, "Failed to read complete config file");
        return ESP_FAIL;
    }
    
    buffer[st.st_size] = '\0';
    
    // Parse JSON
    esp_err_t ret = config_from_json_string(buffer, config);
    free(buffer);
    
    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "Configuration loaded from %s", CONFIG_FILE_PATH);
    }
    
    return ret;
}

esp_err_t config_apply(const loop_config_t *config, QueueHandle_t audio_control_queue, loop_manager_t *loop_manager) {
    if (!config || !audio_control_queue || !loop_manager) {
        ESP_LOGE(TAG, "Invalid parameters");
        return ESP_ERR_INVALID_ARG;
    }
    
    ESP_LOGI(TAG, "Applying configuration...");
    
    // Apply global volume
    audio_control_msg_t vol_msg = {
        .type = AUDIO_ACTION_SET_GLOBAL_VOLUME,
        .data = {}
    };
    vol_msg.data.set_global_volume.volume_percent = config->global_volume_percent;
    if (xQueueSend(audio_control_queue, &vol_msg, pdMS_TO_TICKS(100)) != pdPASS) {
        ESP_LOGW(TAG, "Failed to set global volume");
    }
    
    // Apply each track configuration
    for (int i = 0; i < MAX_TRACKS; i++) {
        // Set track volume
        audio_control_msg_t track_vol_msg = {
            .type = AUDIO_ACTION_SET_VOLUME,
            .data = {}
        };
        track_vol_msg.data.set_volume.track_index = i;
        track_vol_msg.data.set_volume.volume_percent = config->loops[i].volume_percent;
        if (xQueueSend(audio_control_queue, &track_vol_msg, pdMS_TO_TICKS(100)) != pdPASS) {
            ESP_LOGW(TAG, "Failed to set volume for track %d", i);
        }
        
        // Update loop manager state
        loop_manager->loops[i].volume_percent = config->loops[i].volume_percent;
        strncpy(loop_manager->loops[i].file_path, config->loops[i].file_path, 
                sizeof(loop_manager->loops[i].file_path) - 1);
        
        // Start or stop track based on configuration
        if (config->loops[i].is_playing && strlen(config->loops[i].file_path) > 0) {
            // Start the track
            audio_control_msg_t start_msg = {
                .type = AUDIO_ACTION_START_TRACK,
                .data = {}
            };
            start_msg.data.start_track.track_index = i;
            strncpy(start_msg.data.start_track.file_path, config->loops[i].file_path,
                    sizeof(start_msg.data.start_track.file_path) - 1);
            
            if (xQueueSend(audio_control_queue, &start_msg, pdMS_TO_TICKS(100)) == pdPASS) {
                loop_manager->loops[i].is_playing = true;
                ESP_LOGI(TAG, "Started track %d with file: %s", i, config->loops[i].file_path);
            } else {
                ESP_LOGW(TAG, "Failed to start track %d", i);
            }
        } else if (!config->loops[i].is_playing && loop_manager->loops[i].is_playing) {
            // Stop the track
            audio_control_msg_t stop_msg = {
                .type = AUDIO_ACTION_STOP_TRACK,
                .data = {}
            };
            stop_msg.data.stop_track.track_index = i;
            
            if (xQueueSend(audio_control_queue, &stop_msg, pdMS_TO_TICKS(100)) == pdPASS) {
                loop_manager->loops[i].is_playing = false;
                ESP_LOGI(TAG, "Stopped track %d", i);
            } else {
                ESP_LOGW(TAG, "Failed to stop track %d", i);
            }
        }
    }
    
    loop_manager->global_volume_percent = config->global_volume_percent;
    
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
    } else {
        ESP_LOGE(TAG, "Failed to delete configuration file");
        return ESP_FAIL;
    }
}

esp_err_t config_backup(void) {
    // Check if original file exists
    if (!config_exists()) {
        ESP_LOGW(TAG, "No configuration file to backup");
        return ESP_ERR_NOT_FOUND;
    }
    
    // Read original file
    struct stat st;
    if (stat(CONFIG_FILE_PATH, &st) != 0) {
        return ESP_FAIL;
    }
    
    FILE *src = fopen(CONFIG_FILE_PATH, "r");
    if (!src) {
        ESP_LOGE(TAG, "Failed to open source file for backup");
        return ESP_FAIL;
    }
    
    char *buffer = heap_caps_malloc(st.st_size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!buffer) {
        fclose(src);
        return ESP_ERR_NO_MEM;
    }
    
    size_t read_size = fread(buffer, 1, st.st_size, src);
    fclose(src);
    
    if (read_size != st.st_size) {
        free(buffer);
        return ESP_FAIL;
    }
    
    // Write to backup file
    FILE *dst = fopen(CONFIG_BACKUP_PATH, "w");
    if (!dst) {
        free(buffer);
        ESP_LOGE(TAG, "Failed to open backup file for writing");
        return ESP_FAIL;
    }
    
    size_t written = fwrite(buffer, 1, st.st_size, dst);
    fclose(dst);
    free(buffer);
    
    if (written != st.st_size) {
        ESP_LOGE(TAG, "Failed to write backup file");
        return ESP_FAIL;
    }
    
    ESP_LOGI(TAG, "Configuration backed up to %s", CONFIG_BACKUP_PATH);
    return ESP_OK;
}

esp_err_t config_restore_backup(void) {
    // Check if backup exists
    struct stat st;
    if (stat(CONFIG_BACKUP_PATH, &st) != 0) {
        ESP_LOGW(TAG, "No backup file found");
        return ESP_ERR_NOT_FOUND;
    }
    
    // Read backup file
    FILE *src = fopen(CONFIG_BACKUP_PATH, "r");
    if (!src) {
        ESP_LOGE(TAG, "Failed to open backup file");
        return ESP_FAIL;
    }
    
    char *buffer = heap_caps_malloc(st.st_size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!buffer) {
        fclose(src);
        return ESP_ERR_NO_MEM;
    }
    
    size_t read_size = fread(buffer, 1, st.st_size, src);
    fclose(src);
    
    if (read_size != st.st_size) {
        free(buffer);
        return ESP_FAIL;
    }
    
    // Write to main config file
    FILE *dst = fopen(CONFIG_FILE_PATH, "w");
    if (!dst) {
        free(buffer);
        ESP_LOGE(TAG, "Failed to open config file for restore");
        return ESP_FAIL;
    }
    
    size_t written = fwrite(buffer, 1, st.st_size, dst);
    fclose(dst);
    free(buffer);
    
    if (written != st.st_size) {
        ESP_LOGE(TAG, "Failed to restore configuration");
        return ESP_FAIL;
    }
    
    ESP_LOGI(TAG, "Configuration restored from backup");
    return ESP_OK;
}

esp_err_t config_to_json_string(const loop_manager_t *manager, char **json_str) {
    if (!manager || !json_str) {
        return ESP_ERR_INVALID_ARG;
    }
    
    // Create JSON object
    cJSON *root = cJSON_CreateObject();
    if (!root) {
        return ESP_ERR_NO_MEM;
    }
    
    // Add global volume
    cJSON_AddNumberToObject(root, "global_volume", manager->global_volume_percent);
    
    // Add loops array
    cJSON *loops = cJSON_CreateArray();
    for (int i = 0; i < MAX_TRACKS; i++) {
        cJSON *loop = cJSON_CreateObject();
        cJSON_AddNumberToObject(loop, "track", i);
        cJSON_AddBoolToObject(loop, "is_playing", manager->loops[i].is_playing);
        cJSON_AddStringToObject(loop, "file_path", manager->loops[i].file_path);
        cJSON_AddNumberToObject(loop, "volume", manager->loops[i].volume_percent);
        cJSON_AddItemToArray(loops, loop);
    }
    cJSON_AddItemToObject(root, "loops", loops);
    
    // Convert to string
    *json_str = cJSON_Print(root);
    cJSON_Delete(root);
    
    if (!*json_str) {
        return ESP_ERR_NO_MEM;
    }
    
    return ESP_OK;
}

esp_err_t config_from_json_string(const char *json_str, loop_config_t *config) {
    if (!json_str || !config) {
        return ESP_ERR_INVALID_ARG;
    }
    
    // Parse JSON
    cJSON *root = cJSON_Parse(json_str);
    if (!root) {
        ESP_LOGE(TAG, "Failed to parse JSON configuration");
        return ESP_FAIL;
    }
    
    // Initialize config with defaults
    memset(config, 0, sizeof(loop_config_t));
    config->global_volume_percent = 75;  // Default volume
    for (int i = 0; i < MAX_TRACKS; i++) {
        config->loops[i].volume_percent = 100;  // Default track volume
        config->loops[i].is_playing = false;
        config->loops[i].file_path[0] = '\0';
    }
    
    // Parse global volume
    cJSON *global_vol = cJSON_GetObjectItem(root, "global_volume");
    if (cJSON_IsNumber(global_vol)) {
        config->global_volume_percent = global_vol->valueint;
    }
    
    // Parse loops
    cJSON *loops = cJSON_GetObjectItem(root, "loops");
    if (cJSON_IsArray(loops)) {
        int array_size = cJSON_GetArraySize(loops);
        for (int i = 0; i < array_size && i < MAX_TRACKS; i++) {
            cJSON *loop = cJSON_GetArrayItem(loops, i);
            if (!cJSON_IsObject(loop)) continue;
            
            // Get track index
            cJSON *track_idx = cJSON_GetObjectItem(loop, "track");
            int idx = cJSON_IsNumber(track_idx) ? track_idx->valueint : i;
            if (idx < 0 || idx >= MAX_TRACKS) continue;
            
            // Parse loop properties
            cJSON *is_playing = cJSON_GetObjectItem(loop, "is_playing");
            if (cJSON_IsBool(is_playing)) {
                config->loops[idx].is_playing = cJSON_IsTrue(is_playing);
            }
            
            cJSON *file_path = cJSON_GetObjectItem(loop, "file_path");
            if (cJSON_IsString(file_path) && file_path->valuestring) {
                strncpy(config->loops[idx].file_path, file_path->valuestring,
                        sizeof(config->loops[idx].file_path) - 1);
            }
            
            cJSON *volume = cJSON_GetObjectItem(loop, "volume");
            if (cJSON_IsNumber(volume)) {
                config->loops[idx].volume_percent = volume->valueint;
            }
        }
    }
    
    cJSON_Delete(root);
    ESP_LOGI(TAG, "Configuration parsed successfully");
    return ESP_OK;
}

esp_err_t config_get_default(loop_config_t *config) {
    if (!config) {
        ESP_LOGE(TAG, "Invalid config pointer");
        return ESP_ERR_INVALID_ARG;
    }
    
    ESP_LOGI(TAG, "Loading default configuration from compiled-in JSON");
    
    // Parse the default JSON string
    esp_err_t ret = config_from_json_string(DEFAULT_CONFIG_JSON, config);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to parse default configuration JSON");
        // Fall back to hardcoded defaults if JSON parsing fails
        memset(config, 0, sizeof(loop_config_t));
        config->global_volume_percent = 75;
        for (int i = 0; i < MAX_TRACKS; i++) {
            config->loops[i].is_playing = false;
            config->loops[i].volume_percent = 100;
            config->loops[i].file_path[0] = '\0';
        }
    }
    
    return ESP_OK;
}

esp_err_t config_load_or_default(loop_config_t *config) {
    if (!config) {
        ESP_LOGE(TAG, "Invalid config pointer");
        return ESP_ERR_INVALID_ARG;
    }
    
    // Try to load from file first
    esp_err_t ret = config_load(config);
    
    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "Configuration loaded from file: %s", CONFIG_FILE_PATH);
        return ESP_OK;
    } else if (ret == ESP_ERR_NOT_FOUND) {
        ESP_LOGI(TAG, "No saved configuration file found, using default configuration");
        return config_get_default(config);
    } else {
        ESP_LOGW(TAG, "Failed to load configuration file, using default configuration");
        return config_get_default(config);
    }
}
