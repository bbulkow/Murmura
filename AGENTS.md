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
- **Config vs runtime state**: `scene_config_t` (persisted in scenes.json) has per-track `mode`, `active`, `file_path`, `volume`, triggers, and a per-scene `button_trigger` field. `track_status_t` (runtime) reflects the active scene's config. Never put `is_playing` in config structs — use `is_track_playing()` to check pipeline state. When patching the active scene via `scene_apply_patch()`, fields like `trigger_name`, `trigger_type`, and `mode` must be synced to `track_manager->tracks[]` — the scene config and track_manager are separate copies.
- **SPIRAM**: Use `heap_caps_malloc(size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)` for large allocations. If allocation fails, raise a fatal error (`ESP_ERROR_CHECK(ESP_ERR_NO_MEM)`) — do NOT fall back to regular `malloc`.
- **Stack overflow risk with `track_config_t` and `track_manager_t`**: These structs contain trigger names and track configs — they are too large for the 4096-byte task stacks. NEVER declare them as local (stack) variables. Use `heap_caps_calloc` in SPIRAM instead. This has caused heap-corrupting crashes twice already. When adding fields to these structs, always check for existing stack-allocated instances (`track_config_t varname;` patterns in `murmura.c` and `http_server.c`).
- **Boot ordering**: `mur_listener_init()` must be called AFTER `scene_activate()` so that the initial gateway subscribe message contains the correct trigger names from the active scene's tracks. The mur_listener also needs the `scene_manager_t*` for scene trigger dispatch.
- **SPIRAM and synchronization primitives**: The ESP32's S32C1I atomic compare-and-swap instruction does not work correctly through the SPI cache to external PSRAM. Never embed spinlocks, raw atomic variables, or any synchronization primitive inside a struct allocated wholesale in PSRAM. FreeRTOS `SemaphoreHandle_t` is safe because `xSemaphoreCreate*` allocates from internal RAM by default — but the handle itself (a pointer) must not be confused with the underlying memory. Pattern: keep the struct with the lock in internal RAM and point to bulk data in SPIRAM, or use FreeRTOS semaphore handles (which are internally allocated correctly).

# API contract

**HTTP_API.md** is the authoritative source for all HTTP API endpoints, request/response shapes, and behavior. Read it instead of inspecting `http_server.c` when working on HTTP-related tasks. When adding or changing endpoints, update HTTP_API.md to match.

# Key docs (read before changing anything sync-related)

- **MUR_PROTOCOL.md** — wire-format spec for device ↔ Mur Gateway.
- **HTTP_API.md** — authoritative HTTP API contract.
- **SYNC_DESIGN.md** — synchronized multi-MUR playback (TSF-based). Read this before touching `mur_scheduler.{h,c}`, the time fields on the MUR Protocol, the gateway's `TsfMap`, or the per-MUR `late_policy`.
- **TCP_TUNING.md** — TCP/lwIP rationale for the non-default knobs in `sdkconfig.defaults`. Read this before changing any LWIP submenu setting or chasing latency/jitter/stuck-socket bugs.

# Scope discipline (READ THIS)

When the user asks you to **verify, check, audit, or look at** something, your job is to **inspect and report** — not to refactor, "fix," or remove anything that looks inconsistent to you. Inconsistencies are often *features* whose rationale isn't immediately visible in the code.

Concrete rules:
- "Check that X is applied everywhere" means *report where X is and isn't applied*. It does **not** authorize you to make X apply everywhere.
- "Is this consistent?" means *describe the consistency or asymmetry*. It does **not** authorize you to flatten the asymmetry.
- Asymmetric, conditional, or special-case behavior in this codebase is almost always intentional. Examples that have already burned past sessions:
  - The 1-subscriber non-SceneChange passthrough in `mur_gateway._resolve_target_tsf` (regular triggers fire immediately on a single MUR; only multi-MUR or SceneChange get `fanout_delay_ms`). **This is a critical feature, not a bug.** Do not "apply the delay everywhere" — that change has been proposed and explicitly rejected.
- If you spot what looks like a bug while doing a verification task, **stop and ask**. Describe what you see and why you think it's wrong, then wait for the user to confirm before changing anything. Auto mode does not authorize destructive scope creep — and removing a designed feature is destructive.

# Project architecture

- **main/murmura.c** - App entry point, audio pipeline setup, audio_control_task
- **main/http_server.h/c** - HTTP API, type definitions (track_mode_t, track_status_t, track_manager_t)
- **main/scene_manager.h/c** - Scene system: named playback configs, CRUD, activate, atomic patch, JSON persistence to /sdcard/scenes.json
- **main/config_manager.h/c** - SD card config persistence (track_config_t for gateway config), shared JSON helpers
- **main/mur_listener.h/c** - Mur Gateway TCP client, trigger event processing, scene trigger dispatch (discrete + per-scene button triggers), periodic `get_scene` pull (5 s) for reliability against lost SceneChange events
- **mur-gateway scene cache** - Gateway caches the current scene (primed from Scene Service at startup, refreshed on SceneChange events, lazy HTTP refresh on TTL expiry). Answers device `get_scene` queries from cache.
- **main/unit_status_manager.h/c** - Device identity and network status
- **MUR_PROTOCOL.md** - Authoritative spec for the device ↔ Mur Gateway protocol (trigger events, announce/subscribe). **This is the abstraction boundary** — do NOT explore upstream trigger sources or the Haven Trigger Server.
- **mur-gateway/** - Mur Gateway server (implements MUR_PROTOCOL.md, bridges upstream trigger sources to devices). Also converts upstream `On/Off` triggers to `OneShot` (relabels in `/triggers` listing, drops `Off` events at dispatch, strips `On` value to shape events like real OneShots) — see MUR_PROTOCOL.md "Trigger type translation". This is a deployment workaround pending a firmware update.
- **mur-config-server/** - Flask web UI for managing multiple Murmura devices
- **mock-mur-gateway/** - Mock Mur Gateway for device-level testing (devices connect directly, bypasses real gateway)
- **mock-trigger-server/** - Mock Haven Trigger Server for end-to-end testing (real mur-gateway connects to it)

# running device manager and mur-config-server

For the device-manager and mur-config-server, please execute a set of python commands to make sure the basic function is correct.