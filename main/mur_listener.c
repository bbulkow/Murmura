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
#include "mur_scheduler.h"
#include "wifi_manager.h"
#include "murmura.h"
#include "config_manager.h"
#include "unit_status_manager.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "cJSON.h"

#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "esp_netif.h"
#include <inttypes.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>

static const char *TAG = "MUR_LISTENER";

#define TASK_STACK_SIZE     4608
#define TASK_PRIORITY       4
#define RECV_BUF_SIZE       256
#define LINE_BUF_SIZE       512

/* Poll interval while waiting for WiFi (no network traffic) */
#define WIFI_POLL_MS        1000
/* Retry interval after a failed gateway connection or disconnect */
#define RECONNECT_MS        5000
/* Reliability loop: re-pull current scene from gateway every 5s.
 * The push path (SceneChange trigger events) stays authoritative for
 * latency; this catches dropped events and boot/reconnect drift. */
#define SCENE_PULL_INTERVAL_MS  5000

/* If a scheduled event arrives within this many µs of its target TSF,
 * the listener still submits it (the scheduler will fire immediately).
 * Beyond this, the late_policy decides drop vs play-late. Matches the
 * scheduler's DEADLINE_GRACE_US. See SYNC_DESIGN.md. */
#define LATE_GRACE_US        10000

/* Module state */
static track_manager_t  *s_manager     = NULL;
static scene_manager_t  *s_scene_mgr   = NULL;
static TaskHandle_t      s_task_handle = NULL;
static volatile bool     s_resubscribe = false;

/* ---- forward declarations ----------------------------------------- */
static void  mur_task(void *arg);
static void  process_line(const char *line, int sock);
static void  dispatch_event(const char *trigger_name, const char *value);
static void  dispatch_or_defer(const char *trigger_name, const char *value,
                               uint64_t target_tsf_us);
static int   connect_to_gateway(void);
static bool  send_announce(int sock);
static bool  send_subscribe(int sock);
static bool  send_get_scene(int sock);
static bool  send_tsf_reply(int sock);
static void  handle_scene_response(cJSON *event);

/* ================================================================== */
/*  Public API                                                          */
/* ================================================================== */

