/*
 * mur_listener.c
 *
 * Connects to the Mur Gateway over a persistent TCP connection.
 * On connect, announces the device ID and subscribes to all trigger
 * names configured on tracks.  Receives newline-delimited JSON trigger
 * events and dispatches them to the audio control queue.
 *
 * Mur Protocol (device → gateway):
 *   {"type":"announce","id":"MURMURA-001"}\n
 *   {"type":"subscribe","triggers":["Button_1","Dial.Number"]}\n
 *
 * Mur Protocol (gateway → device):
 *   {"type":"welcome","gateway":"mur-gateway","version":"1.0"}\n
 *   {"name":"Button_1","value":"On","id":123,"timestamp":"..."}\n
 *
 * Trigger modes:
 *   TRIGGER_MODE_MOMENTARY  – START_TRACK on "On", STOP_TRACK on "Off"
 *   TRIGGER_MODE_ONESHOT    – START_TRACK on "On", nothing on "Off"
 */

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "mur_listener.h"
#include "wifi_manager.h"
#include "murmura.h"
#include "unit_status_manager.h"
#include "esp_log.h"
#include "cJSON.h"

#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "esp_netif.h"
#include <string.h>
#include <errno.h>

static const char *TAG = "MUR_LISTENER";

#define TASK_STACK_SIZE     4096
#define TASK_PRIORITY       4
#define RECV_BUF_SIZE       256
#define LINE_BUF_SIZE       512

/* Poll interval while waiting for WiFi (no network traffic) */
#define WIFI_POLL_MS        1000
/* Retry interval after a failed gateway connection or disconnect */
#define RECONNECT_MS        5000

/* Module state */
static track_manager_t *s_manager     = NULL;
static TaskHandle_t     s_task_handle = NULL;
static volatile bool    s_resubscribe = false;

/* ---- forward declarations ----------------------------------------- */
static void  mur_task(void *arg);
static void  process_line(const char *line);
static void  dispatch_event(const char *trigger_name, const char *value);
static int   connect_to_gateway(void);
static bool  send_announce(int sock);
static bool  send_subscribe(int sock);

/* ================================================================== */
/*  Public API                                                          */
/* ================================================================== */

esp_err_t mur_listener_init(track_manager_t *manager)
{
    if (!manager) return ESP_ERR_INVALID_ARG;
    s_manager = manager;

    ESP_LOGI(TAG, "Mur Gateway client starting (gateway at %s:%d)",
             s_manager->mur_gateway_ip[0] ? s_manager->mur_gateway_ip : "(not set)",
             s_manager->mur_gateway_port);

    if (xTaskCreate(mur_task, "mur_listener", TASK_STACK_SIZE,
                    NULL, TASK_PRIORITY, &s_task_handle) != pdPASS) {
        ESP_LOGE(TAG, "xTaskCreate failed");
        return ESP_FAIL;
    }

    return ESP_OK;
}

void mur_listener_stop(void)
{
    if (s_task_handle) {
        vTaskDelete(s_task_handle);
        s_task_handle = NULL;
    }
    s_manager = NULL;
}

esp_err_t mur_listener_resubscribe(void)
{
    s_resubscribe = true;
    return ESP_OK;
}

/* ================================================================== */
/*  Internal helpers                                                    */
/* ================================================================== */

/*
 * connect_to_gateway — open a TCP connection to the Mur Gateway.
 * Returns socket fd on success, -1 on failure.
 */
static int connect_to_gateway(void)
{
    if (!s_manager || s_manager->mur_gateway_ip[0] == '\0') {
        return -1;
    }

    struct sockaddr_in srv = {
        .sin_family = AF_INET,
        .sin_port   = htons((uint16_t)s_manager->mur_gateway_port),
    };
    if (inet_pton(AF_INET, s_manager->mur_gateway_ip, &srv.sin_addr) != 1) {
        ESP_LOGW(TAG, "Invalid Mur Gateway IP: %s", s_manager->mur_gateway_ip);
        return -1;
    }

    int sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (sock < 0) return -1;

    struct timeval tv = { .tv_sec = 5, .tv_usec = 0 };
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    if (connect(sock, (struct sockaddr *)&srv, sizeof(srv)) < 0) {
        ESP_LOGW(TAG, "Cannot connect to Mur Gateway %s:%d",
                 s_manager->mur_gateway_ip, s_manager->mur_gateway_port);
        close(sock);
        return -1;
    }

    /* Set 200ms recv timeout for the event loop */
    struct timeval recv_tv = { .tv_sec = 0, .tv_usec = 200000 };
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &recv_tv, sizeof(recv_tv));

    ESP_LOGI(TAG, "Connected to Mur Gateway %s:%d",
             s_manager->mur_gateway_ip, s_manager->mur_gateway_port);
    return sock;
}

