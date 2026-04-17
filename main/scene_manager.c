#include "scene_manager.h"
#include "cJSON.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include <stdio.h>
#include <string.h>
#include <ctype.h>
#include <sys/stat.h>

static const char *TAG = "SCENE_MGR";

// Mutex protecting all scene manager mutations and file I/O
static SemaphoreHandle_t s_scene_mutex = NULL;

// --- Helpers ---

bool scene_name_valid(const char *name) {
    if (!name || name[0] == '\0') return false;
    size_t len = strlen(name);
    if (len >= MAX_SCENE_NAME_LEN) return false;
    for (size_t i = 0; i < len; i++) {
        char c = name[i];
        if (!isalnum((unsigned char)c) && c != '-' && c != '_') return false;
    }
    return true;
}

void scene_config_init_default(scene_config_t *scene) {
    if (!scene) return;
    scene->global_volume_percent = 75;
    for (int i = 0; i < MAX_TRACKS; i++) {
        scene->tracks[i].mode = TRACK_MODE_LOOP;
        scene->tracks[i].active = false;
        scene->tracks[i].volume_percent = 100;
        scene->tracks[i].file_path[0] = '\0';
        scene->tracks[i].trigger_name[0] = '\0';
        scene->tracks[i].trigger_mode = TRIGGER_MODE_MOMENTARY;
    }
}

bool scene_file_exists(void) {
    struct stat st;
    return (stat(SCENES_FILE_PATH, &st) == 0);
}

scene_config_t* scene_find(scene_manager_t *mgr, const char *name) {
    if (!mgr || !name) return NULL;
    for (int i = 0; i < mgr->scene_count; i++) {
        if (strcmp(mgr->scenes[i].name, name) == 0) {
            return &mgr->scenes[i];
        }
    }
    return NULL;
}

// --- File I/O ---

esp_err_t scene_manager_save(const scene_manager_t *mgr) {
    if (!mgr) return ESP_ERR_INVALID_ARG;

    xSemaphoreTake(s_scene_mutex, portMAX_DELAY);

    cJSON *root = cJSON_CreateObject();
    if (!root) {
        xSemaphoreGive(s_scene_mutex);
        return ESP_ERR_NO_MEM;
    }

    cJSON_AddStringToObject(root, "default_scene", mgr->default_scene);

    cJSON *scenes_obj = cJSON_CreateObject();
    for (int i = 0; i < mgr->scene_count; i++) {
        const scene_config_t *sc = &mgr->scenes[i];
        cJSON *scene_json = cJSON_CreateObject();
        cJSON_AddNumberToObject(scene_json, "global_volume", sc->global_volume_percent);
        if (sc->button_trigger[0] != '\0') {
            cJSON_AddStringToObject(scene_json, "button_trigger", sc->button_trigger);
        }
        cJSON *tracks = config_tracks_to_json(sc->tracks);
        if (tracks) cJSON_AddItemToObject(scene_json, "tracks", tracks);
        cJSON_AddItemToObject(scenes_obj, sc->name, scene_json);
    }
    cJSON_AddItemToObject(root, "scenes", scenes_obj);

    char *json_str = cJSON_Print(root);
    cJSON_Delete(root);
    if (!json_str) {
        xSemaphoreGive(s_scene_mutex);
        return ESP_ERR_NO_MEM;
    }

    FILE *f = fopen(SCENES_FILE_PATH, "w");
    if (!f) {
        ESP_LOGE(TAG, "Failed to open %s for writing", SCENES_FILE_PATH);
        free(json_str);
        xSemaphoreGive(s_scene_mutex);
        return ESP_FAIL;
    }

    size_t len = strlen(json_str);
    size_t written = fwrite(json_str, 1, len, f);
    fclose(f);
    free(json_str);

    xSemaphoreGive(s_scene_mutex);

    if (written != len) {
        ESP_LOGE(TAG, "Incomplete write to scenes file");
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "Saved %d scene(s) to %s", mgr->scene_count, SCENES_FILE_PATH);
    return ESP_OK;
}

