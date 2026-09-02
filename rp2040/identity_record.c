#include "identity_record.h"

#include <stddef.h>
#include <string.h>

#define RR_IDENTITY_FORMAT 1u
#define RR_IDENTITY_VALID_MARKER 0u

struct rr_identity_record {
    uint8_t magic[4];
    uint8_t format;
    uint8_t uuid[RR_IDENTITY_UUID_SIZE];
    uint8_t crc32[4];
    uint8_t valid_marker;
    uint8_t reserved[ROADRUNNER_IDENTITY_RECORD_SIZE - 26u];
} __attribute__((packed));

typedef char rr_identity_record_has_expected_size[
    sizeof(struct rr_identity_record) == ROADRUNNER_IDENTITY_RECORD_SIZE ? 1 : -1];

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

static uint32_t rr_identity_crc32_read(const uint8_t crc[4]) {
    return (uint32_t)crc[0]
        | ((uint32_t)crc[1] << 8)
        | ((uint32_t)crc[2] << 16)
        | ((uint32_t)crc[3] << 24);
}

bool rr_identity_record_valid(
    const uint8_t slot[ROADRUNNER_IDENTITY_RECORD_SIZE]) {
    const struct rr_identity_record *record =
        (const struct rr_identity_record *)slot;

    return slot != NULL
        && memcmp(record->magic, "RRID", sizeof(record->magic)) == 0
        && record->format == RR_IDENTITY_FORMAT
        && record->valid_marker == RR_IDENTITY_VALID_MARKER
        && rr_identity_crc32((const uint8_t *)record,
                             offsetof(struct rr_identity_record, crc32))
            == rr_identity_crc32_read(record->crc32);
}

enum rr_identity_result rr_identity_select(
    const uint8_t first[ROADRUNNER_IDENTITY_RECORD_SIZE],
    const uint8_t second[ROADRUNNER_IDENTITY_RECORD_SIZE],
    struct rr_identity *identity) {
    const struct rr_identity_record *selected = NULL;
    bool first_valid = rr_identity_record_valid(first);
    bool second_valid = rr_identity_record_valid(second);

    if (identity != NULL) {
        memset(identity, 0, sizeof(*identity));
    }
    if (!first_valid && !second_valid) {
        return RR_IDENTITY_NONE;
    }
    if (first_valid && second_valid) {
        const struct rr_identity_record *first_record =
            (const struct rr_identity_record *)first;
        const struct rr_identity_record *second_record =
            (const struct rr_identity_record *)second;

        if (memcmp(first_record->uuid, second_record->uuid,
                   RR_IDENTITY_UUID_SIZE) != 0) {
            return RR_IDENTITY_CONFLICT;
        }
        selected = first_record;
    } else {
        selected = (const struct rr_identity_record *)(first_valid ? first : second);
    }

    if (identity != NULL) {
        memcpy(identity->uuid, selected->uuid, RR_IDENTITY_UUID_SIZE);
    }
    return RR_IDENTITY_OK;
}

void rr_identity_serial(const uint8_t uuid[RR_IDENTITY_UUID_SIZE],
                        char serial[RR_IDENTITY_SERIAL_LENGTH + 1]) {
    static const char alphabet[] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

    memcpy(serial, "RR1-", 4);
    for (unsigned int character = 0; character < 26; ++character) {
        unsigned int value = 0;

        for (unsigned int bit = 0; bit < 5; ++bit) {
            int uuid_bit = (int)(character * 5u + bit) - 2;

            value <<= 1;
            if (uuid_bit >= 0) {
                value |= (uuid[(unsigned int)uuid_bit / 8u]
                          >> (7u - ((unsigned int)uuid_bit % 8u))) & 1u;
            }
        }
        serial[4u + character] = alphabet[value];
    }
    serial[RR_IDENTITY_SERIAL_LENGTH] = '\0';
}
