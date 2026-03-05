/*
 * trigger_listener.c
 *
 * Registers with the Haven Trigger Gateway and listens for incoming trigger
 * events over a persistent TCP_SOCKET connection.  When a matching event
 * arrives it queues an audio control message (START_TRACK / STOP_TRACK) on
 * the main audio_control_queue.
 *
 * Protocol (from Trigger Gateway):
 *   - This device registers via  POST /api/register  with protocol TCP_SOCKET
 *     and its own listen port.  The gateway then connects back and sends
 *     newline-delimited JSON events:
 *       {"name":"SomeTrigger","value":"On","id":123,"timestamp":"..."}\n
 *
 * Trigger modes:
 *   TRIGGER_MODE_MOMENTARY  – START_TRACK on "On", STOP_TRACK on "Off"
 *   TRIGGER_MODE_ONESHOT    – START_TRACK on "On", nothing on "Off"
 *                             (track plays to natural completion)
 */

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "trigger_listener.h"
#include "murmura.h"
#include "unit_status_manager.h"
#include "esp_log.h"
#include "cJSON.h"

#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "esp_netif.h"
#include <string.h>
#include <errno.h>
#include <fcntl.h>

static const char *TAG = "TRIGGER_LISTENER";

#define TASK_STACK_SIZE     4096
#define TASK_PRIORITY       4
#define RECV_BUF_SIZE       256
#define LINE_BUF_SIZE       512
#define REREGISTER_INTERVAL_MS  30000   /* re-register every 30 s */

/* Module state — kept in internal RAM (never in SPIRAM) to satisfy the
 * S32C1I constraint: the task handle and fd are accessed by multiple
 * tasks and must not live behind the SPI cache. */
static track_manager_t *s_manager     = NULL;
static TaskHandle_t     s_task_handle = NULL;
static int              s_server_fd   = -1;

/* ---- forward declarations ----------------------------------------- */
static void  trigger_task(void *arg);
static void  process_line(const char *line);
static void  dispatch_event(const char *trigger_name, const char *value);
static esp_err_t do_register(void);

/* ================================================================== */
/*  Public API                                                          */
/* ================================================================== */