esp_err_t scene_manager_load(scene_manager_t *mgr) {
    if (!mgr) return ESP_ERR_INVALID_ARG;

    xSemaphoreTake(s_scene_mutex, portMAX_DELAY);

    struct stat st;
    if (stat(SCENES_FILE_PATH, &st) != 0) {
        xSemaphoreGive(s_scene_mutex);
        return ESP_ERR_NOT_FOUND;
    }

    FILE *f = fopen(SCENES_FILE_PATH, "r");
    if (!f) {
        xSemaphoreGive(s_scene_mutex);
        return ESP_FAIL;
    }

    char *buffer = heap_caps_malloc(st.st_size + 1, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!buffer) {
        fclose(f);
        xSemaphoreGive(s_scene_mutex);
        return ESP_ERR_NO_MEM;
    }

    size_t read_size = fread(buffer, 1, st.st_size, f);
    fclose(f);
    if (read_size != (size_t)st.st_size) {
        free(buffer);
        xSemaphoreGive(s_scene_mutex);
        return ESP_FAIL;
    }
    buffer[st.st_size] = '\0';

    cJSON *root = cJSON_Parse(buffer);
    free(buffer);
    if (!root) {
        ESP_LOGE(TAG, "Failed to parse scenes JSON");
        xSemaphoreGive(s_scene_mutex);
        return ESP_FAIL;
    }

    // Parse default_scene
    cJSON *def = cJSON_GetObjectItem(root, "default_scene");
    if (cJSON_IsString(def) && def->valuestring) {
        strncpy(mgr->default_scene, def->valuestring, MAX_SCENE_NAME_LEN - 1);
        mgr->default_scene[MAX_SCENE_NAME_LEN - 1] = '\0';
    } else {
        mgr->default_scene[0] = '\0';
    }

    // Parse scenes dict
    mgr->scene_count = 0;
    cJSON *scenes_obj = cJSON_GetObjectItem(root, "scenes");
    if (cJSON_IsObject(scenes_obj)) {
        cJSON *scene_json = NULL;
        cJSON_ArrayForEach(scene_json, scenes_obj) {
            if (mgr->scene_count >= MAX_SCENES) {
                ESP_LOGW(TAG, "Max scenes (%d) reached, skipping rest", MAX_SCENES);
                break;
            }
            if (!cJSON_IsObject(scene_json)) continue;

            const char *name = scene_json->string;  // key name in the dict
            if (!name || !scene_name_valid(name)) continue;

            scene_config_t *sc = &mgr->scenes[mgr->scene_count];
            strncpy(sc->name, name, MAX_SCENE_NAME_LEN - 1);
            sc->name[MAX_SCENE_NAME_LEN - 1] = '\0';

            config_parse_scene_from_json(scene_json, &sc->global_volume_percent, sc->tracks);
            cJSON *bt = cJSON_GetObjectItem(scene_json, "button_trigger");
            if (cJSON_IsString(bt) && bt->valuestring) {
                strncpy(sc->button_trigger, bt->valuestring, sizeof(sc->button_trigger) - 1);
            }
            mgr->scene_count++;
        }
    }

    cJSON_Delete(root);
    xSemaphoreGive(s_scene_mutex);

    ESP_LOGI(TAG, "Loaded %d scene(s) from %s (default: '%s')",
             mgr->scene_count, SCENES_FILE_PATH, mgr->default_scene);
    return ESP_OK;
}

// --- Init ---

