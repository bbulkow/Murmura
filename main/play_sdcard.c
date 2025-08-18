/* Based on the esp-adf play_sdcard_music_example,
   But re-written as a looper. The goal is:
   * multiple simultaneous loops
   * each with dynamic gain controls
   * the ability to add independing, or group, eq
   * the ability to change each loop file while the others continue to run

   Assisted by Anthropic Claude Opus 4, using the Cline VS code plugin
    Author: Brian Bulkowski brian@bulkowski.org


   Unless required by applicable law or agreed to in writing, this
   software is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
   CONDITIONS OF ANY KIND, either express or implied.
*/

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

#include "play_sdcard.h"
#include "music_files.h"
#include "wifi_manager.h"
#include "http_server.h"
#include "config_manager.h"
#include <math.h>  // For log10f

static const char *TAG = "PLAY_SDCARD";


// audio element status strings, useful for debugging, see audio_element.h for enum
#if 0
static const char *AEL_COMMAND_STRINGS[] = {
    "NONE",
    "ERROR", /* depricated */
    "FINISH",
    "STOP",
    "PAUSE",
    "RESUME",
    "DESTROY",
    "CHANGE_STATE",
    "REPORT_STATUS",
    "REPORT_MUSIC_INFO",
    "REPORT_CODEC_FMT",
    "REPORT_POSITION"
};

static const char *AEL_STATUS_STRINGS[] = {
    "NONE",
    "ERROR_OPEN",
    "ERROR_INPUT",
    "ERROR_PROCESS",
    "ERROR_OUTPUT",
    "ERROR_CLOSE",
    "ERROR_TIMEOUT",
    "ERROR_UNKNOWN",
    "INPUT_DONE",
    "INPUT_BUFFERING",
    "OUTPUT_DONE",
    "OUTPUT_BUFFERING",
    "STATE_RUNNING",
    "STATE_PAUSED",
    "STATE_STOPPED",
    "STATE_FINISHED",
    "MOUNTED",
    "UNMOUNTED"
};
#endif


esp_err_t audio_stream_init(audio_stream_t **stream_o) {
    ESP_LOGD(TAG, "Initializing audio stream with downmix");
    
    audio_stream_t *stream = calloc(1, sizeof(audio_stream_t));
    if (!stream) {
        ESP_LOGE(TAG, "Failed to allocate memory for audio stream");
        return ESP_FAIL;
    }

    // Create output pipeline first
    audio_pipeline_cfg_t pipeline_cfg = DEFAULT_AUDIO_PIPELINE_CONFIG();
    stream->pipeline = audio_pipeline_init(&pipeline_cfg);
    if (!stream->pipeline) {
        ESP_LOGE(TAG, "Failed to create output pipeline");
        free(stream);
        return ESP_FAIL;
    }

    // Create downmix element
    downmix_cfg_t downmix_cfg = DEFAULT_DOWNMIX_CONFIG();
    downmix_cfg.downmix_info.source_num = MAX_TRACKS;
    downmix_cfg.downmix_info.output_type = ESP_DOWNMIX_OUTPUT_TYPE_TWO_CHANNEL;
    downmix_cfg.downmix_info.mode = ESP_DOWNMIX_WORK_MODE_SWITCH_ON;
    downmix_cfg.downmix_info.out_ctx = ESP_DOWNMIX_OUT_CTX_NORMAL;
    
    stream->downmix_e = downmix_init(&downmix_cfg);
    if (!stream->downmix_e) {
        ESP_LOGE(TAG, "Failed to create downmix element");
        audio_pipeline_deinit(stream->pipeline);
        free(stream);
        return ESP_FAIL;
    }

    // I2S output
    i2s_stream_cfg_t i2s_cfg = I2S_STREAM_CFG_DEFAULT();
    i2s_cfg.type = AUDIO_STREAM_WRITER;
    // ???
    // i2s_cfg.i2s_config.sample_rate = 44100;
    // i2s_cfg.i2s_config.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
    // i2s_cfg.i2s_config.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
    // i2s_cfg.i2s_config.communication_format = I2S_COMM_FORMAT_STAND_I2S;
    // i2s_cfg.i2s_config.intr_alloc_flags = ESP_INTR_FLAG_LEVEL2;
    // i2s_cfg.i2s_config.dma_buf_count = 3;
    // i2s_cfg.i2s_config.dma_buf_len = 300;
    // i2s_cfg.i2s_port = I2S_NUM_0;
    stream->i2s_e = i2s_stream_init(&i2s_cfg);
    if (!stream->i2s_e) {
        ESP_LOGE(TAG, "Failed to create i2s element");
        audio_element_deinit(stream->downmix_e);
        audio_pipeline_deinit(stream->pipeline);
        free(stream);
        return ESP_FAIL;
    }
    audio_element_info_t music_info = {0};
    music_info.sample_rates = 44100;
    music_info.bits = 16;
    music_info.channels = 2;
    audio_element_setinfo(stream->i2s_e, &music_info);
    i2s_stream_set_clk(stream->i2s_e, music_info.sample_rates, music_info.bits, music_info.channels);
      

    // Register downmix and I2S in output pipeline
    audio_pipeline_register(stream->pipeline, stream->downmix_e, "downmix");
    audio_pipeline_register(stream->pipeline, stream->i2s_e, "i2s");
    
    // Link downmix to I2S
    const char *link_tag[2] = {"downmix", "i2s"};
    audio_pipeline_link(stream->pipeline, link_tag, 2);

    // Initialize source info for downmix
    esp_downmix_input_info_t source_info[MAX_TRACKS];
    for (int i = 0; i < MAX_TRACKS; i++) {
        source_info[i].samplerate = 44100;  // Will be updated when track starts
        source_info[i].channel = 2;         // Will be updated when track starts
        source_info[i].bits_num = 16;
        source_info[i].gain[0] = 0.0f;     // Original gain (bypass mode)
        source_info[i].gain[1] = 0.0f;     // Target gain (mix mode) - 0dB
        source_info[i].transit_time = 500;  // 500ms transition time
    }
    source_info_init(stream->downmix_e, source_info);

    // Create track pipelines
    for (int i = 0; i < MAX_TRACKS; i++) {
        // Create pipeline for this track
        audio_pipeline_cfg_t track_pipeline_cfg = DEFAULT_AUDIO_PIPELINE_CONFIG();
        stream->tracks[i].pipeline = audio_pipeline_init(&track_pipeline_cfg);
        if (!stream->tracks[i].pipeline) {
            ESP_LOGE(TAG, "Failed to create pipeline for track %d", i);
            // Cleanup would be needed here
            return ESP_FAIL;
        }
        
        // Create fatfs reader
        fatfs_stream_cfg_t fatfs_cfg = FATFS_STREAM_CFG_DEFAULT();
        fatfs_cfg.type = AUDIO_STREAM_READER;
        fatfs_cfg.task_core = 1;  // Run on APP CPU (core 1) to avoid WiFi conflicts
        stream->tracks[i].fatfs_e = fatfs_stream_init(&fatfs_cfg);
        
#if 0
        // Create decoder with auto-detection for multiple formats
        audio_decoder_t auto_decode[] = {
            DEFAULT_ESP_MP3_DECODER_CONFIG(),
            DEFAULT_ESP_WAV_DECODER_CONFIG(),
            DEFAULT_ESP_AAC_DECODER_CONFIG(),
            DEFAULT_ESP_M4A_DECODER_CONFIG(),
        };
        esp_decoder_cfg_t auto_dec_cfg = DEFAULT_ESP_DECODER_CONFIG();
        stream->tracks[i].decode_e = esp_decoder_init(&auto_dec_cfg, auto_decode, 10);
#else
        ESP_LOGD(TAG, "[3.4] Create wav decoder");
        wav_decoder_cfg_t  wav_dec_cfg  = DEFAULT_WAV_DECODER_CONFIG();
        wav_dec_cfg.task_core = 1;
        wav_dec_cfg.task_prio = 20;
        stream->tracks[i].decode_e = wav_decoder_init(&wav_dec_cfg);
#endif

        // Register elements in track pipeline
        char tag_file[16], tag_dec[16];
        snprintf(tag_file, sizeof(tag_file), "file_%d", i);
        snprintf(tag_dec, sizeof(tag_dec), "dec_%d", i);
        
        audio_pipeline_register(stream->tracks[i].pipeline, stream->tracks[i].fatfs_e, tag_file);
        audio_pipeline_register(stream->tracks[i].pipeline, stream->tracks[i].decode_e, tag_dec);

        // Link track pipeline
        const char *track_link[2] = {tag_file, tag_dec};
        audio_pipeline_link(stream->tracks[i].pipeline, track_link, 2);
        
        // IMPORTANT: Don't connect to downmix here - decoder output ringbuffers don't exist yet!
        // We'll connect them after pipeline initialization
    }

    *stream_o = stream;
    ESP_LOGD(TAG, "Audio stream initialized successfully with downmix");
    return ESP_OK;
}

