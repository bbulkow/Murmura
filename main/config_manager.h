#ifndef CONFIG_MANAGER_H
#define CONFIG_MANAGER_H

#include "esp_err.h"
#include "http_server.h"
#include "murmura.h"
#include "cJSON.h"

// Configuration file path on SD card
#define CONFIG_FILE_PATH        "/sdcard/track_config.json"
#define CONFIG_BACKUP_PATH      "/sdcard/track_config_backup.json"

// Persisted config for a single track
typedef struct {
    track_mode_t mode;
    bool active;
    char file_path[MAX_FILE_PATH_LEN];
    int volume_percent;
    char trigger_name[MAX_TRIGGER_NAME_LEN];
    trigger_type_t trigger_type;
} track_config_entry_t;

// Full persisted configuration
typedef struct {
    track_config_entry_t tracks[MAX_TRACKS];
    int global_volume_percent;
    int device_volume_percent;  // per-device master (0-100)
    char mur_gateway_ip[MUR_GATEWAY_IP_LEN];
    int mur_gateway_port;
    char scene_trigger_name[MAX_TRIGGER_NAME_LEN];
    late_policy_t late_policy;  // policy for scheduled events that arrive late; see SYNC_DESIGN.md
    int32_t playback_offset_us; // signed per-device offset (µs) applied to target_tsf_us; see SYNC_DESIGN.md
} track_config_t;

/**
 * @brief Save current track configuration to SD card
 */
esp_err_t config_save(const track_manager_t *manager);

/**
 * @brief Load track configuration from SD card
 */
esp_err_t config_load(track_config_t *config);

/**
 * @brief Apply loaded configuration to the audio system
 */
esp_err_t config_apply(const track_config_t *config, QueueHandle_t audio_control_queue, track_manager_t *track_manager);

/**
 * @brief Check if configuration file exists
 */
bool config_exists(void);

/**
 * @brief Delete configuration file
 */
esp_err_t config_delete(void);

/**
 * @brief Create backup of current configuration
 */
esp_err_t config_backup(void);

/**
 * @brief Restore configuration from backup
 */
esp_err_t config_restore_backup(void);

/**
 * @brief Serialize manager state to JSON string (caller must free)
 */
esp_err_t config_to_json_string(const track_manager_t *manager, char **json_str);

/**
 * @brief Parse configuration from JSON string
 */
esp_err_t config_from_json_string(const char *json_str, track_config_t *config);

/**
 * @brief Get default configuration
 */
esp_err_t config_get_default(track_config_t *config);

/**
 * @brief Load configuration from file, or fall back to default
 */
esp_err_t config_load_or_default(track_config_t *config);

// --- Shared helpers (used by scene_manager.c) ---

/**
 * @brief Parse global_volume and tracks array from a cJSON object.
 *        Does NOT parse mur_gateway fields.
 */
esp_err_t config_parse_scene_from_json(cJSON *root, int *global_volume,
                                       track_config_entry_t tracks[MAX_TRACKS]);

/**
 * @brief Build a cJSON tracks array from track config entries.
 *        Caller must NOT cJSON_Delete — attach to parent.
 */
cJSON* config_tracks_to_json(const track_config_entry_t tracks[MAX_TRACKS]);

// String conversion helpers
const char* config_mode_to_str(track_mode_t mode);
track_mode_t config_str_to_mode(const char *s);
const char* config_trigger_type_to_str(trigger_type_t tt);
trigger_type_t config_str_to_trigger_type(const char *s);
const char* config_late_policy_to_str(late_policy_t lp);
late_policy_t config_str_to_late_policy(const char *s);

#endif // CONFIG_MANAGER_H
