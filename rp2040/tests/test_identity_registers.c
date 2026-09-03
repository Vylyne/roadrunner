#include "identity_registers.h"
#include "identity_record.h"
#include "usb_descriptor_strings.h"
#include <assert.h>
#include <stdint.h>
#include <string.h>

static const uint8_t test_flash_uid[RR_USB_FLASH_UID_SIZE] = {
    0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef
};

static struct rr_identity test_identity = {
    .uuid = { 0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77,
              0x88, 0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff }
};

static void configure(rr_identity_status_t status) {
    struct rr_identity_registers_config config = {
        .identity_status = status,
        .identity = &test_identity,
        .flash_uid = test_flash_uid,
        .transport = 1u,
        .led_order = 2u,
        .firmware_version = "test-1.2.3",
    };
    rr_identity_registers_init(&config);
}

static void test_reports_identity_state(void) {
    uint8_t buf[64];
    size_t length = 0u;

    configure(RR_IDENTITY_OK);
    assert(rr_identity_registers_read(RR_REG_IDENTITY_STATE, buf, &length));
    assert(length == 1u);
    assert(buf[0] == (uint8_t)RR_IDENTITY_OK);

    configure(RR_IDENTITY_NONE);
    assert(rr_identity_registers_read(RR_REG_IDENTITY_STATE, buf, &length));
    assert(length == 1u);
    assert(buf[0] == (uint8_t)RR_IDENTITY_NONE);
}

static void test_reports_provisioned_serial(void) {
    uint8_t buf[64];
    size_t length = 0u;
    struct rr_usb_descriptor_strings expected;

    configure(RR_IDENTITY_OK);
    rr_usb_descriptor_strings_build(&expected, RR_IDENTITY_OK,
                                    &test_identity, test_flash_uid);

    assert(rr_identity_registers_read(RR_REG_SERIAL, buf, &length));
    assert(length == RR_REG_SERIAL_SIZE);
    assert(strcmp((const char *)buf, expected.serial) == 0);
}

static void test_reports_unprovisioned_serial(void) {
    uint8_t buf[64];
    size_t length = 0u;

    configure(RR_IDENTITY_NONE);
    assert(rr_identity_registers_read(RR_REG_SERIAL, buf, &length));
    assert(length == RR_REG_SERIAL_SIZE);
    assert(strcmp((const char *)buf,
                  "RR-UNPROVISIONED-0123456789ABCDEF") == 0);
}

static void test_pads_serial_with_nuls(void) {
    uint8_t buf[64];
    size_t length = 0u;
    size_t used;

    configure(RR_IDENTITY_NONE);
    memset(buf, 0x5a, sizeof(buf));
    assert(rr_identity_registers_read(RR_REG_SERIAL, buf, &length));

    used = strlen((const char *)buf);
    for (size_t index = used; index < RR_REG_SERIAL_SIZE; ++index) {
        assert(buf[index] == 0u);
    }
}

static void test_reports_firmware_version(void) {
    uint8_t buf[64];
    size_t length = 0u;

    configure(RR_IDENTITY_OK);
    assert(rr_identity_registers_read(RR_REG_FIRMWARE_VERSION, buf, &length));
    assert(length == RR_REG_FIRMWARE_VERSION_SIZE);
    assert(strcmp((const char *)buf, "test-1.2.3") == 0);
    assert(buf[RR_REG_FIRMWARE_VERSION_SIZE - 1u] == 0u);
}

static void test_reports_variant(void) {
    uint8_t buf[64];
    size_t length = 0u;

    configure(RR_IDENTITY_OK);
    assert(rr_identity_registers_read(RR_REG_VARIANT, buf, &length));
    assert(length == RR_REG_VARIANT_SIZE);
    assert(buf[0] == 1u);
    assert(buf[1] == 2u);
}

static void test_reports_flash_uid(void) {
    uint8_t buf[64];
    size_t length = 0u;

    configure(RR_IDENTITY_OK);
    assert(rr_identity_registers_read(RR_REG_FLASH_UID, buf, &length));
    assert(length == RR_REG_FLASH_UID_SIZE);
    assert(memcmp(buf, test_flash_uid, RR_REG_FLASH_UID_SIZE) == 0);
}

static void test_ignores_unknown_registers(void) {
    uint8_t buf[64];
    size_t length = 0xdeadu;

    configure(RR_IDENTITY_OK);
    assert(!rr_identity_registers_read(0x10u, buf, &length));
    assert(length == 0xdeadu);
    assert(!rr_identity_registers_read(0x24u, buf, &length));
    assert(!rr_identity_registers_read(0x35u, buf, &length));
}

int main(void) {
    test_reports_identity_state();
    test_reports_provisioned_serial();
    test_reports_unprovisioned_serial();
    test_pads_serial_with_nuls();
    test_reports_firmware_version();
    test_reports_variant();
    test_reports_flash_uid();
    test_ignores_unknown_registers();
    return 0;
}
