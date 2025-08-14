#
# Main component makefile for Make-based build system (legacy)
#
# This file is included for projects using the legacy Make build system
#

# Exclude LCD peripheral components to avoid ESP-IDF v5.4 compatibility issues
COMPONENT_SRCDIRS := .

# Explicitly set source files
COMPONENT_SRCS := http_server.c \
                  music_files.c \
                  play_sdcard.c \
                  play_sdcard_debug.c \
                  play_sdcard_passthrough.c \
                  wifi_manager.c

COMPONENT_ADD_INCLUDEDIRS := .

# Add compile flags to exclude LCD support
CFLAGS += -DESP_PERIPH_LCD_EXCLUDE=1