// This function is now replaced with the debug version from play_sdcard_debug.c
void audio_control_start(audio_stream_t *stream) {
    // Use the enhanced debug version to diagnose the audio playback issue
    audio_control_start_debug_v2(stream);
    
    // Also call the downmix debug function after a short delay
    vTaskDelay(1000 / portTICK_PERIOD_MS);
    debug_downmix_element(stream);
}

void audio_control_set_gain(audio_stream_t *stream, int track_index, float gain_db) {
    if (track_index < 0 || track_index >= MAX_TRACKS) {
        ESP_LOGE(TAG, "Invalid track index: %d", track_index);
        return;
    }
    
    float gain[2] = {0.0f, gain_db};
    downmix_set_gain_info(stream->downmix_e, gain, track_index);
    ESP_LOGD(TAG, "Set track %d gain to %.1f dB", track_index, gain_db);
}

void audio_control_start_track(audio_stream_t *stream, int track_index) {
    if (track_index < 0 || track_index >= MAX_TRACKS) {
        ESP_LOGE(TAG, "Invalid track index: %d", track_index);
        return;
    }

    ESP_LOGD(TAG, "Starting track %d", track_index);
    audio_pipeline_run(stream->tracks[track_index].pipeline);
    ESP_LOGD(TAG, "Started track %d", track_index);
}

void audio_control_stop_track(audio_stream_t *stream, int track_index) {
    if (track_index < 0 || track_index >= MAX_TRACKS) {
        ESP_LOGE(TAG, "Invalid track index: %d", track_index);
        return;
    }
    
    ESP_LOGD(TAG, "Stoping track %d", track_index);
    audio_pipeline_stop(stream->tracks[track_index].pipeline);
    audio_pipeline_wait_for_stop(stream->tracks[track_index].pipeline);
    audio_pipeline_terminate(stream->tracks[track_index].pipeline);
    ESP_LOGD(TAG, "Stopped track %d", track_index);
}