esp_err_t trigger_listener_init(track_manager_t *manager)
{
    if (!manager) return ESP_ERR_INVALID_ARG;
    s_manager = manager;

    /* Create TCP server socket */
    s_server_fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s_server_fd < 0) {
        ESP_LOGE(TAG, "socket() failed: %d", errno);
        return ESP_FAIL;
    }

    int opt = 1;
    setsockopt(s_server_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    struct sockaddr_in addr = {
        .sin_family      = AF_INET,
        .sin_addr.s_addr = htonl(INADDR_ANY),
        .sin_port        = htons(TRIGGER_LISTEN_PORT),
    };

    if (bind(s_server_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        ESP_LOGE(TAG, "bind() failed on port %d: %d", TRIGGER_LISTEN_PORT, errno);
        close(s_server_fd);
        s_server_fd = -1;
        return ESP_FAIL;
    }

    if (listen(s_server_fd, 1) < 0) {
        ESP_LOGE(TAG, "listen() failed: %d", errno);
        close(s_server_fd);
        s_server_fd = -1;
        return ESP_FAIL;
    }

    /* Make accept() non-blocking so the task can poll for re-registration */
    int flags = fcntl(s_server_fd, F_GETFL, 0);
    fcntl(s_server_fd, F_SETFL, flags | O_NONBLOCK);

    ESP_LOGI(TAG, "Listening for trigger gateway on port %d", TRIGGER_LISTEN_PORT);

    if (xTaskCreate(trigger_task, "trigger_listener", TASK_STACK_SIZE,
                    NULL, TASK_PRIORITY, &s_task_handle) != pdPASS) {
        ESP_LOGE(TAG, "xTaskCreate failed");
        close(s_server_fd);
        s_server_fd = -1;
        return ESP_FAIL;
    }

    return ESP_OK;
}

void trigger_listener_stop(void)
{
    if (s_task_handle) {
        vTaskDelete(s_task_handle);
        s_task_handle = NULL;
    }
    if (s_server_fd >= 0) {
        close(s_server_fd);
        s_server_fd = -1;
    }
    s_manager = NULL;
}

esp_err_t trigger_listener_register_with_gateway(void)
{
    return do_register();
}

/* ================================================================== */
/*  Internal helpers                                                    */
/* ================================================================== */

/*
 * do_register — send POST /api/register to the trigger gateway using a
 * raw TCP socket and hand-crafted HTTP/1.1 request.
 * Returns ESP_OK on HTTP 200/201, ESP_ERR_INVALID_STATE if not configured,
 * ESP_FAIL on network error.
 */
static esp_err_t do_register(void)
{
    if (!s_manager || s_manager->trigger_server_ip[0] == '\0') {
        ESP_LOGD(TAG, "No trigger server IP configured, skipping registration");
        return ESP_ERR_INVALID_STATE;
    }

    /* Get our own IP address so the gateway knows where to connect back */
    char my_ip[16] = "0.0.0.0";
    esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    if (netif) {
        esp_netif_ip_info_t ip_info;
        if (esp_netif_get_ip_info(netif, &ip_info) == ESP_OK) {
            snprintf(my_ip, sizeof(my_ip), IPSTR, IP2STR(&ip_info.ip));
        }
    }

    /* Build JSON body — include "host" so the gateway connects to us, not localhost */
    char device_id[MAX_UNIT_ID_LEN] = "Murmura";
    unit_status_get_id(device_id, sizeof(device_id));

    char body[160];
    int body_len = snprintf(body, sizeof(body),
        "{\"name\":\"%s\",\"host\":\"%s\",\"port\":%d,\"protocol\":\"TCP_SOCKET\"}",
        device_id, my_ip, TRIGGER_LISTEN_PORT);

    /* Build HTTP request */
    char req[512];
    int req_len = snprintf(req, sizeof(req),
        "POST /api/register HTTP/1.1\r\n"
        "Host: %s:%d\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: %d\r\n"
        "Connection: close\r\n"
        "\r\n"
        "%s",
        s_manager->trigger_server_ip, s_manager->trigger_server_port,
        body_len, body);

    struct sockaddr_in srv = {
        .sin_family = AF_INET,
        .sin_port   = htons((uint16_t)s_manager->trigger_server_port),
    };
    if (inet_pton(AF_INET, s_manager->trigger_server_ip, &srv.sin_addr) != 1) {
        ESP_LOGW(TAG, "Invalid trigger server IP: %s", s_manager->trigger_server_ip);
        return ESP_FAIL;
    }

    int sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (sock < 0) return ESP_FAIL;

    struct timeval tv = { .tv_sec = 5, .tv_usec = 0 };
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    if (connect(sock, (struct sockaddr *)&srv, sizeof(srv)) < 0) {
        ESP_LOGW(TAG, "Cannot connect to trigger gateway %s:%d",
                 s_manager->trigger_server_ip, s_manager->trigger_server_port);
        close(sock);
        return ESP_FAIL;
    }

    send(sock, req, req_len, 0);

    /* Drain HTTP response to determine status code */
    char resp[256];
    int rlen = recv(sock, resp, sizeof(resp) - 1, 0);
    close(sock);

    if (rlen > 0) {
        resp[rlen] = '\0';
        /* First line: "HTTP/1.1 200 OK\r\n" */
        int status = 0;
        sscanf(resp, "HTTP/%*s %d", &status);
        if (status == 200 || status == 201) {
            ESP_LOGI(TAG, "Registered with trigger gateway at %s:%d",
                     s_manager->trigger_server_ip, s_manager->trigger_server_port);
            return ESP_OK;
        }
        ESP_LOGW(TAG, "Trigger gateway registration returned HTTP %d", status);
        return ESP_FAIL;
    }

    ESP_LOGW(TAG, "No response from trigger gateway");
    return ESP_FAIL;
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

        /* Name matched — always log that we found it */
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
 * dispatch it.
 */
static void process_line(const char *line)
{
    cJSON *event = cJSON_Parse(line);
    if (!event) {
        ESP_LOGW(TAG, "JSON parse error: %.80s", line);
        return;
    }

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
 * trigger_task — main FreeRTOS task.
 *
 * Loop:
 *   1. Periodically (re-)register with gateway
 *   2. Non-blocking accept() on server socket
 *   3. If connected: blocking recv() with 200 ms timeout; accumulate lines
 *   4. On disconnect: back to accept loop
 */
static void trigger_task(void *arg)
{
    char recv_buf[RECV_BUF_SIZE];
    char line_buf[LINE_BUF_SIZE];
    int  line_pos = 0;

    TickType_t last_reg_tick = xTaskGetTickCount() - pdMS_TO_TICKS(REREGISTER_INTERVAL_MS);

    while (1) {
        /* Re-register if interval has elapsed */
        TickType_t now = xTaskGetTickCount();
        if ((now - last_reg_tick) >= pdMS_TO_TICKS(REREGISTER_INTERVAL_MS)) {
            do_register();
            last_reg_tick = xTaskGetTickCount();
        }

        /* Non-blocking accept */
        struct sockaddr_in client_addr;
        socklen_t addr_len = sizeof(client_addr);
        int client_fd = accept(s_server_fd, (struct sockaddr *)&client_addr, &addr_len);

        if (client_fd < 0) {
            if (errno == EWOULDBLOCK || errno == EAGAIN) {
                vTaskDelay(pdMS_TO_TICKS(200));
                continue;
            }
            ESP_LOGE(TAG, "accept() error %d — retrying", errno);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }

        ESP_LOGI(TAG, "Trigger gateway connected from %s",
                 inet_ntoa(client_addr.sin_addr));

        /* 200 ms receive timeout on client socket */
        struct timeval tv = { .tv_sec = 0, .tv_usec = 200000 };
        setsockopt(client_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

        line_pos = 0;

        while (1) {
            /* Re-register while connected too */
            now = xTaskGetTickCount();
            if ((now - last_reg_tick) >= pdMS_TO_TICKS(REREGISTER_INTERVAL_MS)) {
                do_register();
                last_reg_tick = xTaskGetTickCount();
            }

            int len = recv(client_fd, recv_buf, sizeof(recv_buf) - 1, 0);

            if (len < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK) continue; /* normal timeout */
                ESP_LOGW(TAG, "recv() error %d — disconnecting", errno);
                break;
            }
            if (len == 0) {
                ESP_LOGI(TAG, "Trigger gateway disconnected");
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

        close(client_fd);
    }

    vTaskDelete(NULL);
}
