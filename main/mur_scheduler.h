#ifndef MUR_SCHEDULER_H
#define MUR_SCHEDULER_H

#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef void (*mur_scheduler_cb_t)(void *ctx);
typedef void (*mur_scheduler_free_t)(void *ctx);

esp_err_t mur_scheduler_start(void);

esp_err_t mur_scheduler_submit(uint64_t target_tsf_us,
                               mur_scheduler_cb_t cb,
                               mur_scheduler_free_t free_ctx,
                               void *ctx);

#ifdef __cplusplus
}
#endif

#endif // MUR_SCHEDULER_H
