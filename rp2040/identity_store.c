#include "identity_store.h"

#include <string.h>

enum {
    RR_IDENTITY_MAGIC_OFFSET = 0,
    RR_IDENTITY_FORMAT_OFFSET = 4,
    RR_IDENTITY_UUID_OFFSET = 5,
    RR_IDENTITY_CRC_OFFSET = 21,
    RR_IDENTITY_VALID_MARKER_OFFSET = 25,
    RR_IDENTITY_FORMAT = 1,
    RR_IDENTITY_VALID_MARKER = 0,
};

static uint32_t rr_identity_crc32(const uint8_t *data, size_t length) {
    uint32_t crc = 0xffffffffu;

    for (size_t index = 0; index < length; ++index) {
        crc ^= data[index];
        for (unsigned int bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1) ^ ((crc & 1u) ? 0xedb88320u : 0u);
        }
    }
    return crc ^ 0xffffffffu;
}

static void rr_identity_make_payload(
    uint8_t record[ROADRUNNER_IDENTITY_RECORD_SIZE],
    const uint8_t uuid[RR_IDENTITY_UUID_SIZE]) {
    uint32_t crc;

    memset(record, 0xff, ROADRUNNER_IDENTITY_RECORD_SIZE);
    memcpy(record + RR_IDENTITY_MAGIC_OFFSET, "RRID", 4);
    record[RR_IDENTITY_FORMAT_OFFSET] = RR_IDENTITY_FORMAT;
    memcpy(record + RR_IDENTITY_UUID_OFFSET, uuid, RR_IDENTITY_UUID_SIZE);
    crc = rr_identity_crc32(record, RR_IDENTITY_CRC_OFFSET);
    record[RR_IDENTITY_CRC_OFFSET] = (uint8_t)crc;
    record[RR_IDENTITY_CRC_OFFSET + 1] = (uint8_t)(crc >> 8);
    record[RR_IDENTITY_CRC_OFFSET + 2] = (uint8_t)(crc >> 16);
    record[RR_IDENTITY_CRC_OFFSET + 3] = (uint8_t)(crc >> 24);
}

static bool rr_identity_store_read_slots(
    const struct rr_identity_store *store,
    uint8_t first[ROADRUNNER_IDENTITY_RECORD_SIZE],
    uint8_t second[ROADRUNNER_IDENTITY_RECORD_SIZE]) {
    return store != NULL
        && store->read != NULL
        && store->read(store->context, 0, first, ROADRUNNER_IDENTITY_RECORD_SIZE)
        && store->read(store->context, ROADRUNNER_IDENTITY_RECORD_SIZE, second,
                        ROADRUNNER_IDENTITY_RECORD_SIZE);
}

rr_identity_status_t rr_identity_load(const struct rr_identity_store *store,
                                      struct rr_identity *identity) {
    uint8_t first[ROADRUNNER_IDENTITY_RECORD_SIZE];
    uint8_t second[ROADRUNNER_IDENTITY_RECORD_SIZE];

    if (!rr_identity_store_read_slots(store, first, second)) {
        return RR_IDENTITY_IO_ERROR;
    }
    return rr_identity_select(first, second, identity);
}

