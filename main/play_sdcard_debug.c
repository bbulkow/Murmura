/* Debug additions for play_sdcard_music_example.c */
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
#include "ringbuf.h"

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


#include "esp_log.h"
#include "audio_element.h"
#include "fatfs_stream.h"
#include <sys/stat.h>

#include "play_sdcard.h"

static const char *TAG = "PLAY_SDCARD_DEBUG";

// Function to check if file exists and get its size
esp_err_t check_file_exists(const char *path) {
    struct stat file_stat;
    if (stat(path, &file_stat) == -1) {
    ESP_LOGD(TAG, "File does not exist: %s", path);
        return ESP_FAIL;
    }
    ESP_LOGD(TAG, "File exists: %s, size: %ld bytes", path, (long)file_stat.st_size);
    return ESP_OK;
}

// Function to read and validate WAV header
esp_err_t validate_wav_header(const char *path) {
    FILE *file = fopen(path, "rb");
    if (!file) {
    ESP_LOGD(TAG, "Failed to open file: %s", path);
        return ESP_FAIL;
    }
    
    // WAV header structure
    struct {
        char riff[4];      // "RIFF"
        uint32_t size;     // File size - 8
        char wave[4];      // "WAVE"
        char fmt[4];       // "fmt "
        uint32_t fmt_size; // Format chunk size
        uint16_t format;   // Audio format (1 = PCM)
        uint16_t channels; // Number of channels
        uint32_t sample_rate; // Sample rate
        uint32_t byte_rate;   // Byte rate
        uint16_t block_align; // Block align
        uint16_t bits_per_sample; // Bits per sample
    } wav_header;
    
    size_t read = fread(&wav_header, 1, 36, file);
    fclose(file);
    
    if (read < 36) {
    ESP_LOGD(TAG, "File too small to be a valid WAV: %s", path);
        return ESP_FAIL;
    }
    
    // Check RIFF header
    if (strncmp(wav_header.riff, "RIFF", 4) != 0) {
    ESP_LOGD(TAG, "Invalid RIFF header in %s: %.4s", path, wav_header.riff);
        return ESP_FAIL;
    }
    
    // Check WAVE format
    if (strncmp(wav_header.wave, "WAVE", 4) != 0) {
    ESP_LOGD(TAG, "Invalid WAVE format in %s: %.4s", path, wav_header.wave);
        return ESP_FAIL;
    }
    
    // Check fmt chunk
    if (strncmp(wav_header.fmt, "fmt ", 4) != 0) {
    ESP_LOGD(TAG, "Invalid fmt chunk in %s: %.4s", path, wav_header.fmt);
        return ESP_FAIL;
    }
    
    ESP_LOGD(TAG, "WAV file %s: format=%d, channels=%d, sample_rate=%lu, bits=%d", 
             path, wav_header.format, wav_header.channels, 
             (unsigned long)wav_header.sample_rate, wav_header.bits_per_sample);
    
    return ESP_OK;
}

// NOTE: The old audio_control_start_debug function has been removed.
// It was never called and contained hardcoded filenames.
// We now use audio_control_start_debug_v2 which properly waits for
// configuration to set URIs before starting track pipelines.

