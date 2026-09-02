#include "identity_record.h"
#include "identity_store.h"
#include <assert.h>
#include <stdint.h>
#include <string.h>

enum {
    TEST_MAGIC_OFFSET = 0,
    TEST_FORMAT_OFFSET = 4,
    TEST_UUID_OFFSET = 5,
    TEST_CRC_OFFSET = 21,
    TEST_VALID_MARKER_OFFSET = 25,
};

struct in_memory_store {
    uint8_t sector[ROADRUNNER_IDENTITY_SECTOR_SIZE];
};

static bool in_memory_read(void *context, uint32_t offset,
                           uint8_t *data, size_t length) {
    struct in_memory_store *store = context;

    if (offset > sizeof(store->sector)
        || length > sizeof(store->sector) - offset) {
        return false;
    }
    memcpy(data, store->sector + offset, length);
    return true;
}

static bool in_memory_erase(void *context) {
    struct in_memory_store *store = context;

    memset(store->sector, 0xff, sizeof(store->sector));
    return true;
}

static bool in_memory_page_program(void *context, uint32_t offset,
                                   const uint8_t data[ROADRUNNER_IDENTITY_RECORD_SIZE]) {
    struct in_memory_store *store = context;

    if (offset > sizeof(store->sector) - ROADRUNNER_IDENTITY_RECORD_SIZE
        || offset % ROADRUNNER_IDENTITY_RECORD_SIZE != 0) {
        return false;
    }
    for (size_t index = 0; index < ROADRUNNER_IDENTITY_RECORD_SIZE; ++index) {
        store->sector[offset + index] &= data[index];
    }
    return true;
}

static struct rr_identity_store make_in_memory_store(
    struct in_memory_store *memory) {
    return (struct rr_identity_store){
        .context = memory,
        .read = in_memory_read,
        .erase = in_memory_erase,
        .page_program = in_memory_page_program,
    };
}

static uint32_t test_crc32(const uint8_t *data, size_t length) {
    uint32_t crc = 0xffffffffu;

    for (size_t index = 0; index < length; ++index) {
        crc ^= data[index];
        for (unsigned int bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1) ^ ((crc & 1u) ? 0xedb88320u : 0u);
        }
    }
    return crc ^ 0xffffffffu;
}

static void make_valid_record(uint8_t slot[ROADRUNNER_IDENTITY_RECORD_SIZE],
                              const uint8_t uuid[16]) {
    uint32_t crc;

    memset(slot, 0xff, ROADRUNNER_IDENTITY_RECORD_SIZE);
    memcpy(slot + TEST_MAGIC_OFFSET, "RRID", 4);
    slot[TEST_FORMAT_OFFSET] = 1;
    memcpy(slot + TEST_UUID_OFFSET, uuid, 16);
    crc = test_crc32(slot, TEST_CRC_OFFSET);
    slot[TEST_CRC_OFFSET] = (uint8_t)crc;
    slot[TEST_CRC_OFFSET + 1] = (uint8_t)(crc >> 8);
    slot[TEST_CRC_OFFSET + 2] = (uint8_t)(crc >> 16);
    slot[TEST_CRC_OFFSET + 3] = (uint8_t)(crc >> 24);
    slot[TEST_VALID_MARKER_OFFSET] = 0;
}

