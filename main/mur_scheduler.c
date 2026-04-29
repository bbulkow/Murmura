/*
 * mur_scheduler.c
 *
 * Generic deadline-based callback scheduler keyed on WiFi TSF microseconds.
 * The listener wraps dispatch_event() with a deferral that submits a
 * (target_tsf_us, callback, ctx) entry; this task pops entries when their
 * TSF deadline arrives and invokes the callback. Single shared min-heap,
 * mutex-protected, signaled by a binary semaphore on submit so the task
 * can re-evaluate when an earlier deadline lands.
 *
 * See SYNC_DESIGN.md for the why.
 */

#include "mur_scheduler.h"

#include <stdbool.h>
#include <stdint.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

#include "esp_log.h"
#include "esp_wifi.h"

static const char *TAG = "MUR_SCHEDULER";

#define MUR_SCHEDULER_CAP            32
#define MUR_SCHEDULER_TASK_STACK   3072
#define MUR_SCHEDULER_TASK_PRIO       4
#define MUR_SCHEDULER_TASK_CORE       0

/* Fire if the deadline is within this many µs of now. Matches the listener
 * grace at submit time so the scheduler doesn't busy-wait the last 10ms. */
#define DEADLINE_GRACE_US        10000

/* Cap how long the task sleeps without a wake — periodic wake protects
 * against missed signals and TSF readback jitter. */
#define MAX_WAIT_MS               1000

/* TSF rollback detection. The 802.11 TSF clock is monotonic within one AP
 * epoch but resets to ~0 when the AP reboots. Pending events in our heap
 * carry target_tsf_us from the old epoch — typically far in the "future"
 * of the new TSF — so they would sit unreachable for the full duration of
 * the AP's previous uptime (potentially days). We track the max observed
 * now_tsf and, if a fresh sample falls more than this threshold below it,
 * declare a rollback and flush the heap. See SYNC_DESIGN.md. */
#define TSF_ROLLBACK_THRESHOLD_US ((uint64_t)1000000)  /* 1 second */

typedef struct {
    uint64_t target_tsf_us;
    mur_scheduler_cb_t cb;
    mur_scheduler_free_t free_ctx;
    void *ctx;
} entry_t;

static entry_t           s_heap[MUR_SCHEDULER_CAP];
static int               s_heap_len = 0;
static uint64_t          s_max_now_tsf = 0;
static SemaphoreHandle_t s_lock     = NULL;
static SemaphoreHandle_t s_wake     = NULL;
static TaskHandle_t      s_task     = NULL;

/* ---- min-heap on target_tsf_us ----------------------------------------- */

static inline void heap_swap(int a, int b)
{
    entry_t tmp = s_heap[a];
    s_heap[a] = s_heap[b];
    s_heap[b] = tmp;
}

static void heap_up(int i)
{
    while (i > 0) {
        int parent = (i - 1) / 2;
        if (s_heap[i].target_tsf_us < s_heap[parent].target_tsf_us) {
            heap_swap(i, parent);
            i = parent;
        } else {
            break;
        }
    }
}

static void heap_down(int i)
{
    while (1) {
        int l = 2 * i + 1;
        int r = 2 * i + 2;
        int min = i;
        if (l < s_heap_len && s_heap[l].target_tsf_us < s_heap[min].target_tsf_us) min = l;
        if (r < s_heap_len && s_heap[r].target_tsf_us < s_heap[min].target_tsf_us) min = r;
        if (min == i) break;
        heap_swap(i, min);
        i = min;
    }
}

/* ---- task body --------------------------------------------------------- */

/* Drop every pending entry, calling free_ctx so strdup'd payloads don't leak.
 * Caller must hold s_lock. Used after a TSF rollback (AP reboot). */
static void flush_heap_locked(void)
{
    for (int i = 0; i < s_heap_len; i++) {
        if (s_heap[i].free_ctx) {
            s_heap[i].free_ctx(s_heap[i].ctx);
        }
    }
    s_heap_len = 0;
}