/*
 * send_announce — send the announce message with this device's ID.
 */
static bool send_announce(int sock)
{
    char device_id[MAX_UNIT_ID_LEN] = "Murmura";
    unit_status_get_id(device_id, sizeof(device_id));

    char buf[128];
    int len = snprintf(buf, sizeof(buf),
        "{\"type\":\"announce\",\"id\":\"%s\"}\n", device_id);

    if (send(sock, buf, len, 0) != len) {
        ESP_LOGW(TAG, "Failed to send announce");
        return false;
    }
    ESP_LOGI(TAG, "Announced as '%s'", device_id);
    return true;
}

/*
 * send_subscribe — subscribe to all trigger names configured on tracks.
 */
static bool send_subscribe(int sock)
{
    if (!s_manager) return false;

    /* Collect unique trigger names from all tracks */
    cJSON *msg = cJSON_CreateObject();
    cJSON_AddStringToObject(msg, "type", "subscribe");
    cJSON *triggers = cJSON_CreateArray();

    int count = 0;
    for (int i = 0; i < MAX_TRACKS; i++) {
        const char *tn = s_manager->tracks[i].trigger_name;
        if (tn[0] == '\0') continue;
        if (s_manager->tracks[i].mode != TRACK_MODE_TRIGGER) continue;

        /* Check for duplicates in the array so far */
        bool dup = false;
        cJSON *item;
        cJSON_ArrayForEach(item, triggers) {
            if (strcmp(item->valuestring, tn) == 0) { dup = true; break; }
        }
        if (!dup) {
            cJSON_AddItemToArray(triggers, cJSON_CreateString(tn));
            count++;
        }
    }
    cJSON_AddItemToObject(msg, "triggers", triggers);

    char *json_str = cJSON_PrintUnformatted(msg);
    cJSON_Delete(msg);
    if (!json_str) return false;

    int len = strlen(json_str);
    /* Append newline */
    char *line = malloc(len + 2);
    if (!line) { free(json_str); return false; }
    memcpy(line, json_str, len);
    line[len] = '\n';
    line[len + 1] = '\0';
    free(json_str);

    bool ok = (send(sock, line, len + 1, 0) == len + 1);
    free(line);

    if (ok) {
        ESP_LOGI(TAG, "Subscribed to %d trigger name(s)", count);
    } else {
        ESP_LOGW(TAG, "Failed to send subscribe");
    }
    return ok;
}

/*
 * dispatch_event — map a trigger event to zero or more tracks and queue
 * the appropriate audio control message.
 */
static void dispatch_event(const char *trigger_name, const char *value)
{
    if (!s_manager || !trigger_name) return;

    ESP_LOGD(TAG, "Received trigger: name='%s' value='%s'", trigger_name, value ? value : "(null)");

    bool is_on  = (value && (strcasecmp(value, "on")  == 0 || strcmp(value, "1") == 0));
    bool is_off = (value && (strcasecmp(value, "off") == 0 || strcmp(value, "0") == 0));

    for (int i = 0; i < MAX_TRACKS; i++) {
        track_status_t *t = &s_manager->tracks[i];

        if (t->mode != TRACK_MODE_TRIGGER)   continue;
        if (t->trigger_name[0] == '\0')      continue;
        if (strcmp(t->trigger_name, trigger_name) != 0) continue;

        if (!t->active) {
            ESP_LOGI(TAG, "Trigger '%s' matched track %d but track is disabled — ignored",
                     trigger_name, i);
            continue;
        }
        if (t->file_path[0] == '\0') {
            ESP_LOGI(TAG, "Trigger '%s' matched track %d but no file set — ignored",
                     trigger_name, i);
            continue;
        }

        audio_control_msg_t msg = { .data = {} };

        if (is_on) {
            msg.type = AUDIO_ACTION_START_TRACK;
            msg.data.start_track.track_index = i;
            strncpy(msg.data.start_track.file_path, t->file_path,
                    sizeof(msg.data.start_track.file_path) - 1);
            if (xQueueSend(s_manager->audio_control_queue, &msg, pdMS_TO_TICKS(100)) != pdPASS) {
                ESP_LOGW(TAG, "Queue full — START_TRACK for track %d dropped", i);
            } else {
                ESP_LOGI(TAG, "Trigger '%s' matched track %d (%s) — starting",
                         trigger_name, i,
                         t->trigger_mode == TRIGGER_MODE_ONESHOT ? "oneshot" : "momentary");
            }
        } else if (is_off && t->trigger_mode == TRIGGER_MODE_MOMENTARY) {
            msg.type = AUDIO_ACTION_STOP_TRACK;
            msg.data.stop_track.track_index = i;
            if (xQueueSend(s_manager->audio_control_queue, &msg, pdMS_TO_TICKS(100)) != pdPASS) {
                ESP_LOGW(TAG, "Queue full — STOP_TRACK for track %d dropped", i);
            } else {
                ESP_LOGI(TAG, "Trigger '%s' matched track %d (momentary) — stopping",
                         trigger_name, i);
            }
        } else {
            ESP_LOGI(TAG, "Trigger '%s' matched track %d (%s) — no action for value='%s'",
                     trigger_name, i,
                     t->trigger_mode == TRIGGER_MODE_ONESHOT ? "oneshot" : "momentary",
                     value ? value : "(null)");
        }
    }
}