int main(void) {
    uint8_t erased[ROADRUNNER_IDENTITY_RECORD_SIZE];
    uint8_t valid[ROADRUNNER_IDENTITY_RECORD_SIZE];
    uint8_t corrupt[ROADRUNNER_IDENTITY_RECORD_SIZE];
    uint8_t duplicate[ROADRUNNER_IDENTITY_RECORD_SIZE];
    uint8_t different[ROADRUNNER_IDENTITY_RECORD_SIZE];
    uint8_t zero_uuid[16] = {0};
    uint8_t different_uuid[16] = {0};
    struct rr_identity identity;
    char serial[RR_IDENTITY_SERIAL_LENGTH + 1];
    uint8_t provisioned_uuid[RR_IDENTITY_UUID_SIZE] = {0};
    uint8_t conflicting_uuid[RR_IDENTITY_UUID_SIZE] = {0};
    uint8_t before[ROADRUNNER_IDENTITY_SECTOR_SIZE];
    struct in_memory_store blank_memory;
    struct in_memory_store valid_memory;
    struct in_memory_store conflict_memory;
    struct rr_identity_store blank_store;
    struct rr_identity_store valid_store;
    struct rr_identity_store conflict_store;

    assert(ROADRUNNER_IDENTITY_FLASH_OFFSET == 0x1FF000u);
    assert(ROADRUNNER_IDENTITY_SECTOR_SIZE == 0x1000u);
    assert(ROADRUNNER_APPLICATION_FLASH_SIZE == 0x1FF000u);

    memset(erased, 0xff, sizeof(erased));
    memset(&blank_memory, 0xff, sizeof(blank_memory));
    memset(&valid_memory, 0xff, sizeof(valid_memory));
    memset(&conflict_memory, 0xff, sizeof(conflict_memory));
    different_uuid[15] = 1;
    provisioned_uuid[0] = 0x12;
    provisioned_uuid[15] = 0x34;
    conflicting_uuid[15] = 1;
    make_valid_record(valid, zero_uuid);
    memcpy(corrupt, valid, sizeof(corrupt));
    corrupt[TEST_UUID_OFFSET] ^= 1;
    make_valid_record(duplicate, zero_uuid);
    make_valid_record(different, different_uuid);

    assert(!rr_identity_record_valid(erased));
    assert(rr_identity_record_valid(valid));
    assert(!rr_identity_record_valid(corrupt));
    assert(rr_identity_select(erased, erased, &identity) == RR_IDENTITY_NONE);
    assert(rr_identity_select(valid, erased, &identity) == RR_IDENTITY_OK);
    assert(memcmp(identity.uuid, zero_uuid, sizeof(zero_uuid)) == 0);
    assert(rr_identity_select(corrupt, erased, &identity) == RR_IDENTITY_NONE);
    assert(rr_identity_select(valid, duplicate, &identity) == RR_IDENTITY_OK);
    assert(rr_identity_select(valid, different, &identity) == RR_IDENTITY_CONFLICT);

    rr_identity_serial(zero_uuid, serial);
    assert(strcmp(serial, "RR1-00000000000000000000000000") == 0);
    assert(serial[RR_IDENTITY_SERIAL_LENGTH] == '\0');
    for (size_t index = 0; index < RR_IDENTITY_SERIAL_LENGTH; ++index) {
        assert(serial[index] >= '!' && serial[index] <= '~');
    }

    blank_store = make_in_memory_store(&blank_memory);
    valid_store = make_in_memory_store(&valid_memory);
    conflict_store = make_in_memory_store(&conflict_memory);

    assert(rr_identity_provision(&blank_store, provisioned_uuid) == RR_IDENTITY_OK);
    assert(rr_identity_record_valid(blank_memory.sector));
    assert(rr_identity_record_valid(blank_memory.sector
                                    + ROADRUNNER_IDENTITY_RECORD_SIZE));
    assert(rr_identity_load(&blank_store, &identity) == RR_IDENTITY_OK);
    assert(memcmp(identity.uuid, provisioned_uuid, sizeof(provisioned_uuid)) == 0);

    make_valid_record(valid_memory.sector, provisioned_uuid);
    make_valid_record(valid_memory.sector + ROADRUNNER_IDENTITY_RECORD_SIZE,
                      provisioned_uuid);
    memcpy(before, valid_memory.sector, sizeof(before));
    assert(rr_identity_provision(&valid_store, provisioned_uuid)
           == RR_IDENTITY_ALREADY_PROVISIONED);
    assert(memcmp(valid_memory.sector, before, sizeof(before)) == 0);

    make_valid_record(conflict_memory.sector, provisioned_uuid);
    make_valid_record(conflict_memory.sector + ROADRUNNER_IDENTITY_RECORD_SIZE,
                      conflicting_uuid);
    memcpy(before, conflict_memory.sector, sizeof(before));
    assert(rr_identity_load(&conflict_store, &identity) == RR_IDENTITY_CONFLICT);
    assert(rr_identity_provision(&conflict_store, provisioned_uuid)
           == RR_IDENTITY_CONFLICT);
    assert(memcmp(conflict_memory.sector, before, sizeof(before)) == 0);
    return 0;
}
