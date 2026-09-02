#include "identity_record.h"
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

    assert(ROADRUNNER_IDENTITY_FLASH_OFFSET == 0x1FF000u);
    assert(ROADRUNNER_IDENTITY_SECTOR_SIZE == 0x1000u);
    assert(ROADRUNNER_APPLICATION_FLASH_SIZE == 0x1FF000u);

    memset(erased, 0xff, sizeof(erased));
    different_uuid[15] = 1;
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
    return 0;
}
