#ifndef PLAY_SDCARD_H
#define PLAY_SDCARD_H

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "esp_vfs_fat.h"
#include "sys/stat.h"
#include "nvs_flash.h"
#include "sdkconfig.h"
#include "audio_element.h"
#include "audio_pipeline.h"
#include "audio_event_iface.h"
#include "audio_common.h"
#include "fatfs_stream.h"
#include "i2s_stream.h"
#include "downmix.h"

// we want a set of decoders not just a single configured one
#include "esp_decoder.h"   // audio decoder
#include "esp_audio.h"
#include "mp3_decoder.h"
#include "wav_decoder.h"
#include "equalizer.h" // to cover the sins of bad speaker design if necessary

#include "esp_peripherals.h"
#include "periph_touch.h"
#include "periph_adc_button.h"
#include "periph_button.h"
#include "periph_sdcard.h"
#include "board.h"

// filesystem
#include <string.h>
#include <stdio.h>
#include <dirent.h>
#include <errno.h>
#include <sys/stat.h>

// Audio stream:
// It's the full collection of player pipelines

#define MAX_TRACKS 3

typedef struct {
    audio_pipeline_handle_t pipeline;
    audio_element_handle_t fatfs_e;
    audio_element_handle_t decode_e;
    audio_element_handle_t raw_write_e;  // Raw stream passthrough element
} audio_track_t;

typedef struct 
{
    audio_pipeline_handle_t pipeline; // "output" pipeline has downmix and I2S in it
    audio_element_handle_t downmix_e;
    audio_element_handle_t i2s_e;
    audio_track_t tracks[MAX_TRACKS];
} audio_stream_t;

// some globals here are probably the best way to deal
typedef enum {
    AUDIO_ACTION_START,
    AUDIO_ACTION_NEXT_TRACK,
    AUDIO_ACTION_PLAY_PAUSE
    // Add other audio control actions as needed
} audio_action_type_t;

typedef struct {
    audio_action_type_t type;
    void *data; // usually a str or struct based on the action

} audio_control_msg_t;

// Debug function declarations
void debug_audio_event(audio_event_iface_msg_t *msg);
void audio_control_start_debug(audio_stream_t *stream);
void audio_control_start_debug_v2(audio_stream_t *stream);
void debug_ringbuffer_connections(audio_stream_t *stream);
void debug_element_configs(audio_stream_t *stream);
void debug_downmix_element(audio_stream_t *stream);

// Alternative initialization with passthrough elements
esp_err_t audio_stream_init_with_passthrough(audio_stream_t **stream_o);

#endif // PLAY_SDCARD_H