void audio_control_stop(audio_stream_t *stream) {
    ESP_LOGI(TAG, "Stopping audio control");
    
    // Stop all track pipelines
    for (int i = 0; i < MAX_TRACKS; i++) {
        audio_pipeline_stop(stream->tracks[i].pipeline);
        audio_pipeline_wait_for_stop(stream->tracks[i].pipeline);
        audio_pipeline_terminate(stream->tracks[i].pipeline);
    }
    
    // Stop output pipeline
    audio_pipeline_stop(stream->pipeline);
    audio_pipeline_wait_for_stop(stream->pipeline);
    audio_pipeline_terminate(stream->pipeline);
}

void audio_stream_deinit(audio_stream_t *stream) {
    if (!stream) return;
    
    // Deinit track pipelines
    for (int i = 0; i < MAX_TRACKS; i++) {
        if (stream->tracks[i].pipeline) {
            audio_pipeline_unregister_more(stream->tracks[i].pipeline, 
                                         stream->tracks[i].fatfs_e,
                                         stream->tracks[i].decode_e, NULL);
            audio_pipeline_deinit(stream->tracks[i].pipeline);
        }
        if (stream->tracks[i].fatfs_e) {
            audio_element_deinit(stream->tracks[i].fatfs_e);
        }
        if (stream->tracks[i].decode_e) {
            audio_element_deinit(stream->tracks[i].decode_e);
        }
    }
    
    // Deinit output pipeline
    if (stream->pipeline) {
        audio_pipeline_unregister_more(stream->pipeline, 
                                     stream->downmix_e,
                                     stream->i2s_e, NULL);
        audio_pipeline_deinit(stream->pipeline);
    }
    
    if (stream->downmix_e) {
        audio_element_deinit(stream->downmix_e);
    }
    if (stream->i2s_e) {
        audio_element_deinit(stream->i2s_e);
    }
    
    free(stream);
}

typedef struct {
    QueueHandle_t queue;
    audio_event_iface_handle_t evt;
    audio_board_handle_t board_handle;
} audio_control_parameters_t;