// Function to debug ringbuffer connections
void debug_ringbuffer_connections(audio_stream_t *stream) {
    ESP_LOGD(TAG, "=== Debugging Ringbuffer Connections ===");
    
    for (int i = 0; i < MAX_TRACKS; i++) {
        ESP_LOGD(TAG, "Track %d connections:", i);
        
        // Get ringbuffer between fatfs and decoder
        ringbuf_handle_t rb = audio_element_get_input_ringbuf(stream->tracks[i].decode_e);
        if (rb) {
            ESP_LOGD(TAG, "  Decoder input ringbuf exists, size: %d, fill: %d", 
                     rb_get_size(rb), rb_bytes_filled(rb));
        } else {
            ESP_LOGE(TAG, "  Decoder input ringbuf is NULL!");
        }
        
        // Get output ringbuffer of fatfs
        rb = audio_element_get_output_ringbuf(stream->tracks[i].fatfs_e);
        if (rb) {
            ESP_LOGD(TAG, "  FATFS output ringbuf exists, size: %d, fill: %d", 
                     rb_get_size(rb), rb_bytes_filled(rb));
        } else {
            ESP_LOGE(TAG, "  FATFS output ringbuf is NULL!");
        }
        
        // Check if they are the same (properly linked)
        ringbuf_handle_t rb_in = audio_element_get_input_ringbuf(stream->tracks[i].decode_e);
        ringbuf_handle_t rb_out = audio_element_get_output_ringbuf(stream->tracks[i].fatfs_e);
        if (rb_in == rb_out) {
            ESP_LOGD(TAG, "  Ringbuffers are properly linked");
        } else {
            ESP_LOGE(TAG, "  Ringbuffers are NOT linked! fatfs_out=%p, decode_in=%p", 
                     (void*)rb_out, (void*)rb_in);
        }
    }
}

// Function to debug element configurations
void debug_element_configs(audio_stream_t *stream) {
    ESP_LOGD(TAG, "=== Debugging Element Configurations ===");
    
    for (int i = 0; i < MAX_TRACKS; i++) {
        ESP_LOGD(TAG, "Track %d element configs:", i);
        
        // Get element info
        audio_element_info_t info;
        
        // FATFS element
        audio_element_getinfo(stream->tracks[i].fatfs_e, &info);
        ESP_LOGD(TAG, "  FATFS: sample_rate=%d, channels=%d, bits=%d, byte_pos=%lld, total_bytes=%lld",
                 info.sample_rates, info.channels, info.bits, 
                 info.byte_pos, info.total_bytes);
        
        // Decoder element
        audio_element_getinfo(stream->tracks[i].decode_e, &info);
        ESP_LOGD(TAG, "  Decoder: sample_rate=%d, channels=%d, bits=%d, byte_pos=%lld, total_bytes=%lld",
                 info.sample_rates, info.channels, info.bits, 
                 info.byte_pos, info.total_bytes);
    }
}

