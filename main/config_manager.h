#ifndef CONFIG_MANAGER_H
#define CONFIG_MANAGER_H

#include "esp_err.h"
#include "http_server.h"
#include "murmura.h"

// Configuration file path on SD card
#define CONFIG_FILE_PATH        "/sdcard/track_config.json"
#define CONFIG_BACKUP_PATH      "/sdcard/track_config_backup.json"

// Persisted config for a single track
typedef struct {
    track_mode_t mode;
    bool active;
    char file_path[MAX_FILE_PATH_LEN];
    int volume_percent;
} track_config_entry_t;

// Full persisted configuration
typedef struct {
    track_config_entry_t tracks[MAX_TRACKS];
    int global_volume_percent;
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

#endif // CONFIG_MANAGER_H
