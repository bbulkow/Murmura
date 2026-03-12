#ifndef TRIGGER_LISTENER_H
#define TRIGGER_LISTENER_H

#include "esp_err.h"
#include "http_server.h"

/**
 * @brief Initialize the trigger listener.
 *
 * Starts a FreeRTOS task that connects outbound to the Mur Gateway
 * (using mur_gateway_ip/port from the track manager config):
 *   - Sends an "announce" message with this device's ID
 *   - Sends a "subscribe" message with all configured trigger names
 *   - Reads newline-delimited JSON trigger events and dispatches them to the
 *     audio control queue based on per-track trigger_name / trigger_mode config
 *   - Reconnects with exponential backoff on disconnect
 *
 * Call after http_server_set_track_manager() so the manager pointer is valid.
 *
 * @param manager  Pointer to the live track_manager_t (must remain valid for the
 *                 lifetime of the application).
 */
esp_err_t trigger_listener_init(track_manager_t *manager);

/**
 * @brief Stop the trigger listener task.
 */
void trigger_listener_stop(void);

/**
 * @brief Re-send the subscribe message with current trigger names.
 *
 * Useful after track trigger_name or mode changes at runtime.
 * Safe to call from any task.
 */
esp_err_t trigger_listener_resubscribe(void);

#endif // TRIGGER_LISTENER_H
