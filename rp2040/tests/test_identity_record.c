#include "identity_record.h"
#include "identity_store.h"
#include "usb_admin.h"
#include "usb_descriptor_strings.h"
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

struct usb_admin_test_io {
    uint8_t response[128];
    size_t response_length;
    uint8_t legacy_bytes[16];
    size_t legacy_length;
    bool rebooted;
    char events[8];
    size_t event_length;
};

static void usb_admin_test_write(void *context, const uint8_t *data,
                                 size_t length) {
    struct usb_admin_test_io *io = context;

    assert(length <= sizeof(io->response) - io->response_length);
    memcpy(io->response + io->response_length, data, length);
    io->response_length += length;
    io->events[io->event_length++] = 'W';
}

static void usb_admin_test_legacy_byte(void *context, uint8_t byte) {
    struct usb_admin_test_io *io = context;

    assert(io->legacy_length < sizeof(io->legacy_bytes));
    io->legacy_bytes[io->legacy_length++] = byte;
}

static void usb_admin_test_flush(void *context) {
    struct usb_admin_test_io *io = context;

    io->events[io->event_length++] = 'F';
}

static bool usb_admin_test_transmit_complete(void *context) {
    struct usb_admin_test_io *io = context;

    io->events[io->event_length++] = 'T';
    return true;
}

static void usb_admin_test_reboot_bootsel(void *context) {
    struct usb_admin_test_io *io = context;

    io->rebooted = true;
    io->events[io->event_length++] = 'R';
}

static void test_usb_admin_info_frame(void) {
    static const uint8_t request[] = {0x52, 0x52, 0x01, 0x01, 0x00, 0x90};
    static const uint8_t expected_response[] = {
        0x52, 0x52, 0x01, 0x81, 0x3d,
        0x00, 0x01, 0x03, 0x02, 0x0d,
        0x72, 0x6f, 0x61, 0x64, 0x72, 0x75, 0x6e, 0x6e, 0x65, 0x72,
        0x2d, 0x76, 0x31,
        0x03, 0x64, 0x65, 0x76,
        0x1e, 0x52, 0x52, 0x31, 0x2d,
        0x30, 0x30, 0x30, 0x30, 0x30, 0x30, 0x30, 0x30, 0x30, 0x30,
        0x30, 0x30, 0x30, 0x30, 0x30, 0x30, 0x30, 0x30, 0x30, 0x30,
        0x30, 0x30, 0x30, 0x30, 0x30, 0x30,
        0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77,
        0x91,
    };
    struct rr_identity identity = {0};
    static const uint8_t flash_uid[RR_USB_ADMIN_FLASH_UID_SIZE] = {
        0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77,
    };
    struct usb_admin_test_io io = {0};
    struct rr_usb_admin_config config = {
        .identity_status = RR_IDENTITY_OK,
        .identity = &identity,
        .flash_uid = flash_uid,
        .transport = RR_USB_ADMIN_TRANSPORT_USB,
        .led_order = RR_USB_ADMIN_LED_GRB,
        .firmware_version = "dev",
        .context = &io,
        .write = usb_admin_test_write,
        .legacy_byte = usb_admin_test_legacy_byte,
        .flush = usb_admin_test_flush,
        .transmit_complete = usb_admin_test_transmit_complete,
        .reboot_bootsel = usb_admin_test_reboot_bootsel,
    };

    rr_usb_admin_init(&config);
    for (size_t index = 0; index < sizeof(request); ++index) {
        rr_usb_admin_receive(request[index]);
    }

    assert(io.legacy_length == 0);
    assert(io.response_length == sizeof(expected_response));
    assert(memcmp(io.response, expected_response, sizeof(expected_response)) == 0);
}