esp_err_t scene_manager_init(scene_manager_t **mgr_out) {
    if (!mgr_out) return ESP_ERR_INVALID_ARG;

    if (!s_scene_mutex) {
        s_scene_mutex = xSemaphoreCreateMutex();
        if (!s_scene_mutex) return ESP_ERR_NO_MEM;
    }

    scene_manager_t *mgr = heap_caps_calloc(1, sizeof(scene_manager_t),
                                             MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!mgr) return ESP_ERR_NO_MEM;

    mgr->scene_count = 0;
    mgr->default_scene[0] = '\0';
    mgr->active_scene[0] = '\0';

    // Try to load from file
    if (scene_file_exists()) {
        esp_err_t ret = scene_manager_load(mgr);
        if (ret != ESP_OK) {
            ESP_LOGW(TAG, "Failed to load scenes file, starting empty");
        }
    }

    // If no scenes loaded, create a "default" scene from hardcoded defaults
    if (mgr->scene_count == 0) {
        ESP_LOGI(TAG, "No scenes found, creating 'default' scene");
        scene_config_t *sc = &mgr->scenes[0];
        strncpy(sc->name, "default", MAX_SCENE_NAME_LEN - 1);
        sc->global_volume_percent = 75;
        // Use the hardcoded default file paths
        static const char *default_files[MAX_TRACKS] = {
            "/sdcard/track1.wav", "/sdcard/track2.wav", "/sdcard/track3.wav"
        };
        for (int i = 0; i < MAX_TRACKS; i++) {
            sc->tracks[i].mode = TRACK_MODE_LOOP;
            sc->tracks[i].active = (i == 0);
            sc->tracks[i].volume_percent = 100;
            strncpy(sc->tracks[i].file_path, default_files[i],
                    sizeof(sc->tracks[i].file_path) - 1);
            sc->tracks[i].trigger_name[0] = '\0';
            sc->tracks[i].trigger_mode = TRIGGER_MODE_MOMENTARY;
        }
        mgr->scene_count = 1;
        strncpy(mgr->default_scene, "default", MAX_SCENE_NAME_LEN - 1);
    }

    *mgr_out = mgr;
    ESP_LOGI(TAG, "Scene manager initialized (%d scenes)", mgr->scene_count);
    return ESP_OK;
}

// --- Scene CRUD ---

esp_err_t scene_create(scene_manager_t *mgr, const char *name) {
    if (!mgr || !name) return ESP_ERR_INVALID_ARG;
    if (!scene_name_valid(name)) return ESP_ERR_INVALID_ARG;
    if (scene_find(mgr, name) != NULL) return ESP_ERR_INVALID_STATE;  // already exists
    if (mgr->scene_count >= MAX_SCENES) return ESP_ERR_NO_MEM;

    scene_config_t *sc = &mgr->scenes[mgr->scene_count];

    // Clone from active scene if one exists, otherwise use defaults
    scene_config_t *source = (mgr->active_scene[0] != '\0')
                             ? scene_find(mgr, mgr->active_scene)
                             : NULL;
    if (source) {
        memcpy(sc, source, sizeof(scene_config_t));
        ESP_LOGI(TAG, "Cloned scene '%s' from active scene '%s'", name, source->name);
    } else {
        memset(sc, 0, sizeof(scene_config_t));
        scene_config_init_default(sc);
        ESP_LOGI(TAG, "Created scene '%s' with defaults", name);
    }

    // Set the new name (overwrite the cloned name)
    strncpy(sc->name, name, MAX_SCENE_NAME_LEN - 1);
    sc->name[MAX_SCENE_NAME_LEN - 1] = '\0';
    mgr->scene_count++;

    ESP_LOGI(TAG, "Scene count: %d", mgr->scene_count);
    return ESP_OK;
}

esp_err_t scene_delete(scene_manager_t *mgr, const char *name) {
    if (!mgr || !name) return ESP_ERR_INVALID_ARG;

    // Cannot delete the active scene
    if (strcmp(mgr->active_scene, name) == 0) {
        ESP_LOGE(TAG, "Cannot delete active scene '%s'", name);
        return ESP_ERR_INVALID_STATE;
    }

    int idx = -1;
    for (int i = 0; i < mgr->scene_count; i++) {
        if (strcmp(mgr->scenes[i].name, name) == 0) {
            idx = i;
            break;
        }
    }
    if (idx < 0) return ESP_ERR_NOT_FOUND;

    // Shift remaining scenes down
    for (int i = idx; i < mgr->scene_count - 1; i++) {
        memcpy(&mgr->scenes[i], &mgr->scenes[i + 1], sizeof(scene_config_t));
    }
    mgr->scene_count--;

    // Clear default if it was the deleted scene
    if (strcmp(mgr->default_scene, name) == 0) {
        mgr->default_scene[0] = '\0';
    }

    ESP_LOGI(TAG, "Deleted scene '%s' (%d remaining)", name, mgr->scene_count);
    return ESP_OK;
}

// --- Activate ---

