#ifndef HTTP_SERVER_H
#define HTTP_SERVER_H

#include "esp_err.h"
#include "murmura.h"

// HTTP Server configuration
#define HTTP_SERVER_PORT 80
#define HTTP_MAX_URI_LEN 128
#define HTTP_MAX_RESP_SIZE 2048

// JSON response buffer sizes
#define JSON_BUFFER_SIZE 1024
#define MAX_FILE_PATH_LEN 64
#define MAX_TRIGGER_NAME_LEN 64
#define MUR_GATEWAY_IP_LEN 64
#define MUR_GATEWAY_DEFAULT_PORT 4000

// Track playback mode
typedef enum {
    TRACK_MODE_LOOP = 0,    // Continuously loop the file
    TRACK_MODE_TRIGGER = 1  // Play when triggered
} track_mode_t;

// Trigger sub-mode (only relevant when track_mode == TRACK_MODE_TRIGGER)
typedef enum {
    TRIGGER_MODE_MOMENTARY = 0, // Play on keyDown ("On"), stop on keyUp ("Off")
    TRIGGER_MODE_ONESHOT   = 1  // Play once on keyDown; ignore keyUp (plays to end)
} trigger_mode_t;

// Per-track runtime state
typedef struct {
    int track_index;
    track_mode_t mode;
    bool active;               // Whether this track is enabled (user intent)
    char file_path[MAX_FILE_PATH_LEN];
    int volume_percent;        // 0-100%
    char trigger_name[MAX_TRIGGER_NAME_LEN]; // trigger name to match; empty = none
    trigger_mode_t trigger_mode;             // momentary or oneshot
} track_status_t;

// Global track manager
typedef struct {
    track_status_t tracks[MAX_TRACKS];
    int global_volume_percent;  // 0-100%
    audio_stream_t *audio_stream;
    QueueHandle_t audio_control_queue;
    char mur_gateway_ip[MUR_GATEWAY_IP_LEN];       // empty = no mur gateway
    int mur_gateway_port;                           // default 4000
    char scene_trigger_name[MAX_TRIGGER_NAME_LEN]; // discrete scene trigger: value = scene name
} track_manager_t;

/**
 * @brief Initialize the HTTP server
 */
esp_err_t http_server_init(audio_stream_t *audio_stream, QueueHandle_t audio_control_queue);

/**
 * @brief Stop the HTTP server
 */
esp_err_t http_server_stop(void);

/**
 * @brief Set the track manager reference for the HTTP server
 */
esp_err_t http_server_set_track_manager(track_manager_t *manager);


/**
 * @brief Check if a track's audio pipeline is currently playing.
 *
 * Reads the decoder element state directly — single source of truth.
 * Safe to call from any task (read-only access to element state).
 */
bool is_track_playing(track_manager_t *mgr, int track_index);

#endif // HTTP_SERVER_H
