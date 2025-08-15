#ifndef CONFIG_MANAGER_H
#define CONFIG_MANAGER_H

#include "esp_err.h"
#include "http_server.h"
#include "play_sdcard.h"

// Configuration file path on SD card
#define CONFIG_FILE_PATH "/sdcard/loop_config.json"
#define CONFIG_BACKUP_PATH "/sdcard/loop_config_backup.json"

// Configuration structure that matches loop_manager_t
typedef struct {
    struct {
        bool is_playing;
        char file_path[MAX_FILE_PATH_LEN];
        int volume_percent;
    } loops[MAX_TRACKS];
    int global_volume_percent;
} loop_config_t;

/**
 * @brief Save current loop configuration to SD card
 * 
 * @param manager Pointer to loop manager with current configuration
 * @return esp_err_t ESP_OK on success
 */
esp_err_t config_save(const loop_manager_t *manager);

/**
 * @brief Load loop configuration from SD card
 * 
 * @param config Pointer to configuration structure to populate
 * @return esp_err_t ESP_OK on success, ESP_ERR_NOT_FOUND if file doesn't exist
 */
esp_err_t config_load(loop_config_t *config);

/**
 * @brief Apply loaded configuration to the audio system
 * 
 * @param config Configuration to apply
 * @param audio_control_queue Queue for sending audio control messages
 * @param loop_manager Loop manager to update
 * @return esp_err_t ESP_OK on success
 */
esp_err_t config_apply(const loop_config_t *config, QueueHandle_t audio_control_queue, loop_manager_t *loop_manager);

/**
 * @brief Check if configuration file exists
 * 
 * @return true if configuration file exists
 */
bool config_exists(void);

/**
 * @brief Delete configuration file
 * 
 * @return esp_err_t ESP_OK on success
 */
esp_err_t config_delete(void);

/**
 * @brief Create backup of current configuration
 * 
 * @return esp_err_t ESP_OK on success
 */
esp_err_t config_backup(void);

/**
 * @brief Restore configuration from backup
 * 
 * @return esp_err_t ESP_OK on success
 */
esp_err_t config_restore_backup(void);

/**
 * @brief Get configuration as JSON string
 * 
 * @param manager Loop manager with current configuration
 * @param json_str Pointer to store allocated JSON string (must be freed by caller)
 * @return esp_err_t ESP_OK on success
 */
esp_err_t config_to_json_string(const loop_manager_t *manager, char **json_str);

/**
 * @brief Parse configuration from JSON string
 * 
 * @param json_str JSON string to parse
 * @param config Configuration structure to populate
 * @return esp_err_t ESP_OK on success
 */
esp_err_t config_from_json_string(const char *json_str, loop_config_t *config);

/**
 * @brief Get default configuration
 * 
 * @param config Configuration structure to populate with defaults
 * @return esp_err_t ESP_OK on success
 */
esp_err_t config_get_default(loop_config_t *config);

/**
 * @brief Load configuration from file or use default
 * 
 * @param config Configuration structure to populate
 * @return esp_err_t ESP_OK on success (either from file or default)
 */
esp_err_t config_load_or_default(loop_config_t *config);

#endif // CONFIG_MANAGER_H