esp_err_t scene_activate(scene_manager_t *mgr, const char *name,
                         QueueHandle_t queue, track_manager_t *track_mgr) {
    if (!mgr || !name || !queue || !track_mgr) return ESP_ERR_INVALID_ARG;

    scene_config_t *sc = scene_find(mgr, name);
    if (!sc) {
        ESP_LOGE(TAG, "Scene '%s' not found", name);
        return ESP_ERR_NOT_FOUND;
    }

    // Allocate in SPIRAM — track_config_t is ~1300+ bytes, too large for the
    // audio_control_task's 4096-byte stack
    track_config_t *config = heap_caps_calloc(1, sizeof(track_config_t),
                                               MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!config) return ESP_ERR_NO_MEM;

    config->global_volume_percent = sc->global_volume_percent;
    memcpy(config->tracks, sc->tracks, sizeof(config->tracks));

    // Preserve device-level config from current track_manager
    strncpy(config->mur_gateway_ip, track_mgr->mur_gateway_ip,
            sizeof(config->mur_gateway_ip) - 1);
    config->mur_gateway_port = track_mgr->mur_gateway_port;
    strncpy(config->scene_trigger_name, track_mgr->scene_trigger_name,
            sizeof(config->scene_trigger_name) - 1);

    esp_err_t ret = config_apply(config, queue, track_mgr);
    free(config);

    if (ret == ESP_OK) {
        strncpy(mgr->active_scene, name, MAX_SCENE_NAME_LEN - 1);
        mgr->active_scene[MAX_SCENE_NAME_LEN - 1] = '\0';
        ESP_LOGI(TAG, "Activated scene '%s'", name);
    }
    return ret;
}

// --- Patch validation & application ---