// Enhanced audio_control_start with more debugging
void audio_control_start_debug_v2(audio_stream_t *stream) {
    ESP_LOGI(TAG, "Starting audio control - output pipeline only");
    esp_err_t err;  // Declare err at function scope
    
    // NOTE: This function should ONLY start the output pipeline (downmix + I2S)
    // Track pipelines will be started later via START_TRACK messages after URIs are set
    ESP_LOGI(TAG, "Setting up ringbuffer connections but NOT starting track pipelines");
    
    // Debug connections BEFORE starting pipelines
    ESP_LOGD(TAG, "=== BEFORE starting pipelines ===");
    debug_ringbuffer_connections(stream);
    debug_element_configs(stream);
    
    // Configure initial gains using downmix (prepare for when tracks start)
    float gain0[2] = {0.0f, -6.0f};  // -6dB for track 0
    float gain1[2] = {0.0f, -10.0f}; // -10dB for track 1  
    float gain2[2] = {0.0f, -8.0f};  // -8dB for track 2
    
    downmix_set_gain_info(stream->downmix_e, gain0, 0);
    downmix_set_gain_info(stream->downmix_e, gain1, 1);
    downmix_set_gain_info(stream->downmix_e, gain2, 2);

    // Create output ringbuffers for decoders and connect to downmix
    // This prepares the connections for when track pipelines are started later
    ESP_LOGD(TAG, "Creating decoder output ringbuffers and connecting to downmix");
    for (int i = 0; i < MAX_TRACKS; i++) {
        // Create a ringbuffer for decoder output
        ringbuf_handle_t rb = rb_create(8192, 1);
        if (!rb) {
            ESP_LOGE(TAG, "Failed to create ringbuffer for track %d", i);
            continue;
        }
        
        // Set it as the decoder's output
        audio_element_set_output_ringbuf(stream->tracks[i].decode_e, rb);
        
        // Connect it to downmix input
        downmix_set_input_rb(stream->downmix_e, rb, i);
        downmix_set_input_rb_timeout(stream->downmix_e, 0, i);  // Non-blocking
        
        ESP_LOGD(TAG, "Connected track %d decoder to downmix via ringbuffer", i);
    }
    
    // IMPORTANT: Do NOT start track pipelines here!
    // They will be started via START_TRACK messages after URIs are configured
    ESP_LOGI(TAG, "Track pipelines will be started later via START_TRACK messages");
    
    // Start ONLY the output pipeline (downmix + I2S)
    ESP_LOGD(TAG, "Starting output pipeline (downmix + I2S)");
    err = audio_pipeline_run(stream->pipeline);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start output pipeline: %s", esp_err_to_name(err));
        return;
    }
    
    ESP_LOGI(TAG, "Output pipeline started successfully, waiting for track configurations");
    
    // Debug connections AFTER starting pipelines
    vTaskDelay(100 / portTICK_PERIOD_MS); // Give elements time to initialize
    ESP_LOGD(TAG, "=== AFTER starting pipelines ===");
    debug_ringbuffer_connections(stream);
    debug_element_configs(stream);
    
    // Monitor data flow for a short time
    ESP_LOGD(TAG, "=== Monitoring data flow ===");
    for (int j = 0; j < 5; j++) {
        vTaskDelay(100 / portTICK_PERIOD_MS);
        ESP_LOGD(TAG, "After %d ms:", (j+1)*100);
        
        for (int i = 0; i < MAX_TRACKS; i++) {
            ringbuf_handle_t rb = audio_element_get_input_ringbuf(stream->tracks[i].decode_e);
            if (rb) {
                ESP_LOGD(TAG, "  Track %d decoder input: %d bytes filled", 
                         i, rb_bytes_filled(rb));
            }
            
            // Check decoder output ringbuffer
            rb = audio_element_get_output_ringbuf(stream->tracks[i].decode_e);
            if (rb) {
                ESP_LOGD(TAG, "  Track %d decoder output: %d bytes filled", 
                         i, rb_bytes_filled(rb));
            } else {
                ESP_LOGD(TAG, "  Track %d decoder output is NULL", i);
            }
            
            audio_element_state_t state_fatfs = audio_element_get_state(stream->tracks[i].fatfs_e);
            audio_element_state_t state_decode = audio_element_get_state(stream->tracks[i].decode_e);
            ESP_LOGD(TAG, "  Track %d states: fatfs=%d, decoder=%d", 
                     i, state_fatfs, state_decode);
        }
    }
}

// Function to debug downmix element
void debug_downmix_element(audio_stream_t *stream) {
    ESP_LOGD(TAG, "=== Debugging Downmix Element ===");
    
    // Get downmix status
    audio_element_state_t state = audio_element_get_state(stream->downmix_e);
    ESP_LOGD(TAG, "Downmix state: %d", state);
    
    // Check downmix inputs via decoder outputs (since we can't directly get downmix inputs)
    ESP_LOGD(TAG, "Checking downmix inputs via decoder outputs:");
    for (int i = 0; i < MAX_TRACKS; i++) {
        ringbuf_handle_t rb = audio_element_get_output_ringbuf(stream->tracks[i].decode_e);
        if (rb) {
            ESP_LOGD(TAG, "  Track %d decoder output (downmix input %d): size=%d, filled=%d",
                     i, i, rb_get_size(rb), rb_bytes_filled(rb));
        } else {
            ESP_LOGE(TAG, "  Track %d decoder output is NULL!", i);
        }
    }
    
    // Check downmix output
    ringbuf_handle_t out_rb = audio_element_get_output_ringbuf(stream->downmix_e);
    if (out_rb) {
        ESP_LOGD(TAG, "Downmix output: ringbuf exists, size=%d, filled=%d",
                 rb_get_size(out_rb), rb_bytes_filled(out_rb));
    } else {
        ESP_LOGE(TAG, "Downmix output: ringbuf is NULL!");
    }
    
    // Check I2S input (should be same as downmix output)
    ringbuf_handle_t i2s_rb = audio_element_get_input_ringbuf(stream->i2s_e);
    if (i2s_rb) {
        ESP_LOGD(TAG, "I2S input: ringbuf exists, size=%d, filled=%d",
                 rb_get_size(i2s_rb), rb_bytes_filled(i2s_rb));
        if (i2s_rb == out_rb) {
            ESP_LOGD(TAG, "Downmix output and I2S input are properly linked");
        } else {
            ESP_LOGE(TAG, "Downmix output and I2S input are NOT linked!");
        }
    } else {
        ESP_LOGE(TAG, "I2S input ringbuf is NULL!");
    }
}