rr_identity_status_t rr_identity_provision(
    const struct rr_identity_store *store,
    const uint8_t uuid[RR_IDENTITY_UUID_SIZE]) {
    uint8_t first[ROADRUNNER_IDENTITY_RECORD_SIZE];
    uint8_t second[ROADRUNNER_IDENTITY_RECORD_SIZE];
    uint8_t payload[ROADRUNNER_IDENTITY_RECORD_SIZE];
    uint8_t committed[ROADRUNNER_IDENTITY_RECORD_SIZE];
    struct rr_identity identity;
    rr_identity_status_t status;

    if (uuid == NULL || store == NULL || store->erase == NULL
        || store->page_program == NULL
        || !rr_identity_store_read_slots(store, first, second)) {
        return RR_IDENTITY_IO_ERROR;
    }

    status = rr_identity_select(first, second, &identity);
    if (status == RR_IDENTITY_OK) {
        return RR_IDENTITY_ALREADY_PROVISIONED;
    }
    if (status == RR_IDENTITY_CONFLICT) {
        return RR_IDENTITY_CONFLICT;
    }

    rr_identity_make_payload(payload, uuid);
    if (!store->erase(store->context)
        || !store->page_program(store->context, 0, payload)
        || !store->page_program(store->context, ROADRUNNER_IDENTITY_RECORD_SIZE,
                                payload)
        || !rr_identity_store_read_slots(store, first, second)
        || memcmp(first, payload, sizeof(payload)) != 0
        || memcmp(second, payload, sizeof(payload)) != 0) {
        return RR_IDENTITY_IO_ERROR;
    }

    memcpy(committed, payload, sizeof(committed));
    committed[RR_IDENTITY_VALID_MARKER_OFFSET] = RR_IDENTITY_VALID_MARKER;
    if (!store->page_program(store->context, 0, committed)
        || !store->page_program(store->context, ROADRUNNER_IDENTITY_RECORD_SIZE,
                                committed)
        || !rr_identity_store_read_slots(store, first, second)) {
        return RR_IDENTITY_IO_ERROR;
    }

    status = rr_identity_select(first, second, &identity);
    if (status != RR_IDENTITY_OK
        || memcmp(identity.uuid, uuid, RR_IDENTITY_UUID_SIZE) != 0) {
        return status == RR_IDENTITY_NONE ? RR_IDENTITY_IO_ERROR : status;
    }
    return RR_IDENTITY_OK;
}

#if defined(PICO_ON_DEVICE) && PICO_ON_DEVICE
#include "hardware/flash.h"
#include "pico/flash.h"
#include "pico/stdlib.h"

struct rr_identity_pico_flash_operation {
    bool erase;
    uint32_t offset;
    const uint8_t *data;
};

static void __not_in_flash_func(rr_identity_pico_flash_operation)(void *context) {
    const struct rr_identity_pico_flash_operation *operation = context;

    if (operation->erase) {
        flash_range_erase(operation->offset, ROADRUNNER_IDENTITY_SECTOR_SIZE);
    } else {
        flash_range_program(operation->offset, operation->data,
                            ROADRUNNER_IDENTITY_RECORD_SIZE);
    }
}

static bool rr_identity_pico_read(void *context, uint32_t offset,
                                  uint8_t *data, size_t length) {
    const uint8_t *flash = (const uint8_t *)(XIP_BASE
        + ROADRUNNER_IDENTITY_FLASH_OFFSET + offset);

    (void)context;
    memcpy(data, flash, length);
    return true;
}

static bool rr_identity_pico_erase(void *context) {
    struct rr_identity_pico_flash_operation operation = {
        .erase = true,
        .offset = ROADRUNNER_IDENTITY_FLASH_OFFSET,
        .data = NULL,
    };

    (void)context;
    return flash_safe_execute(rr_identity_pico_flash_operation, &operation, 1000)
        == PICO_OK;
}

static bool rr_identity_pico_page_program(
    void *context, uint32_t offset,
    const uint8_t data[ROADRUNNER_IDENTITY_RECORD_SIZE]) {
    struct rr_identity_pico_flash_operation operation = {
        .erase = false,
        .offset = ROADRUNNER_IDENTITY_FLASH_OFFSET + offset,
        .data = data,
    };

    (void)context;
    return flash_safe_execute(rr_identity_pico_flash_operation, &operation, 1000)
        == PICO_OK;
}

void rr_identity_pico_store_init(struct rr_identity_store *store) {
    *store = (struct rr_identity_store){
        .context = NULL,
        .read = rr_identity_pico_read,
        .erase = rr_identity_pico_erase,
        .page_program = rr_identity_pico_page_program,
    };
}
#endif
