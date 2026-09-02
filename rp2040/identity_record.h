#ifndef IDENTITY_RECORD_H
#define IDENTITY_RECORD_H

#include <stdbool.h>
#include <stdint.h>

#define ROADRUNNER_IDENTITY_FLASH_OFFSET 0x1FF000u
#define ROADRUNNER_IDENTITY_SECTOR_SIZE 0x1000u
#define ROADRUNNER_APPLICATION_FLASH_SIZE 0x1FF000u
#define ROADRUNNER_IDENTITY_RECORD_SIZE 256u

#define RR_IDENTITY_UUID_SIZE 16u
#define RR_IDENTITY_SERIAL_LENGTH 29u

struct rr_identity {
    uint8_t uuid[RR_IDENTITY_UUID_SIZE];
};

enum rr_identity_result {
    RR_IDENTITY_NONE,
    RR_IDENTITY_OK,
    RR_IDENTITY_CONFLICT,
    RR_IDENTITY_ALREADY_PROVISIONED,
    RR_IDENTITY_IO_ERROR,
};

typedef enum rr_identity_result rr_identity_status_t;

bool rr_identity_record_valid(
    const uint8_t slot[ROADRUNNER_IDENTITY_RECORD_SIZE]);
enum rr_identity_result rr_identity_select(
    const uint8_t first[ROADRUNNER_IDENTITY_RECORD_SIZE],
    const uint8_t second[ROADRUNNER_IDENTITY_RECORD_SIZE],
    struct rr_identity *identity);
void rr_identity_serial(const uint8_t uuid[RR_IDENTITY_UUID_SIZE],
                        char serial[RR_IDENTITY_SERIAL_LENGTH + 1]);

#endif