// Add event listener debugging in audio_control_task
void debug_audio_event(audio_event_iface_msg_t *msg) {
    // Map common audio element commands and statuses
    const char *cmd_str = "UNKNOWN";
    const char *status_str = "UNKNOWN";
    
    // Log raw event data first for debugging
    ESP_LOGD(TAG, "Raw Event: source=%p, source_type=%d, cmd=%d, data=%d", 
             msg->source, msg->source_type, msg->cmd, (int)msg->data);
    
    switch(msg->cmd) {
        case AEL_MSG_CMD_NONE: cmd_str = "NONE"; break;
        case AEL_MSG_CMD_FINISH: cmd_str = "FINISH"; break;
        case AEL_MSG_CMD_STOP: cmd_str = "STOP"; break;
        case AEL_MSG_CMD_PAUSE: cmd_str = "PAUSE"; break;
        case AEL_MSG_CMD_RESUME: cmd_str = "RESUME"; break;
        case AEL_MSG_CMD_DESTROY: cmd_str = "DESTROY"; break;
        case AEL_MSG_CMD_REPORT_STATUS: cmd_str = "REPORT_STATUS"; break;
        case AEL_MSG_CMD_REPORT_MUSIC_INFO: cmd_str = "REPORT_MUSIC_INFO"; break;
        case AEL_MSG_CMD_REPORT_POSITION: cmd_str = "REPORT_POSITION"; break;
    }
    
    if (msg->cmd == AEL_MSG_CMD_REPORT_STATUS) {
        switch((int)msg->data) {
            case AEL_STATUS_NONE: status_str = "NONE"; break;
            case AEL_STATUS_ERROR_OPEN: status_str = "ERROR_OPEN"; break;
            case AEL_STATUS_ERROR_INPUT: status_str = "ERROR_INPUT"; break;
            case AEL_STATUS_ERROR_PROCESS: status_str = "ERROR_PROCESS"; break;
            case AEL_STATUS_ERROR_OUTPUT: status_str = "ERROR_OUTPUT"; break;
            case AEL_STATUS_ERROR_CLOSE: status_str = "ERROR_CLOSE"; break;
            case AEL_STATUS_ERROR_TIMEOUT: status_str = "ERROR_TIMEOUT"; break;
            case AEL_STATUS_ERROR_UNKNOWN: status_str = "ERROR_UNKNOWN"; break;
            case AEL_STATUS_INPUT_DONE: status_str = "INPUT_DONE"; break;
            case AEL_STATUS_INPUT_BUFFERING: status_str = "INPUT_BUFFERING"; break;
            case AEL_STATUS_OUTPUT_DONE: status_str = "OUTPUT_DONE"; break;
            case AEL_STATUS_OUTPUT_BUFFERING: status_str = "OUTPUT_BUFFERING"; break;
            case AEL_STATUS_STATE_RUNNING: status_str = "STATE_RUNNING"; break;
            case AEL_STATUS_STATE_PAUSED: status_str = "STATE_PAUSED"; break;
            case AEL_STATUS_STATE_STOPPED: status_str = "STATE_STOPPED"; break;
            case AEL_STATUS_STATE_FINISHED: status_str = "STATE_FINISHED"; break;
        }
        ESP_LOGD(TAG, "Event decoded: cmd=%s, status=%s", cmd_str, status_str);
    } else {
        ESP_LOGD(TAG, "Event decoded: cmd=%s, data=%d", cmd_str, (int)msg->data);
    }
}