esp_err_t mur_listener_init(track_manager_t *manager, scene_manager_t *scene_mgr)
{
    if (!manager) return ESP_ERR_INVALID_ARG;
    s_manager   = manager;
    s_scene_mgr = scene_mgr;

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
    s_manager   = NULL;
    s_scene_mgr = NULL;
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
 * send_announce — send the announce message with this device's ID and the
 * current TSF reading. The TSF seeds the gateway's ISO↔TSF map; gateway
 * may also pull TSF afterward via tsf_query. See SYNC_DESIGN.md.
 */
static bool send_announce(int sock)
{
    char device_id[MAX_UNIT_ID_LEN] = "Murmura";
    unit_status_get_id(device_id, sizeof(device_id));

    uint64_t tsf_us = esp_wifi_get_tsf_time(WIFI_IF_STA);

    char buf[160];
    int len = snprintf(buf, sizeof(buf),
        "{\"type\":\"announce\",\"id\":\"%s\",\"tsf_us\":%" PRIu64 "}\n",
        device_id, tsf_us);

    if (send(sock, buf, len, 0) != len) {
        ESP_LOGW(TAG, "Failed to send announce");
        return false;
    }
    ESP_LOGI(TAG, "Announced as '%s' (tsf_us=%" PRIu64 ")", device_id, tsf_us);
    return true;
}

/*
 * send_tsf_reply — respond to a {"type":"tsf_query"} from the gateway
 * with the current TSF reading. The gateway uses this to keep its
 * ISO↔TSF map fresh; cadence is gateway-side config, not ours.
 */
static bool send_tsf_reply(int sock)
{
    uint64_t tsf_us = esp_wifi_get_tsf_time(WIFI_IF_STA);
    char buf[80];
    int len = snprintf(buf, sizeof(buf),
        "{\"type\":\"tsf_reply\",\"tsf_us\":%" PRIu64 "}\n", tsf_us);
    if (send(sock, buf, len, 0) != len) {
        ESP_LOGW(TAG, "Failed to send tsf_reply");
        return false;
    }
    ESP_LOGD(TAG, "Sent tsf_reply (tsf_us=%" PRIu64 ")", tsf_us);
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

    /* Add scene trigger name if configured */
    if (s_manager->scene_trigger_name[0] != '\0') {
        bool dup = false;
        cJSON *item;
        cJSON_ArrayForEach(item, triggers) {
            if (strcmp(item->valuestring, s_manager->scene_trigger_name) == 0) { dup = true; break; }
        }
        if (!dup) {
            cJSON_AddItemToArray(triggers, cJSON_CreateString(s_manager->scene_trigger_name));
            count++;
        }
    }

    /* Add per-scene button trigger names */
    if (s_scene_mgr) {
        for (int s = 0; s < s_scene_mgr->scene_count; s++) {
            const char *btn = s_scene_mgr->scenes[s].button_trigger;
            if (btn[0] == '\0') continue;
            bool dup = false;
            cJSON *item;
            cJSON_ArrayForEach(item, triggers) {
                if (strcmp(item->valuestring, btn) == 0) { dup = true; break; }
            }
            if (!dup) {
                cJSON_AddItemToArray(triggers, cJSON_CreateString(btn));
                count++;
            }
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
 * send_get_scene — ask the gateway for the current active scene.  The
 * gateway answers asynchronously with a {"type":"scene","value":...}
 * message, handled by handle_scene_response().
 */
static bool send_get_scene(int sock)
{
    static const char msg[] = "{\"type\":\"get_scene\"}\n";
    int len = (int)(sizeof(msg) - 1);
    if (send(sock, msg, len, 0) != len) {
        ESP_LOGW(TAG, "Failed to send get_scene");
        return false;
    }
    ESP_LOGD(TAG, "Sent get_scene");
    return true;
}

/*
 * handle_scene_response — apply a {"type":"scene","value":...} message
 * from the gateway.  Idempotent: if the reported scene already matches
 * the active scene, does nothing (important because we re-pull every 5 s).
 * If the reported scene is unknown locally, falls back to default_scene.
 */
static void handle_scene_response(cJSON *event)
{
    if (!s_scene_mgr || !s_manager) return;

    cJSON *value_j = cJSON_GetObjectItem(event, "value");
    const char *value = cJSON_IsString(value_j) ? value_j->valuestring : NULL;

    if (!value || value[0] == '\0') {
        ESP_LOGD(TAG, "Scene pull: gateway has no current scene; keeping active '%s'",
                 s_scene_mgr->active_scene);
        return;
    }

    /* Idempotent: already on this scene — avoid churning the audio pipeline */
    if (strcmp(value, s_scene_mgr->active_scene) == 0) {
        ESP_LOGD(TAG, "Scene pull: '%s' already active", value);
        return;
    }

    /* Pick target: the reported scene if known locally, else default_scene */
    const char *target = value;
    if (!scene_find(s_scene_mgr, target)) {
        ESP_LOGW(TAG, "Scene pull: '%s' unknown locally, falling back to default '%s'",
                 target, s_scene_mgr->default_scene);
        target = s_scene_mgr->default_scene;
        if (target[0] == '\0' || !scene_find(s_scene_mgr, target)) {
            ESP_LOGW(TAG, "Scene pull: no usable default — ignored");
            return;
        }
        if (strcmp(target, s_scene_mgr->active_scene) == 0) {
            ESP_LOGD(TAG, "Scene pull: default '%s' already active", target);
            return;
        }
    }

    esp_err_t ret = scene_activate(s_scene_mgr, target,
                                   s_manager->audio_control_queue, s_manager,
                                   SCENE_ACTIVATE_BACKSTOP);
    if (ret == ESP_OK) {
        ESP_LOGI(TAG, "Scene pull activated '%s'", target);
        mur_listener_resubscribe();
    } else if (ret == ESP_ERR_INVALID_STATE) {
        /* Scene is synchronized — backstop activation refused by design.
         * The MUR will sync to it when the next SceneChange trigger fires. */
        ESP_LOGI(TAG, "Scene pull skipped '%s' (synchronized; awaiting trigger)", target);
    } else {
        ESP_LOGW(TAG, "Scene pull failed to activate '%s': %s",
                 target, esp_err_to_name(ret));
    }
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

    /* --- Discrete scene trigger: value is the scene name --- */
    if (s_scene_mgr && s_manager->scene_trigger_name[0] != '\0'
        && strcmp(trigger_name, s_manager->scene_trigger_name) == 0) {
        const char *target = value;
        if (!target || target[0] == '\0' || !scene_find(s_scene_mgr, target)) {
            ESP_LOGI(TAG, "Scene trigger value '%s' not found, falling back to default '%s'",
                     value ? value : "(null)", s_scene_mgr->default_scene);
            target = s_scene_mgr->default_scene;
        }
        if (target[0] != '\0') {
            esp_err_t ret = scene_activate(s_scene_mgr, target,
                                           s_manager->audio_control_queue, s_manager,
                                           SCENE_ACTIVATE_TRIGGER);
            if (ret == ESP_OK) {
                ESP_LOGI(TAG, "Scene trigger activated scene '%s'", target);
                mur_listener_resubscribe();
            } else {
                ESP_LOGW(TAG, "Scene trigger failed to activate '%s': %s",
                         target, esp_err_to_name(ret));
            }
        }
        return;  /* consumed — don't also match tracks */
    }

    /* --- Button scene triggers: per-scene button_trigger field --- */
    if (s_scene_mgr) {
        for (int i = 0; i < s_scene_mgr->scene_count; i++) {
            scene_config_t *sc = &s_scene_mgr->scenes[i];
            if (sc->button_trigger[0] != '\0'
                && strcmp(trigger_name, sc->button_trigger) == 0) {
                if (is_on) {
                    esp_err_t ret = scene_activate(s_scene_mgr, sc->name,
                                                   s_manager->audio_control_queue, s_manager,
                                                   SCENE_ACTIVATE_TRIGGER);
                    if (ret == ESP_OK) {
                        ESP_LOGI(TAG, "Button trigger '%s' activated scene '%s'",
                                 trigger_name, sc->name);
                        mur_listener_resubscribe();
                    } else {
                        ESP_LOGW(TAG, "Button trigger '%s' failed to activate '%s': %s",
                                 trigger_name, sc->name, esp_err_to_name(ret));
                    }
                }
                return;  /* consumed */
            }
        }
    }

    /* --- Per-track trigger matching (existing behavior) --- */
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
            if (audio_control_send_start_track(s_manager->audio_control_queue, i,
                    t->file_path, pdMS_TO_TICKS(100)) != pdPASS) {
                ESP_LOGW(TAG, "Queue full or alloc failed — START_TRACK for track %d dropped", i);
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
 * deferred_dispatch_ctx — payload for a scheduled trigger event. Owned by
 * the scheduler entry; freed via deferred_dispatch_free after the callback
 * fires (or if submit fails).
 */
typedef struct {
    char *name;
    char *value;  /* may be NULL */
} deferred_dispatch_ctx_t;

static void deferred_dispatch_cb(void *arg)
{
    deferred_dispatch_ctx_t *c = (deferred_dispatch_ctx_t *)arg;
    if (!c) return;
    dispatch_event(c->name, c->value);
}

static void deferred_dispatch_free(void *arg)
{
    deferred_dispatch_ctx_t *c = (deferred_dispatch_ctx_t *)arg;
    if (!c) return;
    free(c->name);
    free(c->value);
    free(c);
}

/*
 * dispatch_or_defer — common entry point for trigger events. If
 * target_tsf_us is 0 (omitted by sender), dispatches immediately like the
 * original code path. Otherwise checks lateness and either submits to the
 * mur_scheduler, fires immediately with a 'late' warning (LATE_POLICY_PLAY),
 * or drops with a 'late' warning (LATE_POLICY_DROP). See SYNC_DESIGN.md.
 */
static void dispatch_or_defer(const char *name, const char *value,
                              uint64_t target_tsf_us)
{
    if (target_tsf_us == 0) {
        dispatch_event(name, value);
        return;
    }

    /* Per-device playback offset: signed µs, +delays / -advances. Applied
     * before the lateness check so policy decisions reflect the adjusted
     * deadline. See SYNC_DESIGN.md "Per-MUR playback offset". */
    int32_t offset_us = s_manager ? s_manager->playback_offset_us : 0;
    if (offset_us != 0) {
        target_tsf_us = (uint64_t)((int64_t)target_tsf_us + (int64_t)offset_us);
    }

    uint64_t now_tsf = esp_wifi_get_tsf_time(WIFI_IF_STA);
    if (now_tsf == 0) {
        /* WiFi not associated — TSF undefined. Fall back to immediate. */
        ESP_LOGW(TAG, "scheduled '%s' but TSF unavailable — firing immediately",
                 name ? name : "(null)");
        dispatch_event(name, value);
        return;
    }

    int64_t lateness_us = (int64_t)now_tsf - (int64_t)target_tsf_us;
    if (lateness_us > LATE_GRACE_US) {
        late_policy_t policy = s_manager ? s_manager->late_policy : LATE_POLICY_PLAY;
        if (policy == LATE_POLICY_DROP) {
            ESP_LOGW(TAG, "late event '%s' by %" PRId64 " us — dropped (late_policy=drop)",
                     name ? name : "(null)", lateness_us);
        } else {
            ESP_LOGW(TAG, "late event '%s' by %" PRId64 " us — playing anyway (late_policy=play)",
                     name ? name : "(null)", lateness_us);
            dispatch_event(name, value);
        }
        return;
    }

    /* On-time (possibly slightly late within grace) — schedule for the deadline */
    deferred_dispatch_ctx_t *ctx = calloc(1, sizeof(*ctx));
    if (!ctx) {
        ESP_LOGW(TAG, "deferred ctx alloc failed — firing '%s' immediately",
                 name ? name : "(null)");
        dispatch_event(name, value);
        return;
    }
    ctx->name = name ? strdup(name) : NULL;
    ctx->value = value ? strdup(value) : NULL;
    if (name && !ctx->name) {
        ESP_LOGW(TAG, "deferred name strdup failed — firing immediately");
        deferred_dispatch_free(ctx);
        dispatch_event(name, value);
        return;
    }

    esp_err_t err = mur_scheduler_submit(target_tsf_us,
                                          deferred_dispatch_cb,
                                          deferred_dispatch_free,
                                          ctx);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "scheduler submit failed (%s) — firing '%s' immediately",
                 esp_err_to_name(err), name ? name : "(null)");
        deferred_dispatch_free(ctx);
        dispatch_event(name, value);
        return;
    }

    int64_t lead_us = -lateness_us;
    ESP_LOGD(TAG, "scheduled '%s' for tsf=%" PRIu64 " (in %" PRId64 " us)",
             name ? name : "(null)", target_tsf_us, lead_us);
}

/*
 * process_line — parse a single newline-terminated JSON event string and
 * dispatch it.  Handles both trigger events and gateway protocol messages.
 */
static void process_line(const char *line, int sock)
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
        } else if (strcmp(type_j->valuestring, "scene") == 0) {
            handle_scene_response(event);
        } else if (strcmp(type_j->valuestring, "tsf_query") == 0) {
            send_tsf_reply(sock);
        } else {
            ESP_LOGD(TAG, "Gateway message type='%s'", type_j->valuestring);
        }
        cJSON_Delete(event);
        return;
    }

    /* Otherwise it's a trigger event */
    cJSON *name_j   = cJSON_GetObjectItem(event, "name");
    cJSON *value_j  = cJSON_GetObjectItem(event, "value");
    cJSON *target_j = cJSON_GetObjectItem(event, "target_tsf_us");

    uint64_t target_tsf_us = 0;
    if (cJSON_IsNumber(target_j)) {
        /* cJSON valuedouble is the canonical 64-bit-safe path; valueint
         * is a 32-bit int in this version. */
        if (target_j->valuedouble > 0) {
            target_tsf_us = (uint64_t)target_j->valuedouble;
        }
    }

    if (cJSON_IsString(name_j)) {
        const char *val = cJSON_IsString(value_j) ? value_j->valuestring : NULL;
        ESP_LOGD(TAG, "Event: name='%s' value='%s' target_tsf_us=%" PRIu64,
                 name_j->valuestring, val ? val : "null", target_tsf_us);
        dispatch_or_defer(name_j->valuestring, val, target_tsf_us);
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
    static char recv_buf[RECV_BUF_SIZE];
    static char line_buf[LINE_BUF_SIZE];
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

        /* Send announce, subscribe, and initial scene pull */
        if (!send_announce(sock) || !send_subscribe(sock)) {
            close(sock);
            continue;
        }
        send_get_scene(sock);   /* non-fatal if it fails; periodic timer will retry */

        s_resubscribe = false;
        line_pos = 0;
        TickType_t last_pull_tick = xTaskGetTickCount();

        /* Event receive loop */
        while (1) {
            /* Check if we need to re-subscribe (config changed) */
            if (s_resubscribe) {
                send_subscribe(sock);
                s_resubscribe = false;
            }

            /* Reliability loop: re-pull current scene every SCENE_PULL_INTERVAL_MS */
            TickType_t now = xTaskGetTickCount();
            if ((now - last_pull_tick) >= pdMS_TO_TICKS(SCENE_PULL_INTERVAL_MS)) {
                if (!send_get_scene(sock)) {
                    /* Send failed — socket is dead; break to reconnect */
                    break;
                }
                last_pull_tick = now;
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
                        process_line(line_buf, sock);
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