void audio_control_task(void *pvParameters)
{
    audio_control_parameters_t *params = (audio_control_parameters_t *)pvParameters;
    QueueHandle_t control_queue = params->queue;

    //(QueueHandle_t) pvParameters;
    ESP_LOGI(TAG, "Audio control task started.");

    ESP_LOGI(TAG, "audio_control: create stream");

    audio_stream_t *stream;
    // Use the passthrough approach to fix decoder output issue
    audio_stream_init_with_passthrough(&stream);
    
    // Initialize loop tracking state
    loop_manager_t *loop_manager = heap_caps_calloc(1, sizeof(loop_manager_t), MALLOC_CAP_SPIRAM);
    if (!loop_manager) {
        ESP_LOGE(TAG, "Failed to allocate loop manager");
        return;
    }
    loop_manager->audio_stream = stream;
    loop_manager->audio_control_queue = control_queue;
    loop_manager->global_volume_percent = 75;  // Default volume 75%
    for (int i = 0; i < MAX_TRACKS; i++) {
        loop_manager->loops[i].is_playing = false;
        loop_manager->loops[i].volume_percent = 100;  // Default to 100% (0dB)
        loop_manager->loops[i].track_index = i;
    }

    ESP_LOGI(TAG, "audio_control: Initialize HTTP server");
    // Initialize HTTP server for remote control
    esp_err_t http_ret = http_server_init(stream, control_queue);
    if (http_ret == ESP_OK) {
        ESP_LOGI(TAG, "HTTP server initialized successfully");
        ESP_LOGI(TAG, "Access the API documentation at http://<device-ip>/");
        // Update HTTP server with loop manager reference
        http_server_set_loop_manager(loop_manager);
    } else {
        ESP_LOGW(TAG, "Failed to initialize HTTP server: %s", esp_err_to_name(http_ret));
    }
    
    ESP_LOGI(TAG, "audio_control: Load configuration (from file or default)");
    
    // Load configuration FIRST - either from file or use default
    loop_config_t startup_config;
    if (config_load_or_default(&startup_config) == ESP_OK) {
        ESP_LOGI(TAG, "Configuration loaded:");
        ESP_LOGI(TAG, "  Global volume: %d%%", startup_config.global_volume_percent);
        for (int i = 0; i < MAX_TRACKS; i++) {
            if (strlen(startup_config.loops[i].file_path) > 0) {
                ESP_LOGI(TAG, "  Track %d: %s (volume=%d%%, playing=%s)", 
                         i, startup_config.loops[i].file_path, 
                         startup_config.loops[i].volume_percent,
                         startup_config.loops[i].is_playing ? "yes" : "no");
            }
        }
        
        // Start the audio system infrastructure (output pipeline only)
        ESP_LOGI(TAG, "Starting audio system infrastructure...");
        
        // Send START message to initialize audio infrastructure
        audio_control_msg_t start_msg = {
            .type = AUDIO_ACTION_START,
            .data = {}
        };
        xQueueSend(control_queue, &start_msg, portMAX_DELAY);
        
        // Wait for audio system to be ready
        vTaskDelay(1000 / portTICK_PERIOD_MS);
        
        // Apply the configuration through the message queue (thread-safe)
        ESP_LOGI(TAG, "Applying configuration through message queue...");
        if (config_apply(&startup_config, control_queue, loop_manager) == ESP_OK) {
            ESP_LOGI(TAG, "Configuration messages sent successfully");
        } else {
            ESP_LOGW(TAG, "Failed to send some configuration messages");
        }
    } else {
        ESP_LOGW(TAG, "Failed to load configuration, starting with empty tracks");
        // Start audio infrastructure anyway but with no tracks
        audio_control_msg_t start_msg = {
            .type = AUDIO_ACTION_START,
            .data = {}
        };
        xQueueSend(control_queue, &start_msg, portMAX_DELAY);
    }

    ESP_LOGI(TAG, "audio_control: start listener");

    // Listen to all track pipelines and output pipeline
    audio_pipeline_set_listener(stream->pipeline, params->evt);
    for (int i = 0; i < MAX_TRACKS; i++) {
        audio_pipeline_set_listener(stream->tracks[i].pipeline, params->evt);
    }

    audio_control_msg_t msg;
    bool audio_started = false;
    
    while (1) {
        // Check for control messages with a short timeout
        if (xQueueReceive(control_queue, &msg, pdMS_TO_TICKS(10)) == pdPASS) {
            ESP_LOGI(TAG, "Received control action: %d", msg.type);

            switch (msg.type) {
                case AUDIO_ACTION_START:
                    ESP_LOGI(TAG, "Processing START action...");
                    audio_control_start(stream);
                    audio_started = true;
                    // Note: Tracks are now started by config_apply() during initialization
                    // This action just starts the audio system infrastructure
                    break;

                case AUDIO_ACTION_START_TRACK: {
                    ESP_LOGI(TAG, "Processing START_TRACK action for track %d", msg.data.start_track.track_index);
                    int track = msg.data.start_track.track_index;
                    if (track >= 0 && track < MAX_TRACKS) {
                        // Stop track if already playing
                        audio_pipeline_stop(stream->tracks[track].pipeline);
                        audio_pipeline_wait_for_stop(stream->tracks[track].pipeline);
                        audio_pipeline_reset_ringbuffer(stream->tracks[track].pipeline);
                        audio_pipeline_reset_elements(stream->tracks[track].pipeline);
                        
                        // Set new file path
                        audio_element_set_uri(stream->tracks[track].fatfs_e, msg.data.start_track.file_path);
                        
                        // Start the track
                        audio_pipeline_run(stream->tracks[track].pipeline);
                        ESP_LOGI(TAG, "Started track %d with file: %s", track, msg.data.start_track.file_path);
                        
                        // Update loop manager state
                        loop_manager->loops[track].is_playing = true;
                        strncpy(loop_manager->loops[track].file_path, msg.data.start_track.file_path, 
                                sizeof(loop_manager->loops[track].file_path) - 1);
                    }
                    break;
                }

                case AUDIO_ACTION_STOP_TRACK: {
                    ESP_LOGI(TAG, "Processing STOP_TRACK action for track %d", msg.data.stop_track.track_index);
                    int track = msg.data.stop_track.track_index;
                    if (track >= 0 && track < MAX_TRACKS) {
                        audio_pipeline_stop(stream->tracks[track].pipeline);
                        audio_pipeline_wait_for_stop(stream->tracks[track].pipeline);
                        audio_pipeline_terminate(stream->tracks[track].pipeline);
                        ESP_LOGI(TAG, "Stopped track %d", track);
                        
                        // Update loop manager state - only change playing state, preserve file path
                        loop_manager->loops[track].is_playing = false;
                        // Note: We intentionally preserve file_path so track can be restarted
                    }
                    break;
                }

                case AUDIO_ACTION_SET_VOLUME: {
                    ESP_LOGI(TAG, "Processing SET_VOLUME action for track %d: %d%%", 
                             msg.data.set_volume.track_index, msg.data.set_volume.volume_percent);
                    int track = msg.data.set_volume.track_index;
                    if (track >= 0 && track < MAX_TRACKS) {
                        int volume = msg.data.set_volume.volume_percent;
                        if (volume < 0) volume = 0;
                        if (volume > 100) volume = 100;
                        
                        // Convert volume percent to dB gain
                        // 100% = 0dB, 50% = -6dB, 25% = -12dB, 0% = -60dB
                        float gain_db;
                        if (volume == 0) {
                            gain_db = -60.0f;  // Effectively mute
                        } else {
                            gain_db = 20.0f * log10f(volume / 100.0f);
                        }
                        
                        float gain[2] = {0.0f, gain_db};
                        downmix_set_gain_info(stream->downmix_e, gain, track);
                        ESP_LOGI(TAG, "Set track %d volume to %d%% (%.1f dB)", track, volume, gain_db);
                        
                        // Update loop manager state
                        loop_manager->loops[track].volume_percent = volume;
                    }
                    break;
                }

                case AUDIO_ACTION_SET_GLOBAL_VOLUME: {
                    ESP_LOGI(TAG, "Processing SET_GLOBAL_VOLUME action: %d%%", msg.data.set_global_volume.volume_percent);
                    int volume = msg.data.set_global_volume.volume_percent;
                    if (volume < 0) volume = 0;
                    if (volume > 100) volume = 100;
                    
                    // Update loop manager state
                    loop_manager->global_volume_percent = volume;
                    
                    // Actually set the hardware volume using the board handle
                    if (params->board_handle && params->board_handle->audio_hal) {
                        audio_hal_set_volume(params->board_handle->audio_hal, volume);
                        ESP_LOGI(TAG, "Global volume set to %d%% (hardware codec updated)", volume);
                    } else {
                        ESP_LOGW(TAG, "Global volume set to %d%% (no board handle available)", volume);
                    }
                    break;
                }

                case AUDIO_ACTION_NEXT_TRACK:
                    ESP_LOGI(TAG, "Processing NEXT_TRACK action...");
                    // For next track, you would:
                    // 1. Stop current track
                    // 2. Update the URI
                    // 3. Restart the pipeline
                    // This is simplified - you'd need to track which track to play next
                    audio_control_stop(stream);
                    // Update URIs here based on your playlist logic
                    audio_control_start(stream);
                    break;

                case AUDIO_ACTION_PLAY_PAUSE:
                    ESP_LOGI(TAG, "Processing PLAY_PAUSE action...");
                    // Implement play/pause logic here
                    break;

                default:
                    ESP_LOGW(TAG, "Unknown audio action type: %d", msg.type);
                    break;
            }
        }
        
        // Also check for audio events if audio has been started
        if (audio_started) {
            audio_event_iface_msg_t evt_msg;
            esp_err_t evt_ret = audio_event_iface_listen(params->evt, &evt_msg, 0);
            
            // Check element states for looping even if no event received
            static int loop_check_counter = 0;
            static bool track_finished[MAX_TRACKS] = {false, false, false};
            loop_check_counter++;
            
            for (int i = 0; i < MAX_TRACKS; i++) {
                // Check all element states
                audio_element_state_t fatfs_state = audio_element_get_state(stream->tracks[i].fatfs_e);
                audio_element_state_t decode_state = audio_element_get_state(stream->tracks[i].decode_e);
                audio_element_state_t raw_state = audio_element_get_state(stream->tracks[i].raw_write_e);
                
                // Check file position to detect end of file
                audio_element_info_t info;
                audio_element_getinfo(stream->tracks[i].fatfs_e, &info);
                bool at_end = (info.byte_pos >= info.total_bytes - 1024) && (info.total_bytes > 0);
                
                // Log states periodically or when at end of file
                // if (loop_check_counter % 50 == 0 || at_end) {  // Every ~0.5 second or at EOF
                //     ESP_LOGI(TAG, "Track %d: fatfs=%d, decode=%d, raw=%d, pos=%lld/%lld", 
                //              i, fatfs_state, decode_state, raw_state, info.byte_pos, info.total_bytes);
                // }
                
                // Detect when track has finished playing
                if (at_end && !track_finished[i]) {
                    track_finished[i] = true;
                    ESP_LOGI(TAG, "Track %d reached end of file, marking for restart", i);
                }
                
                // Restart track if it's finished and pipeline is no longer running
                if (track_finished[i] && (fatfs_state != AEL_STATE_RUNNING || decode_state != AEL_STATE_RUNNING)) {
                    ESP_LOGI(TAG, "Track %d finished and stopped, restarting for loop", i);
                    
                    // Stop the pipeline completely
                    audio_pipeline_stop(stream->tracks[i].pipeline);
                    audio_pipeline_wait_for_stop(stream->tracks[i].pipeline);
                    
                    // Reset pipeline
                    audio_pipeline_reset_ringbuffer(stream->tracks[i].pipeline);
                    audio_pipeline_reset_elements(stream->tracks[i].pipeline);
                    
                    // Re-set the URI (keep the same file that was playing)
                    const char *current_file = loop_manager->loops[i].file_path;
                    // Only restart if there's actually a file configured
                    if (strlen(current_file) > 0) {
                        audio_element_set_uri(stream->tracks[i].fatfs_e, current_file);
                        
                        // Restart pipeline
                        audio_pipeline_run(stream->tracks[i].pipeline);
                        
                        track_finished[i] = false;  // Reset the flag
                        ESP_LOGI(TAG, "Track %d restarted with file: %s", i, current_file);
                    } else {
                        // No file configured for this track, don't restart
                        track_finished[i] = false;  // Reset the flag anyway
                        ESP_LOGW(TAG, "Track %d finished but no file configured, not restarting", i);
                    }
                }
            }
            
            if (evt_ret == ESP_OK) {
                // Use the debug function to log the event
                debug_audio_event(&evt_msg);
                
                // Identify which element sent the event
                for (int i = 0; i < MAX_TRACKS; i++) {
                    if (evt_msg.source == (void *)stream->tracks[i].fatfs_e) {
                    ESP_LOGD(TAG, "Event from track %d FATFS element", i);
                } else if (evt_msg.source == (void *)stream->tracks[i].decode_e) {
                    ESP_LOGD(TAG, "Event from track %d decoder element", i);
                } else if (evt_msg.source == (void *)stream->tracks[i].raw_write_e) {
                    ESP_LOGD(TAG, "Event from track %d raw_write element", i);
                    }
                }
                
                if (evt_msg.source == (void *)stream->downmix_e) {
                ESP_LOGD(TAG, "Event from downmix element");
            } else if (evt_msg.source == (void *)stream->i2s_e) {
                ESP_LOGD(TAG, "Event from I2S element");
                }
                
                // Handle specific important events
                if (evt_msg.cmd == AEL_MSG_CMD_REPORT_STATUS) {
                    if ((int)evt_msg.data == AEL_STATUS_ERROR_OPEN) {
                        ESP_LOGE(TAG, "Error opening file or element!");
                    } else if ((int)evt_msg.data == AEL_STATUS_ERROR_INPUT) {
                        ESP_LOGE(TAG, "Error reading input!");
                    } else if ((int)evt_msg.data == AEL_STATUS_ERROR_PROCESS) {
                        ESP_LOGE(TAG, "Error processing audio!");
                    } else if ((int)evt_msg.data == AEL_STATUS_STATE_FINISHED) {
                        ESP_LOGI(TAG, "Track finished (STATE_FINISHED)");
                    }
                }
                
                // Handle track looping when pipeline finishes
                // Check for AEL_STATUS_STATE_FINISHED or when raw_write gets input done
                if (evt_msg.cmd == AEL_MSG_CMD_REPORT_STATUS) {
                    // Check which track finished
                    for (int i = 0; i < MAX_TRACKS; i++) {
                        bool should_restart = false;
                        
                        // Check if it's from any element in this track
                        if (evt_msg.source == (void *)stream->tracks[i].fatfs_e ||
                            evt_msg.source == (void *)stream->tracks[i].decode_e ||
                            evt_msg.source == (void *)stream->tracks[i].raw_write_e) {
                            
                            // Check for various finish conditions
                            if ((int)evt_msg.data == AEL_STATUS_STATE_FINISHED ||
                                (int)evt_msg.data == AEL_STATUS_INPUT_DONE) {
                                should_restart = true;
                                ESP_LOGI(TAG, "Track %d element reported finish (status=%d)", i, (int)evt_msg.data);
                            }
                        }
                        
                        if (should_restart) {
                            ESP_LOGI(TAG, "Track %d finished, restarting for loop", i);
                            
                            // Stop the pipeline first
                            audio_pipeline_stop(stream->tracks[i].pipeline);
                            audio_pipeline_wait_for_stop(stream->tracks[i].pipeline);
                            
                            // Reset pipeline state
                            audio_pipeline_reset_ringbuffer(stream->tracks[i].pipeline);
                            audio_pipeline_reset_elements(stream->tracks[i].pipeline);
                            
                            // Restart the pipeline
                            audio_pipeline_run(stream->tracks[i].pipeline);
                            
                            ESP_LOGI(TAG, "Track %d restarted", i);
                            break;
                        }
                    }
                }
            }
        }
    }
    
    // Clean up before exit (though this is unreachable in current implementation)
    audio_stream_deinit(stream);
    vTaskDelete(NULL);
}



