#ifndef IDENTITY_STORE_H
#define IDENTITY_STORE_H

#include "identity_record.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

struct rr_identity_store {
    void *context;
    bool (*read)(void *context, uint32_t offset, uint8_t *data, size_t length);
    bool (*erase)(void *context);
    bool (*page_program)(void *context, uint32_t offset,
                         const uint8_t data[ROADRUNNER_IDENTITY_RECORD_SIZE]);
};

rr_identity_status_t rr_identity_load(const struct rr_identity_store *store,
                                      struct rr_identity *identity);
rr_identity_status_t rr_identity_clear(const struct rr_identity_store *store);
rr_identity_status_t rr_identity_provision(
    const struct rr_identity_store *store,
    const uint8_t uuid[RR_IDENTITY_UUID_SIZE]);

#if defined(PICO_ON_DEVICE) && PICO_ON_DEVICE
void rr_identity_pico_store_init(struct rr_identity_store *store);
#endif

#endif
