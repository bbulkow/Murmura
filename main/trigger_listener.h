#ifndef TRIGGER_LISTENER_H
#define TRIGGER_LISTENER_H

#include "esp_err.h"
#include "http_server.h"

/**
 * @brief Initialize the trigger listener.
 *
 * Opens a TCP server socket on TRIGGER_LISTEN_PORT (defined in http_server.h),
 * starts a FreeRTOS task that:
 *   - Registers this device with the trigger gateway (if trigger_server_ip is set)
 *   - Accepts an incoming TCP_SOCKET connection from the gateway
 *   - Reads newline-delimited JSON trigger events and dispatches them to the
 *     audio control queue based on per-track trigger_name / trigger_mode config
 *
 * Call after http_server_set_track_manager() so the manager pointer is valid.
 *
 * @param manager  Pointer to the live track_manager_t (must remain valid for the
 *                 lifetime of the application).
 */
esp_err_t trigger_listener_init(track_manager_t *manager);

/**
 * @brief Stop the trigger listener task and close the server socket.
 */
void trigger_listener_stop(void);

/**
 * @brief Re-register with the trigger gateway immediately.
 *
 * Useful after the trigger server IP/port is changed at runtime.
 * Safe to call from any task.
 */
esp_err_t trigger_listener_register_with_gateway(void);

#endif // TRIGGER_LISTENER_H