void app_main(void)
{
    esp_log_level_set("*", ESP_LOG_INFO);
    esp_log_level_set(TAG, ESP_LOG_DEBUG);
    esp_log_level_set("DOWNMIX", ESP_LOG_DEBUG);
    
    // Reduce log spew from ESP-ADF components
    esp_log_level_set("AUDIO_ELEMENT", ESP_LOG_ERROR);
    esp_log_level_set("AUDIO_PIPELINE", ESP_LOG_ERROR);
    esp_log_level_set("WAV_DECODER", ESP_LOG_ERROR);
    esp_log_level_set("FATFS_STREAM", ESP_LOG_ERROR);
    esp_log_level_set("CODEC_ELEMENT_HELPER", ESP_LOG_ERROR); 
    esp_log_level_set("DEC_WAV", ESP_LOG_ERROR);

    // wifis a little chatty too
    esp_log_level_set("wifi", ESP_LOG_WARN);


    ESP_LOGI(TAG, "[ 0 ] Create control queue and start audio control task");
    // Create a queue to handle audio control messages
    QueueHandle_t audio_control_queue = xQueueCreate(10, sizeof(audio_control_msg_t));
    if (audio_control_queue == NULL) {
        ESP_LOGE(TAG, "Failed to create audio control queue");
        return;
    }


    ESP_LOGI(TAG, "[ 1 ] Initialize NVS and mount SD card");
    // Initialize NVS
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    
    ESP_LOGI(TAG, "[ 1.5 ] Initialize WiFi manager");
    
    // Check if WiFi networks are already stored to avoid unnecessary NVS writes
    wifiman_config_t existing_config;
    esp_err_t read_ret = wifi_manager_read_credentials(&existing_config);
    
    if (read_ret != ESP_OK || existing_config.network_count == 0) {
        // No networks stored yet, add them for the first time
        ESP_LOGI(TAG, "No WiFi networks found in NVS, adding initial networks...");
        wifi_manager_add_network("medea", "!medea4u");
        // wifi_manager_add_network("YourOfficeWiFi", "YourOfficePassword");
        // wifi_manager_add_network("YourMobileHotspot", "YourHotspotPassword");
        ESP_LOGI(TAG, "WiFi networks stored in NVS");
    } else {
        ESP_LOGI(TAG, "Found %d existing WiFi networks in NVS, skipping add", existing_config.network_count);
        // List existing networks for debug
        bool has_auth_failures = false;
        for (int i = 0; i < existing_config.network_count; i++) {
            ESP_LOGI(TAG, "  Network %d: %s (Auth fail count: %d)", 
                     i, existing_config.networks[i].ssid, existing_config.networks[i].auth_fail_count);
            if (existing_config.networks[i].auth_fail_count > 0) {
                has_auth_failures = true;
            }
        }
        
        // Clear auth failures if any exist (allows retry after password change or temporary issues)
        if (has_auth_failures) {
            ESP_LOGI(TAG, "Clearing authentication failures to allow reconnection attempts...");
            wifi_manager_clear_all_auth_failures();
        }
    }
    
    // Initialize WiFi manager - this will attempt to connect using stored credentials
    ret = wifi_manager_init();
    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "WiFi connected successfully");
        char ip_str[16];
        if (wifi_manager_get_ip_string(ip_str, sizeof(ip_str)) == ESP_OK) {
            ESP_LOGI(TAG, "IP Address: %s", ip_str);
        }
        
    } else if (ret == ESP_ERR_NOT_FOUND) {
        ESP_LOGW(TAG, "No WiFi credentials found in NVS. WiFi not connected.");
        ESP_LOGI(TAG, "To configure WiFi, use wifi_manager_add_network() or store credentials in NVS:");
        ESP_LOGI(TAG, "  - namespace: '%s'", WIFI_NVS_NAMESPACE);
        ESP_LOGI(TAG, "  - SSID prefix: '%s' (e.g., %s0, %s1, ...)", WIFI_NVS_SSID_PREFIX, WIFI_NVS_SSID_PREFIX, WIFI_NVS_SSID_PREFIX);
        ESP_LOGI(TAG, "  - Password prefix: '%s' (e.g., %s0, %s1, ...)", WIFI_NVS_PASSWORD_PREFIX, WIFI_NVS_PASSWORD_PREFIX, WIFI_NVS_PASSWORD_PREFIX);
        ESP_LOGI(TAG, "  - Network count key: '%s'", WIFI_NVS_COUNT_KEY);
    } else {
        ESP_LOGW(TAG, "WiFi initialization failed: %s", esp_err_to_name(ret));
        ESP_LOGW(TAG, "Continuing without network connectivity");
    }
    
    // Initialize peripherals management
    esp_periph_config_t periph_cfg = DEFAULT_ESP_PERIPH_SET_CONFIG();
    esp_periph_set_handle_t set = esp_periph_set_init(&periph_cfg);

    // Initialize SD Card
    audio_board_sdcard_init(set, SD_MODE_1_LINE);

    ESP_LOGI(TAG, "[ 3 ] Initialize buttons");
    audio_board_key_init(set);

    // List music files on SD card
    char **music_files;
    if (music_filenames_get(&music_files) == ESP_OK && music_files != NULL) {
        ESP_LOGD(TAG, "Music files found on SD card:");
        for (int i = 0; music_files[i] != NULL; i++) {
            ESP_LOGD(TAG, "  [%d] %s", i, music_files[i]);
        }
    }

    ESP_LOGI(TAG, "[ 2 ] Start codec chip");
    audio_board_handle_t board_handle = NULL;
    int player_volume = 75;
    
    board_handle = audio_board_init();
    if (board_handle && board_handle->audio_hal) {
        audio_hal_ctrl_codec(board_handle->audio_hal, AUDIO_HAL_CODEC_MODE_DECODE, AUDIO_HAL_CTRL_START);
        audio_hal_set_volume(board_handle->audio_hal, player_volume);
        ESP_LOGI(TAG, "External codec initialized and volume set to %d %%", player_volume);
    } else {
        ESP_LOGW(TAG, "Failed to initialize audio board/codec");
    }


    ESP_LOGI(TAG, "[ 4 ] Set up event listeners");
    // Create separate event interfaces for peripherals and audio pipeline
    audio_event_iface_cfg_t evt_cfg = AUDIO_EVENT_IFACE_DEFAULT_CFG();
    audio_event_iface_handle_t periph_evt = audio_event_iface_init(&evt_cfg);
    
    audio_event_iface_cfg_t audio_evt_cfg = AUDIO_EVENT_IFACE_DEFAULT_CFG();
    audio_event_iface_handle_t audio_evt = audio_event_iface_init(&audio_evt_cfg);

    ESP_LOGI(TAG, "[ 5 ] Listen to peripherals");
    audio_event_iface_set_listener(esp_periph_set_get_event_iface(set), periph_evt);

    // Start the audio control task - allocate params on heap so it persists
    audio_control_parameters_t *params = heap_caps_malloc(sizeof(audio_control_parameters_t), MALLOC_CAP_DEFAULT);
    if (!params) {
        ESP_LOGE(TAG, "Failed to allocate memory for audio control parameters");
        vQueueDelete(audio_control_queue);
        return;
    }
    
    params->queue = audio_control_queue;
    params->evt = audio_evt;  // Use the audio event interface, not the peripheral one
    params->board_handle = board_handle;

    // Pin audio control task to Core 1 (APP CPU) to avoid WiFi interference on Core 0
    if (xTaskCreatePinnedToCore(audio_control_task, "audio_control", 4096, (void *)params, 
                                 5,  // Higher priority than WiFi
                                 NULL,
                                 1) != pdPASS) {  // Pin to Core 1 (APP CPU)
        ESP_LOGE(TAG, "Failed to create audio control task");
        free(params);
        vQueueDelete(audio_control_queue);
        return;
    }

    // Note: We no longer send START message here - the audio control task
    // will load configuration and start playing automatically on initialization
    ESP_LOGI(TAG, "[ 6 ] Audio control task will load configuration and start playing");

    ESP_LOGI(TAG, "[ 7 ] Listen for all pipeline events (Note: actual audio is now handled by audio_control_task)");

    // Note: Since we're now using the audio_control_task, the main task will only handle peripheral events
    while (1) {

        audio_event_iface_msg_t msg;

        esp_err_t ret = audio_event_iface_listen(periph_evt, &msg, portMAX_DELAY);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "[ * ] Event interface error : %d errno %d ", ret, errno);
            continue;
        }

        if (msg.need_free_data) {
            ESP_LOGE(TAG, "[ * ] Warning! Leak! Received message that requires freeing of data, sourcetype %d cmd %d",msg.source_type,msg.cmd);
        }

        // Note: Music info and decoder finish events are now handled in the audio_control_task
        // The main task only handles peripheral events (buttons, etc.)

        // grab button inputs (down only not release) and do things
        if ((msg.source_type == PERIPH_ID_TOUCH || msg.source_type == PERIPH_ID_BUTTON || msg.source_type == PERIPH_ID_ADC_BTN) &&
            (msg.cmd == PERIPH_TOUCH_TAP || msg.cmd == PERIPH_BUTTON_PRESSED || msg.cmd == PERIPH_ADC_BUTTON_PRESSED)) {
            if ((int) msg.data == get_input_volup_id()) {
                ESP_LOGI(TAG, "[ * ] [Vol+] touch tap event");
                player_volume += 10;
                if (player_volume > 100) {
                    player_volume = 100;
                }
                if (board_handle && board_handle->audio_hal) {
                    audio_hal_set_volume(board_handle->audio_hal, player_volume);
                }
                ESP_LOGI(TAG, "[ * ] Volume set to %d %%", player_volume);
                
                // Also update the loop manager's global volume state via control queue
                audio_control_msg_t vol_msg = {
                    .type = AUDIO_ACTION_SET_GLOBAL_VOLUME,
                    .data = {}
                };
                vol_msg.data.set_global_volume.volume_percent = player_volume;
                xQueueSend(audio_control_queue, &vol_msg, 0);  // Non-blocking send
            } else if ((int) msg.data == get_input_voldown_id()) {
                ESP_LOGI(TAG, "[ * ] [Vol-] touch tap event");
                player_volume -= 10;
                if (player_volume < 0) {
                    player_volume = 0;
                }
                if (board_handle && board_handle->audio_hal) {
                    audio_hal_set_volume(board_handle->audio_hal, player_volume);
                }
                ESP_LOGI(TAG, "[ * ] Volume set to %d %%", player_volume);
                
                // Also update the loop manager's global volume state via control queue
                audio_control_msg_t vol_msg = {
                    .type = AUDIO_ACTION_SET_GLOBAL_VOLUME,
                    .data = {}
                };
                vol_msg.data.set_global_volume.volume_percent = player_volume;
                xQueueSend(audio_control_queue, &vol_msg, 0);  // Non-blocking send
            } else if ((int) msg.data == get_input_play_id()) {
                ESP_LOGI(TAG, "[ * ] play button pressed - would send control message to toggle track");
                // TODO: Send a control message to the audio_control_task to toggle track 0
                // For now, just log the button press
            } else if ((int) msg.data == get_input_rec_id()) {
                ESP_LOGI(TAG, "[ * ] rec button pressed - would send control message to adjust gains");
                // TODO: Send a control message to the audio_control_task to adjust gains
                // For now, just log the button press
            } else {
                ESP_LOGI(TAG, "[ * ] Received periph unhandled event cmd %d data int %d",msg.cmd, (int) msg.data);
            }
            continue;
        }

        // Note: Stop event handling would need to be coordinated with the audio_control_task
        // For now, we'll continue listening for peripheral events

