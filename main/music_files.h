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

#ifndef MUSIC_FILES_H
#define MUSIC_FILES_H

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "esp_vfs_fat.h"
#include "sys/stat.h"


// filesystem
#include <string.h>
#include <stdio.h>
#include <dirent.h>
#include <errno.h>

static const char MP3_SUFFIX[] = ".mp3";
static const char WAV_SUFFIX[] = ".wav";
static const char PATH_PREFIX[] = "/sdcard";

enum FILETYPE_ENUM {
    FILETYPE_UNKNOWN,
    FILETYPE_MP3,
    FILETYPE_WAV
};

esp_err_t music_determine_filetype( const char *filename, enum FILETYPE_ENUM *filetype_o);

esp_err_t music_filename_validate( const char *filename, enum FILETYPE_ENUM *filetype_o);

int music_filename_get( char **file_o, enum FILETYPE_ENUM *filetype_o);

esp_err_t music_filenames_get(char ***file_array_o);

#endif /* MUSIC_FILES_H */
