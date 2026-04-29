#include "tsf_drift_test.h"

#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_wifi.h"

#include "wifi_manager.h"

static const char *TAG = "TSF_DRIFT";

#define TSF_SAMPLE_INTERVAL_MS   5000
#define TSF_WIFI_WAIT_POLL_MS     500
#define TSF_WIFI_WAIT_LOG_MS    10000
#define TSF_TASK_STACK_BYTES     3072
#define TSF_TASK_PRIORITY           2
#define TSF_TASK_CORE               0

static void wait_for_wifi(void)
{
    uint32_t waited_ms = 0;
    while (!wifi_manager_is_connected()) {
        if (waited_ms % TSF_WIFI_WAIT_LOG_MS == 0) {
            ESP_LOGI(TAG, "waiting for WiFi association...");
        }
        vTaskDelay(pdMS_TO_TICKS(TSF_WIFI_WAIT_POLL_MS));
        waited_ms += TSF_WIFI_WAIT_POLL_MS;
    }
}

static bool sample_clocks(int64_t *mono_out, uint64_t *tsf_out)
{
    int64_t mono = esp_timer_get_time();
    uint64_t tsf = esp_wifi_get_tsf_time(WIFI_IF_STA);
    if (tsf == 0) {
        return false;
    }
    *mono_out = mono;
    *tsf_out = tsf;
    return true;
}

static void tsf_drift_task(void *arg)
{
    (void)arg;

    ESP_LOGI(TAG, "task started (interval=%d ms)", TSF_SAMPLE_INTERVAL_MS);

    while (1) {
        wait_for_wifi();

        int64_t mono0 = 0;
        uint64_t tsf0 = 0;
        while (!sample_clocks(&mono0, &tsf0)) {
            ESP_LOGW(TAG, "TSF read returned 0 — retrying");
            vTaskDelay(pdMS_TO_TICKS(100));
            if (!wifi_manager_is_connected()) {
                break;
            }
        }
        if (tsf0 == 0) {
            continue;
        }

        int64_t delta0 = (int64_t)tsf0 - mono0;
        int64_t prev_delta = delta0;
        ESP_LOGI(TAG, "first sample mono=%" PRId64 " tsf=%" PRIu64 " delta=%" PRId64,
                 mono0, tsf0, delta0);

        TickType_t last_wake = xTaskGetTickCount();
        const TickType_t period = pdMS_TO_TICKS(TSF_SAMPLE_INTERVAL_MS);

        while (1) {
            vTaskDelayUntil(&last_wake, period);

            if (!wifi_manager_is_connected()) {
                ESP_LOGW(TAG, "WiFi disassociated — re-baselining on reconnect");
                break;
            }

            int64_t mono = 0;
            uint64_t tsf = 0;
            if (!sample_clocks(&mono, &tsf)) {
                ESP_LOGW(TAG, "TSF unavailable — re-baselining on reconnect");
                break;
            }

            int64_t delta = (int64_t)tsf - mono;
            int64_t step = delta - prev_delta;
            int64_t drift_since_start = delta - delta0;

            ESP_LOGI(TAG,
                     "tsf=%" PRIu64 "  mono=%" PRId64 "  delta=%" PRId64
                     "  step=%+" PRId64 "  drift_since_start=%+" PRId64,
                     tsf, mono, delta, step, drift_since_start);

            prev_delta = delta;
        }
    }
}

void tsf_drift_test_start(void)
{
    BaseType_t ret = xTaskCreatePinnedToCore(
        tsf_drift_task,
        "tsf_drift",
        TSF_TASK_STACK_BYTES,
        NULL,
        TSF_TASK_PRIORITY,
        NULL,
        TSF_TASK_CORE);

    if (ret != pdPASS) {
        ESP_LOGE(TAG, "failed to create tsf_drift task");
    }
}
