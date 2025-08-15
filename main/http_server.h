#ifndef HTTP_SERVER_H
#define HTTP_SERVER_H

#include "esp_err.h"
#include "play_sdcard.h"

// HTTP Server configuration
#define HTTP_SERVER_PORT 80
#define HTTP_MAX_URI_LEN 128 // 256
#define HTTP_MAX_RESP_SIZE 2048 // 4096

// JSON response buffer sizes
#define JSON_BUFFER_SIZE 1024 // 2048
#define MAX_FILE_PATH_LEN 64 // 256

// Loop status structure for tracking
typedef struct {
    bool is_playing;
    char file_path[MAX_FILE_PATH_LEN];
    int volume_percent;  // 0-100%
    int track_index;
} loop_status_t;

// Global loop manager structure
typedef struct {
    loop_status_t loops[MAX_TRACKS];
    int global_volume_percent;  // 0-100%
    audio_stream_t *audio_stream;
    QueueHandle_t audio_control_queue;
} loop_manager_t;

/**
 * @brief Initialize the HTTP server
 * 
 * @param audio_stream Pointer to the audio stream structure
 * @param audio_control_queue Queue for sending audio control messages
 * @return esp_err_t ESP_OK on success
 */
esp_err_t http_server_init(audio_stream_t *audio_stream, QueueHandle_t audio_control_queue);

/**
 * @brief Stop the HTTP server
 * 
 * @return esp_err_t ESP_OK on success
 */
esp_err_t http_server_stop(void);

/**
 * @brief Get the current status of all loops
 * 
 * @param manager Pointer to loop manager structure to populate
 * @return esp_err_t ESP_OK on success
 */
esp_err_t http_server_get_loop_status(loop_manager_t *manager);

/**
 * @brief Set the loop manager reference for the HTTP server
 * 
 * @param manager Pointer to the loop manager maintained by audio_control_task
 * @return esp_err_t ESP_OK on success
 */
esp_err_t http_server_set_loop_manager(loop_manager_t *manager);

#endif // HTTP_SERVER_H
