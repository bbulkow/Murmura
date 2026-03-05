# Notes for Agents (Claude, Gemini, GPT)

After making code changes, run code to make sure it works as best as possible.

For esp-idf components, run build and make sure all compile errors are removed.

There are a series of warnings regarding obsolete drivers, those are acceptable.

Please note the working environment is WINDOWS 11 and the default shell is POWERSHELL 7.

# Environment

You are running in powershell 7 on windows.

# Running esp-idf

The environment variables ADF_PATH , IDF_PATH, and IDF_TOOLS path are correctly configured.

The working environment is in ~/dev/esp/esp-adf/esp-idf . The tools directory is configured as ~/dev/esp/esp-idf/tools . 

The espressif extension is configured correctly, and its' configuration is in the Tools 

# running build on git bash

Claude Code can only execute through git bash. Therefore the provided command file 'esp-build.p1' is provided.

```
powershell.exe -ExecutionPolicy Bypass -File esp-build.ps1 build 2>&1
```

Notice that the typical -NoProfile must be ommitted.

This allows claude to run build and determine the sources of error.

Build output is written to `build_output.txt` in the project root (UTF-16LE encoded). Use Grep to search for `error:` lines. Filter to `D:/dev/esp/Murmura/main/` to see only project errors (not cascading framework errors).

# ESP-IDF coding notes

- **FreeRTOS include order**: `#include "freertos/FreeRTOS.h"` MUST appear before any other FreeRTOS headers (`semphr.h`, `task.h`, `queue.h`). Violating this causes hundreds of cascading errors from kernel headers.
- **Config vs runtime state**: `track_config_t` (persisted to SD) has `mode` (off/loop/trigger). `track_status_t` (runtime) has both `mode` and `is_playing` (bool). Never put `is_playing` in config structs.
- **SPIRAM**: Use `heap_caps_malloc(size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)` for large allocations. If allocation fails, raise a fatal error (`ESP_ERROR_CHECK(ESP_ERR_NO_MEM)`) — do NOT fall back to regular `malloc`.
- **SPIRAM and synchronization primitives**: The ESP32's S32C1I atomic compare-and-swap instruction does not work correctly through the SPI cache to external PSRAM. Never embed spinlocks, raw atomic variables, or any synchronization primitive inside a struct allocated wholesale in PSRAM. FreeRTOS `SemaphoreHandle_t` is safe because `xSemaphoreCreate*` allocates from internal RAM by default — but the handle itself (a pointer) must not be confused with the underlying memory. Pattern: keep the struct with the lock in internal RAM and point to bulk data in SPIRAM, or use FreeRTOS semaphore handles (which are internally allocated correctly).

# Project architecture

- **main/murmura.c** - App entry point, audio pipeline setup, audio_control_task
- **main/http_server.h/c** - HTTP API, type definitions (track_mode_t, track_status_t, track_manager_t)
- **main/config_manager.h/c** - SD card config persistence (track_config_t, JSON serialization)
- **main/trigger_listener.h/c** - TCP trigger gateway integration, trigger event processing
- **main/unit_status_manager.h/c** - Device identity and network status
- **scape-server/** - Flask web UI for managing multiple Murmura devices

# running device manager and scape-server

For the device-manager and scape-server, please execute a set of python commands to make sure the basic function is correct.