/*
 * process_line — parse a single newline-terminated JSON event string and
 * dispatch it.  Handles both trigger events and gateway protocol messages.
 */
static void process_line(const char *line)
{
    cJSON *event = cJSON_Parse(line);
    if (!event) {
        ESP_LOGW(TAG, "JSON parse error: %.80s", line);
        return;
    }

    /* Check if this is a gateway protocol message (has "type" field) */
    cJSON *type_j = cJSON_GetObjectItem(event, "type");
    if (cJSON_IsString(type_j)) {
        if (strcmp(type_j->valuestring, "welcome") == 0) {
            cJSON *ver = cJSON_GetObjectItem(event, "version");
            ESP_LOGI(TAG, "Gateway welcome (version %s)",
                     cJSON_IsString(ver) ? ver->valuestring : "?");
        } else {
            ESP_LOGD(TAG, "Gateway message type='%s'", type_j->valuestring);
        }
        cJSON_Delete(event);
        return;
    }

    /* Otherwise it's a trigger event */
    cJSON *name_j  = cJSON_GetObjectItem(event, "name");
    cJSON *value_j = cJSON_GetObjectItem(event, "value");

    if (cJSON_IsString(name_j)) {
        const char *val = cJSON_IsString(value_j) ? value_j->valuestring : NULL;
        ESP_LOGD(TAG, "Event: name='%s' value='%s'", name_j->valuestring, val ? val : "null");
        dispatch_event(name_j->valuestring, val);
    } else {
        ESP_LOGW(TAG, "Trigger event missing 'name' field");
    }

    cJSON_Delete(event);
}

/*
 * mur_task — main FreeRTOS task.
 *
 * Loop:
 *   1. Wait for WiFi connectivity (poll every 1 s, no network traffic)
 *   2. Connect to Mur Gateway
 *   3. Send announce + subscribe
 *   4. Read events, dispatch them
 *   5. On disconnect or failure: wait 5 s, then retry from step 1
 */
static void mur_task(void *arg)
{
    char recv_buf[RECV_BUF_SIZE];
    char line_buf[LINE_BUF_SIZE];
    int  line_pos = 0;

    while (1) {
        /* Wait until gateway is configured and WiFi is connected */
        while (!s_manager || s_manager->mur_gateway_ip[0] == '\0'
               || !wifi_manager_is_connected()) {
            vTaskDelay(pdMS_TO_TICKS(WIFI_POLL_MS));
        }

        /* WiFi is up — attempt gateway connection */
        int sock = connect_to_gateway();
        if (sock < 0) {
            vTaskDelay(pdMS_TO_TICKS(RECONNECT_MS));
            continue;
        }

        /* Send announce and subscribe */
        if (!send_announce(sock) || !send_subscribe(sock)) {
            close(sock);
            continue;
        }

        s_resubscribe = false;
        line_pos = 0;

        /* Event receive loop */
        while (1) {
            /* Check if we need to re-subscribe (config changed) */
            if (s_resubscribe) {
                send_subscribe(sock);
                s_resubscribe = false;
            }

            int len = recv(sock, recv_buf, sizeof(recv_buf) - 1, 0);

            if (len < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK) continue;
                ESP_LOGW(TAG, "recv() error %d — disconnecting", errno);
                break;
            }
            if (len == 0) {
                ESP_LOGI(TAG, "Mur Gateway disconnected");
                break;
            }

            /* Accumulate bytes into line_buf; process on '\n' */
            for (int i = 0; i < len; i++) {
                char c = recv_buf[i];
                if (c == '\n') {
                    line_buf[line_pos] = '\0';
                    if (line_pos > 0) {
                        process_line(line_buf);
                    }
                    line_pos = 0;
                } else if (c != '\r' && line_pos < (int)sizeof(line_buf) - 1) {
                    line_buf[line_pos++] = c;
                }
            }
        }

        close(sock);
        ESP_LOGI(TAG, "Connection closed, will reconnect in %d s...", RECONNECT_MS / 1000);
        vTaskDelay(pdMS_TO_TICKS(RECONNECT_MS));
    }

    vTaskDelete(NULL);
}
