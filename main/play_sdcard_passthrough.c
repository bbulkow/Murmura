/* Alternative approach using passthrough elements */
#include "play_sdcard.h"
#include "raw_stream.h"
#include "filter_resample.h"
#include "esp_decoder.h"
#include "mp3_decoder.h"
#include "wav_decoder.h"
#include "esp_heap_caps.h"
#include "esp_log.h"

static const char *TAG = "PLAY_SDCARD_PASSTHROUGH";

// Custom allocation functions for MP3 decoder to use PSRAM
static void* mp3_malloc_psram(size_t size) {
    void* ptr = heap_caps_malloc(size, MALLOC_CAP_SPIRAM);
    if (ptr) {
        ESP_LOGI(TAG, "MP3: Allocated %d bytes in PSRAM at %p", size, ptr);
    } else {
        // Fallback to internal memory if PSRAM allocation fails
        ptr = malloc(size);
        ESP_LOGW(TAG, "MP3: PSRAM allocation failed for %d bytes, using internal memory at %p", size, ptr);
    }
    return ptr;
}

static void mp3_free_psram(void* ptr) {
    free(ptr);
}

// Helper function to log memory usage
static void log_memory_info(const char *context) {
    ESP_LOGI(TAG, "=== Memory Info: %s ===", context);
    ESP_LOGI(TAG, "Free internal: %d bytes", heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    ESP_LOGI(TAG, "Free PSRAM: %d bytes", heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    ESP_LOGI(TAG, "Largest free internal block: %d bytes", heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
}

// Alternative initialization using passthrough elements
esp_err_t audio_stream_init_with_passthrough(audio_stream_t **stream_o) {
    ESP_LOGI(TAG, "Initializing audio stream with passthrough elements");
    
    audio_stream_t *stream = calloc(1, sizeof(audio_stream_t));
    if (!stream) {
        ESP_LOGE(TAG, "Failed to allocate memory for audio stream");
        return ESP_FAIL;
    }

    // Create a single main pipeline
    audio_pipeline_cfg_t pipeline_cfg = DEFAULT_AUDIO_PIPELINE_CONFIG();
    stream->pipeline = audio_pipeline_init(&pipeline_cfg);
    if (!stream->pipeline) {
        ESP_LOGE(TAG, "Failed to create main pipeline");
        free(stream);
        return ESP_FAIL;
    }

    // Create downmix element - Pin to Core 1 (APP CPU)
    downmix_cfg_t downmix_cfg = DEFAULT_DOWNMIX_CONFIG();
    downmix_cfg.downmix_info.source_num = MAX_TRACKS;
    downmix_cfg.downmix_info.output_type = ESP_DOWNMIX_OUTPUT_TYPE_TWO_CHANNEL;
    downmix_cfg.downmix_info.mode = ESP_DOWNMIX_WORK_MODE_SWITCH_ON;
    downmix_cfg.downmix_info.out_ctx = ESP_DOWNMIX_OUT_CTX_NORMAL;
    downmix_cfg.task_core = 1;  // Pin to Core 1 (APP CPU)
    downmix_cfg.task_prio = 22; // High priority for smooth audio
    
    stream->downmix_e = downmix_init(&downmix_cfg);
    if (!stream->downmix_e) {
        ESP_LOGE(TAG, "Failed to create downmix element");
        audio_pipeline_deinit(stream->pipeline);
        free(stream);
        return ESP_FAIL;
    }

    // I2S output - Pin to Core 1 (APP CPU)
    i2s_stream_cfg_t i2s_cfg = I2S_STREAM_CFG_DEFAULT();
    i2s_cfg.type = AUDIO_STREAM_WRITER;
    i2s_cfg.task_core = 1;  // Pin to Core 1 (APP CPU)
    i2s_cfg.task_prio = 23; // Highest priority for I2S output
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

    // Register downmix and I2S in main pipeline
    audio_pipeline_register(stream->pipeline, stream->downmix_e, "downmix");
    audio_pipeline_register(stream->pipeline, stream->i2s_e, "i2s");
    
    // Link downmix to I2S
    const char *link_tag[2] = {"downmix", "i2s"};
    audio_pipeline_link(stream->pipeline, link_tag, 2);

    // Initialize source info for downmix
    esp_downmix_input_info_t source_info[MAX_TRACKS];
    for (int i = 0; i < MAX_TRACKS; i++) {
        source_info[i].samplerate = 44100;
        source_info[i].channel = 2;
        source_info[i].bits_num = 16;
        source_info[i].gain[0] = 0.0f;
        source_info[i].gain[1] = 0.0f;
        source_info[i].transit_time = 500;
    }
    source_info_init(stream->downmix_e, source_info);

    // Create track pipelines with passthrough elements
    for (int i = 0; i < MAX_TRACKS; i++) {
        // Create pipeline for this track
        audio_pipeline_cfg_t track_pipeline_cfg = DEFAULT_AUDIO_PIPELINE_CONFIG();
        stream->tracks[i].pipeline = audio_pipeline_init(&track_pipeline_cfg);
        if (!stream->tracks[i].pipeline) {
            ESP_LOGE(TAG, "Failed to create pipeline for track %d", i);
            return ESP_FAIL;
        }
        
        // Create fatfs reader - Pin to Core 1 (APP CPU)
        fatfs_stream_cfg_t fatfs_cfg = FATFS_STREAM_CFG_DEFAULT();
        fatfs_cfg.type = AUDIO_STREAM_READER;
        fatfs_cfg.task_core = 1;  // Pin to Core 1 (APP CPU)
        fatfs_cfg.task_prio = 19; // Lower than decoder but still high
        fatfs_cfg.task_stack = 3584;  // Increased to 3.5KB to prevent stack overflow
        fatfs_cfg.buf_sz = 2048;  // Keep buffer at 2KB (must be internal for DMA)
        fatfs_cfg.out_rb_size = 2048;  // Reduce output ringbuffer to save memory
        stream->tracks[i].fatfs_e = fatfs_stream_init(&fatfs_cfg);
        
        // Log memory before creating decoder
        log_memory_info("Before decoder creation");
        
        // Create auto decoder that supports multiple formats
        ESP_LOGI(TAG, "Creating auto decoder for track %d (supports MP3, WAV, etc.)", i);
        
        // Configure the supported decoders
        audio_decoder_t auto_decode[] = {
            DEFAULT_ESP_WAV_DECODER_CONFIG(),
            DEFAULT_ESP_MP3_DECODER_CONFIG(),
            // Can add more formats here: OGG, AAC, FLAC, etc.
        };
        
        // Configure esp_decoder with memory optimization
        esp_decoder_cfg_t auto_dec_cfg = DEFAULT_ESP_DECODER_CONFIG();
        auto_dec_cfg.task_stack = 4096;  // 4KB stack for auto decoder
        auto_dec_cfg.task_core = 1;       // Pin to Core 1 (APP CPU)
        auto_dec_cfg.task_prio = 20;      // Decoder priority
        auto_dec_cfg.out_rb_size = 3072;  // 3KB output buffer (slightly larger for MP3)
        auto_dec_cfg.stack_in_ext = true; // Try to use PSRAM for stack
        
        stream->tracks[i].decode_e = esp_decoder_init(&auto_dec_cfg, auto_decode, 
                                                      sizeof(auto_decode) / sizeof(audio_decoder_t));
        
        // Log memory after creating decoder
        log_memory_info("After decoder creation");

        // Create a raw stream element with reduced buffer
        raw_stream_cfg_t raw_cfg = RAW_STREAM_CFG_DEFAULT();
        raw_cfg.type = AUDIO_STREAM_WRITER;
        raw_cfg.out_rb_size = 2 * 1024;  // Reduce from 4KB to 2KB
        // Note: raw_stream doesn't support direct task_core configuration
        stream->tracks[i].raw_write_e = raw_stream_init(&raw_cfg);

        // Register elements in track pipeline
        char tag_file[16], tag_dec[16], tag_raw[16];
        snprintf(tag_file, sizeof(tag_file), "file_%d", i);
        snprintf(tag_dec, sizeof(tag_dec), "dec_%d", i);
        snprintf(tag_raw, sizeof(tag_raw), "raw_%d", i);
        
        audio_pipeline_register(stream->tracks[i].pipeline, stream->tracks[i].fatfs_e, tag_file);
        audio_pipeline_register(stream->tracks[i].pipeline, stream->tracks[i].decode_e, tag_dec);
        audio_pipeline_register(stream->tracks[i].pipeline, stream->tracks[i].raw_write_e, tag_raw);

        // Link track pipeline: file -> decoder -> raw
        const char *track_link[3] = {tag_file, tag_dec, tag_raw};
        audio_pipeline_link(stream->tracks[i].pipeline, track_link, 3);

        // Get the output ringbuffer from raw element and connect to downmix
        ringbuf_handle_t rb = audio_element_get_output_ringbuf(stream->tracks[i].raw_write_e);
        downmix_set_input_rb(stream->downmix_e, rb, i);
        downmix_set_input_rb_timeout(stream->downmix_e, 0, i);  // Non-blocking
        
        // Enable event reporting for all elements
        audio_element_set_event_callback(stream->tracks[i].fatfs_e, NULL, NULL);
        audio_element_set_event_callback(stream->tracks[i].decode_e, NULL, NULL);
        audio_element_set_event_callback(stream->tracks[i].raw_write_e, NULL, NULL);
        
        ESP_LOGI(TAG, "Track %d configured with passthrough element", i);
    }

    *stream_o = stream;
    ESP_LOGI(TAG, "Audio stream initialized successfully with passthrough elements");
    return ESP_OK;
}