static void test_usb_admin_preserves_legacy_register_traffic(void) {
    struct usb_admin_test_io io = {0};
    struct rr_usb_admin_config config = {
        .context = &io,
        .legacy_byte = usb_admin_test_legacy_byte,
        .flush = usb_admin_test_flush,
        .transmit_complete = usb_admin_test_transmit_complete,
        .reboot_bootsel = usb_admin_test_reboot_bootsel,
    };

    rr_usb_admin_init(&config);
    rr_usb_admin_receive(0xf5);
    rr_usb_admin_receive(0x10);

    assert(io.response_length == 0);
    assert(io.legacy_length == 2);
    assert(io.legacy_bytes[0] == 0xf5);
    assert(io.legacy_bytes[1] == 0x10);
}

static void test_usb_admin_rejects_bad_crc(void) {
    static const uint8_t request[] = {0x52, 0x52, 0x01, 0x01, 0x00, 0x00};
    static const uint8_t expected_response[] = {
        0x52, 0x52, 0x01, 0x81, 0x01, 0x01, 0xe0,
    };
    struct usb_admin_test_io io = {0};
    struct rr_usb_admin_config config = {
        .context = &io,
        .write = usb_admin_test_write,
        .flush = usb_admin_test_flush,
        .transmit_complete = usb_admin_test_transmit_complete,
        .reboot_bootsel = usb_admin_test_reboot_bootsel,
    };

    rr_usb_admin_init(&config);
    for (size_t index = 0; index < sizeof(request); ++index) {
        rr_usb_admin_receive(request[index]);
    }

    assert(io.response_length == sizeof(expected_response));
    assert(memcmp(io.response, expected_response, sizeof(expected_response)) == 0);
}

static void test_usb_admin_acknowledges_before_bootsel_reboot(void) {
    static const uint8_t request[] = {0x52, 0x52, 0x01, 0x02, 0x00, 0xaf};
    static const uint8_t expected_response[] = {
        0x52, 0x52, 0x01, 0x82, 0x01, 0x00, 0x5a,
    };
    struct usb_admin_test_io io = {0};
    struct rr_usb_admin_config config = {
        .context = &io,
        .write = usb_admin_test_write,
        .flush = usb_admin_test_flush,
        .transmit_complete = usb_admin_test_transmit_complete,
        .reboot_bootsel = usb_admin_test_reboot_bootsel,
    };

    rr_usb_admin_init(&config);
    for (size_t index = 0; index < sizeof(request); ++index) {
        rr_usb_admin_receive(request[index]);
    }

    assert(io.response_length == sizeof(expected_response));
    assert(memcmp(io.response, expected_response, sizeof(expected_response)) == 0);
    assert(io.rebooted);
    assert(io.event_length == 4);
    assert(memcmp(io.events, "WFTR", io.event_length) == 0);
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
    struct rr_identity descriptor_identity = {0};
    struct rr_usb_descriptor_strings descriptor_strings;
    uint8_t flash_uid[RR_USB_FLASH_UID_SIZE] = {
        0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef,
    };

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

    descriptor_identity.uuid[0] = 0;
    rr_usb_descriptor_strings_build(&descriptor_strings, RR_IDENTITY_OK,
                                    &descriptor_identity, flash_uid);
    assert(strcmp(RR_USB_MANUFACTURER, "Vylyne") == 0);
    assert(strcmp(RR_USB_PRODUCT, "Roadrunner") == 0);
    assert(strcmp(descriptor_strings.serial,
                  "RR1-00000000000000000000000000") == 0);

    rr_usb_descriptor_strings_build(&descriptor_strings, RR_IDENTITY_NONE,
                                    NULL, flash_uid);
    assert(strcmp(descriptor_strings.serial,
                  "RR1-UNPROVISIONED-0123456789ABCDEF") == 0);
    test_usb_admin_info_frame();
    test_usb_admin_preserves_legacy_register_traffic();
    test_usb_admin_rejects_bad_crc();
    test_usb_admin_acknowledges_before_bootsel_reboot();
    return 0;
}
