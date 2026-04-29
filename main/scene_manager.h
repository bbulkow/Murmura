#ifndef SCENE_MANAGER_H
#define SCENE_MANAGER_H

#include "esp_err.h"
#include "config_manager.h"
#include "cJSON.h"

// Scene storage
#define SCENES_FILE_PATH        "/sdcard/scenes.json"
#define MAX_SCENE_NAME_LEN      32
#define MAX_SCENES              16

// A single scene: named playback configuration
typedef struct {
    char name[MAX_SCENE_NAME_LEN];
    int global_volume_percent;
    track_config_entry_t tracks[MAX_TRACKS];
    char button_trigger[MAX_TRIGGER_NAME_LEN];  // trigger name that activates this scene on "On"
    bool synchronized;  // if true, only synchronized paths may activate this scene; see SYNC_DESIGN.md
} scene_config_t;

// Source of a scene_activate request — drives the synchronized-scene gate.
// See SYNC_DESIGN.md "Synchronized scenes".
typedef enum {
    SCENE_ACTIVATE_TRIGGER = 0,    // dispatched from a deferred trigger event (path 1, 2) — sync-allowed
    SCENE_ACTIVATE_BACKSTOP = 1,   // get_scene reply from gateway (path 3) — refused for sync scenes
    SCENE_ACTIVATE_DIRECT = 2,     // direct REST POST /api/scene activate (path 4) — refused for sync scenes
    SCENE_ACTIVATE_BOOT = 3,       // app_main startup or post-config-reload (path 5, 6) — sync-allowed but logs ERROR
} scene_activate_source_t;

// Full scene manager state (lives in SPIRAM)
typedef struct {
    scene_config_t scenes[MAX_SCENES];
    int scene_count;
    char default_scene[MAX_SCENE_NAME_LEN];
    char active_scene[MAX_SCENE_NAME_LEN];  // runtime only, not persisted to disk
} scene_manager_t;

/**
 * @brief Initialize the scene manager. Allocates in SPIRAM, loads from file or creates empty.
 */
esp_err_t scene_manager_init(scene_manager_t **mgr);

/**
 * @brief Save all scenes to /sdcard/scenes.json
 */
esp_err_t scene_manager_save(const scene_manager_t *mgr);

/**
 * @brief Load scenes from /sdcard/scenes.json into existing manager
 */
esp_err_t scene_manager_load(scene_manager_t *mgr);

/**
 * @brief Check if scenes file exists on SD card
 */
bool scene_file_exists(void);

/**
 * @brief Find a scene by name. Returns NULL if not found.
 */
scene_config_t* scene_find(scene_manager_t *mgr, const char *name);

/**
 * @brief Create a new scene with default config. Fails if name exists or max scenes reached.
 */
esp_err_t scene_create(scene_manager_t *mgr, const char *name);

/**
 * @brief Delete a scene by name. Cannot delete the active scene.
 */
esp_err_t scene_delete(scene_manager_t *mgr, const char *name);

/**
 * @brief Activate a scene — apply its config to the hardware.
 *        Preserves gateway settings from track_manager.
 *
 *        Returns ESP_ERR_INVALID_STATE if the scene is marked synchronized
 *        and the source is BACKSTOP or DIRECT (gating per SYNC_DESIGN.md).
 *        BOOT is allowed but logs an ERROR if the boot scene is synchronized.
 */
esp_err_t scene_activate(scene_manager_t *mgr, const char *name,
                         QueueHandle_t queue, track_manager_t *track_mgr,
                         scene_activate_source_t source);

/**
 * @brief Validate a patch body (pass 1 of atomic update).
 *        Checks all scene names exist and all values are valid.
 *        Returns ESP_OK if valid, ESP_ERR_INVALID_ARG with error_msg set on failure.
 */
esp_err_t scene_validate_patch(scene_manager_t *mgr, cJSON *patch_body,
                               char *error_msg, size_t error_msg_size);

/**
 * @brief Apply a validated patch body (pass 2 of atomic update).
 *        For the active scene, sends audio_control_queue messages for hardware changes.
 */
esp_err_t scene_apply_patch(scene_manager_t *mgr, cJSON *patch_body,
                            QueueHandle_t queue, track_manager_t *track_mgr);

/**
 * @brief Validate a scene name (1-31 chars, alphanumeric + hyphen + underscore).
 */
bool scene_name_valid(const char *name);

/**
 * @brief Build the full scenes GET response as cJSON.
 *        Caller must cJSON_Delete the result.
 */
cJSON* scene_build_get_response(const scene_manager_t *mgr, const track_manager_t *track_mgr);

/**
 * @brief Initialize a scene_config_t with default values.
 */
void scene_config_init_default(scene_config_t *scene);

/**
 * @brief Set the scene manager reference for the HTTP server.
 *        (Defined in http_server.c)
 */
esp_err_t http_server_set_scene_manager(scene_manager_t *manager);

#endif // SCENE_MANAGER_H