static void mur_scheduler_task(void *arg)
{
    (void)arg;

    while (1) {
        uint64_t next_deadline = 0;
        bool have_top = false;

        xSemaphoreTake(s_lock, portMAX_DELAY);
        if (s_heap_len > 0) {
            next_deadline = s_heap[0].target_tsf_us;
            have_top = true;
        }
        xSemaphoreGive(s_lock);

        if (!have_top) {
            xSemaphoreTake(s_wake, portMAX_DELAY);
            continue;
        }

        uint64_t now = esp_wifi_get_tsf_time(WIFI_IF_STA);
        if (now == 0) {
            /* TSF unavailable (not associated). Hold and retry — when WiFi
             * comes back the entry will fire (possibly late, listener will
             * have already passed its lateness check at submit time). */
            xSemaphoreTake(s_wake, pdMS_TO_TICKS(500));
            continue;
        }

        /* TSF rollback detection: if the AP rebooted, now will be much smaller
         * than what we've seen before. Flush the heap rather than let stale
         * old-epoch entries sit unreachable. See SYNC_DESIGN.md. */
        if (s_max_now_tsf > now + TSF_ROLLBACK_THRESHOLD_US) {
            int dropped;
            xSemaphoreTake(s_lock, portMAX_DELAY);
            dropped = s_heap_len;
            flush_heap_locked();
            xSemaphoreGive(s_lock);
            ESP_LOGW(TAG,
                     "TSF rollback detected (max=%" PRIu64 " now=%" PRIu64
                     ") — likely AP reboot; flushed %d pending entr%s",
                     s_max_now_tsf, now, dropped, dropped == 1 ? "y" : "ies");
            s_max_now_tsf = now;
            continue;
        }
        if (now > s_max_now_tsf) {
            s_max_now_tsf = now;
        }

        int64_t remaining_us = (int64_t)next_deadline - (int64_t)now;
        if (remaining_us <= DEADLINE_GRACE_US) {
            entry_t e;
            xSemaphoreTake(s_lock, portMAX_DELAY);
            if (s_heap_len > 0 && s_heap[0].target_tsf_us == next_deadline) {
                e = s_heap[0];
                s_heap_len--;
                if (s_heap_len > 0) {
                    s_heap[0] = s_heap[s_heap_len];
                    heap_down(0);
                }
                xSemaphoreGive(s_lock);
                if (e.cb) e.cb(e.ctx);
                if (e.free_ctx) e.free_ctx(e.ctx);
            } else {
                xSemaphoreGive(s_lock);
            }
        } else {
            uint32_t wait_ms = (uint32_t)(remaining_us / 1000);
            if (wait_ms > MAX_WAIT_MS) wait_ms = MAX_WAIT_MS;
            if (wait_ms == 0) wait_ms = 1;
            xSemaphoreTake(s_wake, pdMS_TO_TICKS(wait_ms));
        }
    }
}

/* ---- public API -------------------------------------------------------- */

esp_err_t mur_scheduler_start(void)
{
    if (s_task) return ESP_OK;

    s_lock = xSemaphoreCreateMutex();
    s_wake = xSemaphoreCreateBinary();
    if (!s_lock || !s_wake) {
        ESP_LOGE(TAG, "failed to create sync primitives");
        return ESP_ERR_NO_MEM;
    }

    if (xTaskCreatePinnedToCore(mur_scheduler_task,
                                "mur_scheduler",
                                MUR_SCHEDULER_TASK_STACK,
                                NULL,
                                MUR_SCHEDULER_TASK_PRIO,
                                &s_task,
                                MUR_SCHEDULER_TASK_CORE) != pdPASS) {
        ESP_LOGE(TAG, "xTaskCreatePinnedToCore failed");
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "started (cap=%d, prio=%d, core=%d)",
             MUR_SCHEDULER_CAP, MUR_SCHEDULER_TASK_PRIO, MUR_SCHEDULER_TASK_CORE);
    return ESP_OK;
}

esp_err_t mur_scheduler_submit(uint64_t target_tsf_us,
                               mur_scheduler_cb_t cb,
                               mur_scheduler_free_t free_ctx,
                               void *ctx)
{
    if (!cb) return ESP_ERR_INVALID_ARG;
    if (!s_lock || !s_wake) return ESP_ERR_INVALID_STATE;

    xSemaphoreTake(s_lock, portMAX_DELAY);
    if (s_heap_len >= MUR_SCHEDULER_CAP) {
        xSemaphoreGive(s_lock);
        ESP_LOGW(TAG, "queue full (cap=%d) — submit rejected", MUR_SCHEDULER_CAP);
        return ESP_ERR_NO_MEM;
    }
    s_heap[s_heap_len].target_tsf_us = target_tsf_us;
    s_heap[s_heap_len].cb            = cb;
    s_heap[s_heap_len].free_ctx      = free_ctx;
    s_heap[s_heap_len].ctx           = ctx;
    heap_up(s_heap_len);
    s_heap_len++;
    xSemaphoreGive(s_lock);

    xSemaphoreGive(s_wake);
    return ESP_OK;
}