// useful prints for understanding your data pipeline
#if 0
        // note: element is 1 << 17 is 131072 player is 1 << 18 service is 1 << 19 periph is 1 << 20
        if (msg.source == (void *)fatfs_stream_reader) {
            if (msg.cmd == AEL_MSG_CMD_REPORT_STATUS) 
                ESP_LOGI(TAG, "[ * ] Received unhandled fatfs source type %d status %s",msg.source_type, AEL_STATUS_STRINGS[(int) msg.data]);
            else
                ESP_LOGI(TAG, "[ * ] Received unhandled fatfs source type %d cmd %s data (int) %d",msg.source_type, AEL_COMMAND_STRINGS[msg.cmd], (int) msg.data);
        } else if (msg.source == (void *)*music_decoder) { 
            if (msg.cmd == AEL_MSG_CMD_REPORT_STATUS) 
                ESP_LOGI(TAG, "[ * ] Received unhandled decoder source type %d status %s",msg.source_type, AEL_STATUS_STRINGS[ (int) msg.data]);
            else
                ESP_LOGI(TAG, "[ * ] Received unhandled decoder source type %d cmd %s data (int) %d",msg.source_type, AEL_COMMAND_STRINGS[msg.cmd], (int) msg.data);
        } else if (msg.source == (void *)equalizer) { 
            if (msg.cmd == AEL_MSG_CMD_REPORT_STATUS) 
                ESP_LOGI(TAG, "[ * ] Received unhandled equalizer source type %d status %s",msg.source_type, AEL_STATUS_STRINGS[ (int) msg.data]);
            else
                ESP_LOGI(TAG, "[ * ] Received unhandled equalizer source type %d cmd %s data (int) %d",msg.source_type, AEL_COMMAND_STRINGS[msg.cmd], (int) msg.data);
        } else if (msg.source == (void *)i2s_stream_writer) { 
            if (msg.cmd == AEL_MSG_CMD_REPORT_STATUS) 
                ESP_LOGI(TAG, "[ * ] Received unhandled i2s source type %d status %s",msg.source_type, AEL_STATUS_STRINGS[ (int) msg.data]);
            else
                ESP_LOGI(TAG, "[ * ] Received unhandled i2s source type %d cmd %s data (int) %d",msg.source_type, AEL_COMMAND_STRINGS[msg.cmd], (int) msg.data);
        } else {
            ESP_LOGI(TAG, "[ * ] Received unhandled unknown source type %d cmd %d data (int) %d",msg.source_type, msg.cmd, (int) msg.data);
        }
#endif // useful debug

    }

    // Cleanup
    ESP_LOGI(TAG, "[ 8 ] Stop and cleanup");
    
    // TODO: Send stop message to audio_control_task and wait for it to finish
    // For now, we'll just clean up the peripherals

    /* Stop all periph before removing the listener */
    esp_periph_set_stop_all(set);
    audio_event_iface_remove_listener(esp_periph_set_get_event_iface(set), periph_evt);

    /* Make sure audio_pipeline_remove_listener & audio_event_iface_remove_listener are called before destroying event_iface */
    audio_event_iface_destroy(periph_evt);
    audio_event_iface_destroy(audio_evt);

    /* Release peripheral resources */
    esp_periph_set_destroy(set);
    
    /* Delete the control queue */
    vQueueDelete(audio_control_queue);
}
