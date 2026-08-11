#ifndef MUR_LISTENER_H
#define MUR_LISTENER_H

#include "esp_err.h"
#include "http_server.h"
#include "scene_manager.h"

/**
 * @brief Initialize the Mur Gateway listener.
 *
 * Starts a FreeRTOS task that connects outbound to the Mur Gateway
 * (using mur_gateway_ip/port from the track manager config):
 *   - Sends an "announce" message with this device's ID
 *   - Sends a "subscribe" message with all configured trigger names
 *   - Reads newline-delimited JSON trigger events and dispatches them to the
 *     audio control queue based on per-track trigger_name / trigger_type config
 *   - Reconnects on disconnect
 *
 * Call after http_server_set_track_manager() so the manager pointer is valid.
 *
 * @param manager    Pointer to the live track_manager_t (must remain valid).
 * @param scene_mgr  Pointer to the scene_manager_t for scene trigger dispatch
 *                   (may be NULL to disable scene triggers).
 */
esp_err_t mur_listener_init(track_manager_t *manager, scene_manager_t *scene_mgr);

/**
 * @brief Stop the Mur Gateway listener task.
 */
void mur_listener_stop(void);

/**
 * @brief Re-send the subscribe message with current trigger names.
 *
 * Useful after track trigger_name or mode changes at runtime.
 * Safe to call from any task.
 */
esp_err_t mur_listener_resubscribe(void);

/**
 * @brief Whether the gateway socket is currently connected.
 *
 * Reported by GET /api/device and shown on the device's own status page. A
 * configured-looking mur_gateway_ip proves nothing on its own - the port may be
 * unset or the gateway may be unreachable - and this is the only way to see
 * that without a serial console.
 */
bool mur_listener_is_connected(void);

#endif // MUR_LISTENER_H