esp_err_t scene_validate_patch(scene_manager_t *mgr, cJSON *patch_body,
                               char *error_msg, size_t error_msg_size) {
    if (!mgr || !patch_body) return ESP_ERR_INVALID_ARG;

    // Iterate each key in the patch body — each key should be a scene name
    cJSON *scene_patch = NULL;
    cJSON_ArrayForEach(scene_patch, patch_body) {
        const char *name = scene_patch->string;

        // Check scene exists
        if (!scene_find(mgr, name)) {
            if (error_msg) {
                snprintf(error_msg, error_msg_size, "Scene '%s' not found", name);
            }
            return ESP_ERR_NOT_FOUND;
        }

        if (!cJSON_IsObject(scene_patch)) {
            if (error_msg) {
                snprintf(error_msg, error_msg_size,
                         "Value for scene '%s' must be an object", name);
            }
            return ESP_ERR_INVALID_ARG;
        }

        // Validate global_volume if present
        cJSON *gv = cJSON_GetObjectItem(scene_patch, "global_volume");
        if (gv && !cJSON_IsNumber(gv)) {
            if (error_msg) {
                snprintf(error_msg, error_msg_size,
                         "Scene '%s': global_volume must be a number", name);
            }
            return ESP_ERR_INVALID_ARG;
        }
        if (gv && (gv->valueint < 0 || gv->valueint > 100)) {
            if (error_msg) {
                snprintf(error_msg, error_msg_size,
                         "Scene '%s': global_volume must be 0-100", name);
            }
            return ESP_ERR_INVALID_ARG;
        }

        // Validate tracks array if present
        cJSON *tracks_arr = cJSON_GetObjectItem(scene_patch, "tracks");
        if (tracks_arr) {
            if (!cJSON_IsArray(tracks_arr)) {
                if (error_msg) {
                    snprintf(error_msg, error_msg_size,
                             "Scene '%s': tracks must be an array", name);
                }
                return ESP_ERR_INVALID_ARG;
            }

            int n = cJSON_GetArraySize(tracks_arr);
            for (int i = 0; i < n; i++) {
                cJSON *t = cJSON_GetArrayItem(tracks_arr, i);
                if (!cJSON_IsObject(t)) {
                    if (error_msg) {
                        snprintf(error_msg, error_msg_size,
                                 "Scene '%s': track entry must be an object", name);
                    }
                    return ESP_ERR_INVALID_ARG;
                }

                // track index required
                cJSON *tidx = cJSON_GetObjectItem(t, "track");
                if (!cJSON_IsNumber(tidx) || tidx->valueint < 0 || tidx->valueint >= MAX_TRACKS) {
                    if (error_msg) {
                        snprintf(error_msg, error_msg_size,
                                 "Scene '%s': invalid track index", name);
                    }
                    return ESP_ERR_INVALID_ARG;
                }

                // mode
                cJSON *mode = cJSON_GetObjectItem(t, "mode");
                if (mode) {
                    if (!cJSON_IsString(mode) ||
                        (strcmp(mode->valuestring, "loop") != 0 &&
                         strcmp(mode->valuestring, "trigger") != 0)) {
                        if (error_msg) {
                            snprintf(error_msg, error_msg_size,
                                     "Scene '%s' track %d: mode must be 'loop' or 'trigger'",
                                     name, tidx->valueint);
                        }
                        return ESP_ERR_INVALID_ARG;
                    }
                }

                // volume
                cJSON *vol = cJSON_GetObjectItem(t, "volume");
                if (vol && (!cJSON_IsNumber(vol) || vol->valueint < 0 || vol->valueint > 100)) {
                    if (error_msg) {
                        snprintf(error_msg, error_msg_size,
                                 "Scene '%s' track %d: volume must be 0-100",
                                 name, tidx->valueint);
                    }
                    return ESP_ERR_INVALID_ARG;
                }

                // trigger_mode
                cJSON *tm = cJSON_GetObjectItem(t, "trigger_mode");
                if (tm) {
                    if (!cJSON_IsString(tm) ||
                        (strcmp(tm->valuestring, "momentary") != 0 &&
                         strcmp(tm->valuestring, "oneshot") != 0)) {
                        if (error_msg) {
                            snprintf(error_msg, error_msg_size,
                                     "Scene '%s' track %d: trigger_mode must be 'momentary' or 'oneshot'",
                                     name, tidx->valueint);
                        }
                        return ESP_ERR_INVALID_ARG;
                    }
                }

                // file_path — check SD card if specified
                cJSON *fp = cJSON_GetObjectItem(t, "file_path");
                if (!fp) fp = cJSON_GetObjectItem(t, "file");
                if (fp && cJSON_IsString(fp) && fp->valuestring[0]) {
                    char resolved[MAX_FILE_PATH_LEN];
                    const char *fv = fp->valuestring;
                    if (fv[0] == '/') {
                        strncpy(resolved, fv, sizeof(resolved) - 1);
                        resolved[sizeof(resolved) - 1] = '\0';
                    } else {
                        // Reject path separators in bare filenames
                        if (strchr(fv, '/') || strchr(fv, '\\')) {
                            if (error_msg) {
                                snprintf(error_msg, error_msg_size,
                                         "Scene '%s' track %d: invalid filename",
                                         name, tidx->valueint);
                            }
                            return ESP_ERR_INVALID_ARG;
                        }
                        snprintf(resolved, sizeof(resolved), "/sdcard/%s", fv);
                    }
                    struct stat file_st;
                    if (stat(resolved, &file_st) != 0) {
                        if (error_msg) {
                            snprintf(error_msg, error_msg_size,
                                     "Scene '%s' track %d: file not found: %s",
                                     name, tidx->valueint, resolved);
                        }
                        return ESP_ERR_INVALID_ARG;
                    }
                }
            }
        }
    }

    return ESP_OK;
}

