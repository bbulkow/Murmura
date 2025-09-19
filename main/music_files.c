#include <string.h>
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
#include "equalizer.h" // to cover the sins of bad speaker design if necessary

#include "esp_peripherals.h"
#include "periph_touch.h"
#include "periph_adc_button.h"
#include "periph_button.h"
#include "periph_sdcard.h"
#include "board.h"

#include "music_files.h"

// filesystem
#include <stdio.h>
#include <string.h>
#include <dirent.h>
#include <errno.h>

#define TAG "MUSIC_FILES"


esp_err_t music_determine_filetype( const char *filename, enum FILETYPE_ENUM *filetype_o) {
    int lenstr = strlen(filename);

    if ( (lenstr > sizeof(MP3_SUFFIX)) &&
         (strncmp(filename + lenstr - sizeof(MP3_SUFFIX) + 1 , MP3_SUFFIX, sizeof(MP3_SUFFIX) -1) == 0 ) ) {
        ESP_LOGD(TAG, "[ MFG ] Found MP3: %s", filename);
        *filetype_o = FILETYPE_MP3;
        return(ESP_OK);
    }
    // is it wav?
    else if ((lenstr > sizeof(WAV_SUFFIX)) &&
         (strncmp(filename + lenstr - sizeof(WAV_SUFFIX) + 1 , WAV_SUFFIX, sizeof(WAV_SUFFIX) -1) == 0 ) ) {
        ESP_LOGD(TAG, "[ MFG ] Found WAV: %s", filename);
        *filetype_o = FILETYPE_WAV;
        return(ESP_OK);
    }
    return(ESP_FAIL);
}


esp_err_t music_filename_validate( const char *filename, enum FILETYPE_ENUM *filetype_o) {


    *filetype_o = FILETYPE_UNKNOWN;

    struct stat file_stat;

    if (stat(filename, &file_stat) != 0) {
        ESP_LOGW(TAG, "[] File %s does not exist",filename);
        return(ESP_FAIL);
    }
    if (! S_ISREG(file_stat.st_mode) ){
        ESP_LOGW(TAG, "[] File %s not a regular file",filename);
        return(ESP_FAIL);
    } 

    esp_err_t err = music_determine_filetype(filename, filetype_o);

    if (err == ESP_FAIL) {
        ESP_LOGW(TAG, "[] File %s is not a supported encoder extension", filename);
        return(ESP_FAIL);
    }

    return(ESP_OK);
}


int music_filename_get( char **file_o, enum FILETYPE_ENUM *filetype_o) {

    // pass pointers in correctly or get memory overwrites, not going to check
    // every little thing

    *file_o = NULL;
    *filetype_o = FILETYPE_UNKNOWN;

    char *filename = NULL;

    // let's see if I can autodetect the file format of the test stream on the sd card
    DIR *dir = opendir(PATH_PREFIX);
    if (!dir) {
        ESP_LOGI(TAG, "[E] can't open sd card for autodetect");
        return(-1);
    }

    struct dirent *ent;
    ESP_LOGD(TAG, "[ MFG ] enumerate SDcard");
    while ((ent = readdir(dir)) != NULL) {

        int lenstr = strlen(ent->d_name);

        ESP_LOGD(TAG, "[ MFG ] %s", ent->d_name);

        // is mp3?
        if ( (lenstr > sizeof(MP3_SUFFIX)) &&
             (strncmp(ent->d_name + lenstr - sizeof(MP3_SUFFIX) + 1 , MP3_SUFFIX, sizeof(MP3_SUFFIX) -1) == 0 ) ) {
            ESP_LOGD(TAG, "[ MFG ] Found MP3: %s", ent->d_name);
            if (filename) free(filename);
            filename = heap_caps_malloc(lenstr + sizeof(PATH_PREFIX) + 2, MALLOC_CAP_SPIRAM);
            // filename = malloc(lenstr + sizeof(PATH_PREFIX) + 2);
            sprintf(filename, "%s/%s", PATH_PREFIX, ent->d_name);
            *filetype_o = FILETYPE_MP3;
        }
        // is it wav?
        else if ((lenstr > sizeof(WAV_SUFFIX)) &&
             (strncmp(ent->d_name + lenstr - sizeof(WAV_SUFFIX) + 1 , WAV_SUFFIX, sizeof(WAV_SUFFIX) -1) == 0 ) ) {
            ESP_LOGD(TAG, "[ MFG ] Found WAV: %s", ent->d_name);
            if (filename) free(filename);
            filename = heap_caps_malloc(lenstr + sizeof(PATH_PREFIX) + 2, MALLOC_CAP_SPIRAM);
            // filename = malloc(lenstr + sizeof(PATH_PREFIX) + 2);
            sprintf(filename, "%s/%s", PATH_PREFIX, ent->d_name);
            *filetype_o = FILETYPE_WAV;
        }

    }
    ESP_LOGD(TAG, "[ 1.1] that's all the SDcard");
    closedir(dir);

    *file_o = filename;

    return(0);

}

// get an array of all the valid music filenames on the root of the SD card
// 
esp_err_t music_filenames_get(char ***file_array_o) {
    // FIRST PASS: Count files
    DIR *dir = opendir(PATH_PREFIX);
    if (!dir) {
        ESP_LOGI(TAG, "[E] can't open sd card for autodetect");
        return(ESP_FAIL);
    }
    
    struct dirent *ent;
    
    // first pass, determine how many files we have
    int n_files = 0;
    while ((ent = readdir(dir)) != NULL) {
        enum FILETYPE_ENUM filetype;
        if (ESP_OK == music_determine_filetype( ent->d_name, &filetype)) {
            n_files++;
        }
    }
    
    // CLOSE directory after first pass to free DMA resources
    closedir(dir);

    // Allocate array for file names (add one for NULL terminator)
    n_files++; // let's put a null at the end
    char **files = heap_caps_malloc(n_files * sizeof(void *), MALLOC_CAP_SPIRAM);
    if (files == NULL) return(ESP_FAIL);
    
    // SECOND PASS: Collect file names - REOPEN directory
    dir = opendir(PATH_PREFIX);
    if (!dir) {
        ESP_LOGE(TAG, "[E] can't reopen sd card for second pass");
        free(files);
        return(ESP_FAIL);
    }

    n_files = 0;
    while ((ent = readdir(dir)) != NULL) {
        enum FILETYPE_ENUM filetype;
        if (ESP_OK == music_determine_filetype( ent->d_name, &filetype)) {
            // Use SPIRAM instead of strdup() to avoid internal RAM exhaustion
            size_t len = strlen(ent->d_name) + 1;
            char *fn = heap_caps_malloc(len, MALLOC_CAP_SPIRAM);
            if (fn == NULL) {
                ESP_LOGE(TAG, "Failed to allocate filename in SPIRAM");
                closedir(dir);
                // Free already allocated filenames
                for (int i = 0; i < n_files; i++) {
                    free(files[i]);
                }
                free(files);
                return(ESP_FAIL);
            }
            memcpy(fn, ent->d_name, len);
            files[n_files] = fn;
            n_files++;
        }
    }
    files[n_files] = NULL;

    closedir(dir);
    
    *file_array_o = files;

    return(ESP_OK);
}
