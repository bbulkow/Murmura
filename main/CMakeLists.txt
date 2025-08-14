set(COMPONENT_SRCS "http_server.c" "music_files.c" "play_sdcard.c" "play_sdcard_debug.c" "play_sdcard_passthrough.c" "wifi_manager.c")
set(COMPONENT_ADD_INCLUDEDIRS .)

# Specify exact peripherals needed to avoid LCD compilation issues with ESP-IDF v5.4
set(COMPONENT_PRIV_REQUIRES 
    fatfs
    nvs_flash
    driver
    esp_http_server
    esp_peripherals
    json
    audio_pipeline
    audio_stream
    audio_hal
    audio_board
    esp_wifi
    esp_netif
    lwip
)

register_component()