esp_err_t scene_apply_patch(scene_manager_t *mgr, cJSON *patch_body,
                            QueueHandle_t queue, track_manager_t *track_mgr) {
    if (!mgr || !patch_body) return ESP_ERR_INVALID_ARG;

    cJSON *scene_patch = NULL;
    cJSON_ArrayForEach(scene_patch, patch_body) {
        const char *name = scene_patch->string;
        scene_config_t *sc = scene_find(mgr, name);
        if (!sc) continue;  // validated in pass 1

        bool is_active = (strcmp(mgr->active_scene, name) == 0);

        // Apply global_volume
        cJSON *gv = cJSON_GetObjectItem(scene_patch, "global_volume");
        if (cJSON_IsNumber(gv)) {
            int vol = gv->valueint;
            if (vol < 0) vol = 0;
            if (vol > 100) vol = 100;
            sc->global_volume_percent = vol;

            if (is_active && queue && track_mgr) {
                audio_control_msg_t msg = { .type = AUDIO_ACTION_SET_GLOBAL_VOLUME, .data = {} };
                msg.data.set_global_volume.volume_percent = vol;
                xQueueSend(queue, &msg, pdMS_TO_TICKS(500));
                track_mgr->global_volume_percent = vol;
            }
        }

        // Apply button_trigger
        cJSON *bt = cJSON_GetObjectItem(scene_patch, "button_trigger");
        if (cJSON_IsString(bt)) {
            strncpy(sc->button_trigger, bt->valuestring, sizeof(sc->button_trigger) - 1);
            sc->button_trigger[sizeof(sc->button_trigger) - 1] = '\0';
        }

        // Apply tracks
        cJSON *tracks_arr = cJSON_GetObjectItem(scene_patch, "tracks");
        if (cJSON_IsArray(tracks_arr)) {
            int n = cJSON_GetArraySize(tracks_arr);
            for (int i = 0; i < n; i++) {
                cJSON *t = cJSON_GetArrayItem(tracks_arr, i);
                cJSON *tidx = cJSON_GetObjectItem(t, "track");
                int track = tidx->valueint;
                track_config_entry_t *entry = &sc->tracks[track];

                bool was_active = entry->active;
                bool file_changed = false;

                // mode
                cJSON *mode = cJSON_GetObjectItem(t, "mode");
                if (cJSON_IsString(mode)) {
                    entry->mode = config_str_to_mode(mode->valuestring);
                    if (is_active && track_mgr) {
                        track_mgr->tracks[track].mode = entry->mode;
                    }
                }

                // trigger_name
                cJSON *tn = cJSON_GetObjectItem(t, "trigger_name");
                if (cJSON_IsString(tn)) {
                    strncpy(entry->trigger_name, tn->valuestring,
                            sizeof(entry->trigger_name) - 1);
                    entry->trigger_name[sizeof(entry->trigger_name) - 1] = '\0';
                    if (is_active && track_mgr) {
                        strncpy(track_mgr->tracks[track].trigger_name, entry->trigger_name,
                                sizeof(track_mgr->tracks[track].trigger_name) - 1);
                    }
                }

                // trigger_mode
                cJSON *tm = cJSON_GetObjectItem(t, "trigger_mode");
                if (cJSON_IsString(tm)) {
                    entry->trigger_mode = config_str_to_trigger_mode(tm->valuestring);
                    if (is_active && track_mgr) {
                        track_mgr->tracks[track].trigger_mode = entry->trigger_mode;
                    }
                }

                // file_path
                cJSON *fp = cJSON_GetObjectItem(t, "file_path");
                if (!fp) fp = cJSON_GetObjectItem(t, "file");
                if (cJSON_IsString(fp)) {
                    char resolved[MAX_FILE_PATH_LEN] = {0};
                    const char *fv = fp->valuestring;
                    if (fv[0] == '\0') {
                        // Empty string = clear file
                        resolved[0] = '\0';
                    } else if (fv[0] == '/') {
                        strncpy(resolved, fv, sizeof(resolved) - 1);
                    } else {
                        snprintf(resolved, sizeof(resolved), "/sdcard/%s", fv);
                    }
                    if (strcmp(entry->file_path, resolved) != 0) {
                        strncpy(entry->file_path, resolved, sizeof(entry->file_path) - 1);
                        entry->file_path[sizeof(entry->file_path) - 1] = '\0';
                        file_changed = true;
                    }
                }

                // volume
                cJSON *vol = cJSON_GetObjectItem(t, "volume");
                if (cJSON_IsNumber(vol)) {
                    int v = vol->valueint;
                    if (v < 0) v = 0;
                    if (v > 100) v = 100;
                    entry->volume_percent = v;

                    if (is_active && queue && track_mgr) {
                        audio_control_msg_t msg = { .type = AUDIO_ACTION_SET_VOLUME, .data = {} };
                        msg.data.set_volume.track_index = track;
                        msg.data.set_volume.volume_percent = v;
                        xQueueSend(queue, &msg, pdMS_TO_TICKS(500));
                        track_mgr->tracks[track].volume_percent = v;
                    }
                }

                // active
                cJSON *active_json = cJSON_GetObjectItem(t, "active");
                if (cJSON_IsBool(active_json)) {
                    entry->active = cJSON_IsTrue(active_json);
                }

                // Apply hardware changes for active scene
                if (is_active && queue && track_mgr) {
                    // Sync mode, trigger, file to track_manager
                    track_mgr->tracks[track].mode = entry->mode;
                    strncpy(track_mgr->tracks[track].file_path, entry->file_path,
                            sizeof(track_mgr->tracks[track].file_path) - 1);
                    strncpy(track_mgr->tracks[track].trigger_name, entry->trigger_name,
                            sizeof(track_mgr->tracks[track].trigger_name) - 1);
                    track_mgr->tracks[track].trigger_mode = entry->trigger_mode;

                    // Handle active state changes
                    if (cJSON_IsBool(active_json)) {
                        bool want_active = cJSON_IsTrue(active_json);
                        bool is_trigger = (entry->mode == TRACK_MODE_TRIGGER);

                        if (is_trigger) {
                            audio_control_msg_t msg = {
                                .type = want_active ? AUDIO_ACTION_ENABLE_TRACK : AUDIO_ACTION_DISABLE_TRACK,
                                .data = {}
                            };
                            msg.data.stop_track.track_index = track;
                            xQueueSend(queue, &msg, pdMS_TO_TICKS(500));
                        } else if (want_active) {
                            if (entry->file_path[0] != '\0') {
                                audio_control_msg_t enable_msg = { .type = AUDIO_ACTION_ENABLE_TRACK, .data = {} };
                                enable_msg.data.stop_track.track_index = track;
                                xQueueSend(queue, &enable_msg, pdMS_TO_TICKS(500));

                                audio_control_send_start_track(queue, track,
                                        entry->file_path, pdMS_TO_TICKS(500));
                            }
                        } else {
                            audio_control_msg_t disable_msg = { .type = AUDIO_ACTION_DISABLE_TRACK, .data = {} };
                            disable_msg.data.stop_track.track_index = track;
                            xQueueSend(queue, &disable_msg, pdMS_TO_TICKS(500));
                        }
                    } else if (file_changed && was_active &&
                               is_track_playing(track_mgr, track)) {
                        // File changed while track is playing — restart
                        audio_control_send_start_track(queue, track,
                                entry->file_path, pdMS_TO_TICKS(500));
                    }
                }
            }
        }
    }

    return ESP_OK;
}

// --- GET response builder ---

cJSON* scene_build_get_response(const scene_manager_t *mgr, const track_manager_t *track_mgr) {
    cJSON *root = cJSON_CreateObject();
    if (!root) return NULL;

    cJSON_AddStringToObject(root, "default_scene", mgr->default_scene);
    cJSON_AddStringToObject(root, "active_scene", mgr->active_scene);

    cJSON *scenes_obj = cJSON_CreateObject();
    for (int i = 0; i < mgr->scene_count; i++) {
        const scene_config_t *sc = &mgr->scenes[i];
        cJSON *scene_json = cJSON_CreateObject();
        cJSON_AddNumberToObject(scene_json, "global_volume", sc->global_volume_percent);
        cJSON_AddStringToObject(scene_json, "button_trigger", sc->button_trigger);

        bool is_active = (strcmp(mgr->active_scene, sc->name) == 0);

        cJSON *tracks = cJSON_CreateArray();
        for (int j = 0; j < MAX_TRACKS; j++) {
            cJSON *t = cJSON_CreateObject();
            cJSON_AddNumberToObject(t, "track", j);
            cJSON_AddStringToObject(t, "mode", config_mode_to_str(sc->tracks[j].mode));
            cJSON_AddBoolToObject(t, "active", sc->tracks[j].active);
            cJSON_AddStringToObject(t, "file_path", sc->tracks[j].file_path);
            cJSON_AddNumberToObject(t, "volume", sc->tracks[j].volume_percent);
            cJSON_AddStringToObject(t, "trigger_name", sc->tracks[j].trigger_name);
            cJSON_AddStringToObject(t, "trigger_mode",
                                    config_trigger_mode_to_str(sc->tracks[j].trigger_mode));

            // Add playing state for active scene only
            if (is_active && track_mgr) {
                cJSON_AddBoolToObject(t, "playing", is_track_playing((track_manager_t*)track_mgr, j));
            }

            cJSON_AddItemToArray(tracks, t);
        }
        cJSON_AddItemToObject(scene_json, "tracks", tracks);
        cJSON_AddItemToObject(scenes_obj, sc->name, scene_json);
    }
    cJSON_AddItemToObject(root, "scenes", scenes_obj);

    return root;
}
