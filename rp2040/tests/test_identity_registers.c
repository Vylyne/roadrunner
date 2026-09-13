#include "identity_registers.h"
#include "identity_record.h"
#include "usb_descriptor_strings.h"
#include <assert.h>
#include <stddef.h>
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
                                    &test_identity);

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
    /* Exactly the bare word: no flash UID suffix, even though the config
     * carries a flash UID. */
    assert(strcmp((const char *)buf, "RR-UNPROVISIONED") == 0);
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

/* docs/roadrunner-usb-admin-protocol.md, "Golden vector". */
#define GOLDEN_IMAGE_START 0x10000000u
#define GOLDEN_IMAGE_LENGTH 600u
#define GOLDEN_IMAGE_DIGEST 0xbbe38aa9u

static uint8_t golden_image[GOLDEN_IMAGE_LENGTH];

static void install_golden_image(void) {
    for (size_t index = 0; index < GOLDEN_IMAGE_LENGTH; ++index) {
        golden_image[index] = (uint8_t)((index * 7u + 3u) & 0xffu);
    }
    rr_image_digest_init(golden_image, GOLDEN_IMAGE_START, GOLDEN_IMAGE_LENGTH);
}

static uint32_t read_u32(const uint8_t *buf) {
    return (uint32_t)buf[0] | ((uint32_t)buf[1] << 8)
        | ((uint32_t)buf[2] << 16) | ((uint32_t)buf[3] << 24);
}

static void test_reports_image_digest(void) {
    uint8_t buf[64];
    size_t length = 0u;

    configure(RR_IDENTITY_OK);
    install_golden_image();

    /* Four bytes exactly: this register fits Klipper's ten-byte tmcuart
     * buffer, and a UART host asking for more is an MCU shutdown rather than
     * a failed read. Growing it breaks that transport silently. */
    assert(rr_identity_registers_read(RR_REG_IMAGE_DIGEST, buf, &length));
    assert(length == 4u);
    assert(read_u32(buf) == GOLDEN_IMAGE_DIGEST);
}

static void test_reports_image_range(void) {
    uint8_t buf[64];
    size_t length = 0u;

    configure(RR_IDENTITY_OK);
    install_golden_image();

    assert(rr_identity_registers_read(RR_REG_IMAGE_RANGE, buf, &length));
    assert(length == 8u);
    assert(read_u32(buf) == GOLDEN_IMAGE_START);
    assert(read_u32(buf + 4u) == GOLDEN_IMAGE_LENGTH);
}

static void test_image_digest_register_refuses_without_a_digest(void) {
    uint8_t buf[64];
    size_t length = 0xdeadu;

    /* 0x00000000 is a legal CRC, and this register has no room for an
     * algorithm byte to say "none" - so a board with no digest declines the
     * register rather than answering with a number a host would compare. */
    configure(RR_IDENTITY_OK);
    rr_image_digest_init(NULL, GOLDEN_IMAGE_START, GOLDEN_IMAGE_LENGTH);

    assert(!rr_identity_registers_read(RR_REG_IMAGE_DIGEST, buf, &length));
    assert(length == 0xdeadu);

    /* The range register still answers: it says which bytes a digest would
     * have covered, which is useful even when there is no digest. */
    assert(rr_identity_registers_read(RR_REG_IMAGE_RANGE, buf, &length));
    assert(length == 8u);

    install_golden_image();
}

static void test_image_registers_readable_while_locked(void) {
    uint8_t buf[64];
    size_t length = 0u;

    /* "Which build is this" is the question you most need answered about a
     * board that is refusing to serve sensor data. */
    configure(RR_IDENTITY_NONE);
    install_golden_image();

    assert(rr_identity_registers_locked());
    assert(rr_identity_registers_read(RR_REG_IMAGE_DIGEST, buf, &length));
    assert(read_u32(buf) == GOLDEN_IMAGE_DIGEST);
    assert(rr_identity_registers_read(RR_REG_IMAGE_RANGE, buf, &length));
    assert(read_u32(buf) == GOLDEN_IMAGE_START);
}

static void test_ignores_unknown_registers(void) {
    uint8_t buf[64];
    size_t length = 0xdeadu;

    configure(RR_IDENTITY_OK);
    assert(!rr_identity_registers_read(0x10u, buf, &length));
    assert(length == 0xdeadu);
    assert(!rr_identity_registers_read(0x24u, buf, &length));
    /* 0x3F is reserved inside the window, and 0x4A is the first address past
     * it. Both must stay unknown. */
    assert(!rr_identity_registers_read(0x3fu, buf, &length));
    assert(length == 0xdeadu);
    assert(!rr_identity_registers_read(0x4au, buf, &length));
    assert(length == 0xdeadu);
}

/* Reads chunks `base`.. of a field and checks each is exactly four bytes, that
 * only four bytes were written, and that together they equal `expected`. */
static void assert_chunks(uint8_t base, size_t chunks, const uint8_t *expected) {
    for (size_t index = 0; index < chunks; ++index) {
        uint8_t buf[64];
        size_t length = 0u;

        memset(buf, 0x5a, sizeof(buf));
        assert(rr_identity_registers_read((uint8_t)(base + index), buf,
                                          &length));
        assert(length == RR_REG_CHUNK_SIZE);
        assert(memcmp(buf, expected + index * RR_REG_CHUNK_SIZE,
                      RR_REG_CHUNK_SIZE) == 0);
        assert(buf[RR_REG_CHUNK_SIZE] == 0x5au);
    }
}

static void test_chunk_register_numbers(void) {
    /* docs/roadrunner-uart-chunked-identity-design.md, "Register allocation".
     * Hosts hard-code these, so they are pinned against the document. */
    assert(RR_REG_SERIAL_CHUNK_BASE == 0x37);
    assert(RR_REG_SERIAL_CHUNKS == 8u);
    assert(RR_REG_FIRMWARE_VERSION_CHUNK_BASE == 0x40);
    assert(RR_REG_FIRMWARE_VERSION_CHUNKS == 8u);
    assert(RR_REG_IMAGE_RANGE_CHUNK_BASE == 0x48);
    assert(RR_REG_IMAGE_RANGE_CHUNKS == 2u);
    assert(RR_REG_IDENTITY_WINDOW_LAST == 0x49);
}

static void test_serial_chunks_carry_the_provisioned_serial(void) {
    uint8_t expected[RR_REG_SERIAL_SIZE] = {0};
    struct rr_usb_descriptor_strings strings;

    configure(RR_IDENTITY_OK);
    rr_usb_descriptor_strings_build(&strings, RR_IDENTITY_OK, &test_identity);
    assert(strlen(strings.serial) == 29u);
    memcpy(expected, strings.serial, 29u);

    /* Chunk 7 carries the last character and three zero bytes. */
    assert_chunks(RR_REG_SERIAL_CHUNK_BASE, RR_REG_SERIAL_CHUNKS, expected);
}

static void test_serial_chunks_carry_the_unprovisioned_serial(void) {
    static const uint8_t expected[RR_REG_SERIAL_SIZE] = "RR-UNPROVISIONED";

    configure(RR_IDENTITY_NONE);
    /* Sixteen characters fill chunks 0-3 exactly; the terminator is chunk 4,
     * so a host stopping at the first NUL reads five chunks, not four. */
    assert_chunks(RR_REG_SERIAL_CHUNK_BASE, RR_REG_SERIAL_CHUNKS, expected);
}

static void test_firmware_version_chunks(void) {
    static const uint8_t expected[RR_REG_FIRMWARE_VERSION_SIZE] = "test-1.2.3";

    configure(RR_IDENTITY_OK);
    assert_chunks(RR_REG_FIRMWARE_VERSION_CHUNK_BASE,
                  RR_REG_FIRMWARE_VERSION_CHUNKS, expected);
}

static void test_image_range_chunks(void) {
    /* Start, then length, each little-endian: 0x10000000 and 600. */
    static const uint8_t expected[RR_REG_IMAGE_RANGE_SIZE] = {
        0x00, 0x00, 0x00, 0x10, 0x58, 0x02, 0x00, 0x00,
    };

    configure(RR_IDENTITY_OK);
    install_golden_image();
    assert_chunks(RR_REG_IMAGE_RANGE_CHUNK_BASE, RR_REG_IMAGE_RANGE_CHUNKS,
                  expected);

    /* Like 0x36, the range chunks answer even with no digest to cover. */
    rr_image_digest_init(NULL, GOLDEN_IMAGE_START, GOLDEN_IMAGE_LENGTH);
    assert_chunks(RR_REG_IMAGE_RANGE_CHUNK_BASE, RR_REG_IMAGE_RANGE_CHUNKS,
                  expected);
    install_golden_image();
}

static void test_every_chunk_readable_while_locked(void) {
    uint8_t buf[64];
    size_t length = 0u;

    /* A locked board still has to say which board and which build it is -
     * that is the reason the window is outside the gate. */
    configure(RR_IDENTITY_NONE);
    install_golden_image();
    assert(rr_identity_registers_locked());
    for (unsigned reg = RR_REG_SERIAL_CHUNK_BASE;
         reg <= RR_REG_IDENTITY_WINDOW_LAST; ++reg) {
        if (reg == 0x3fu) {
            continue;
        }
        length = 0u;
        assert(rr_identity_registers_read((uint8_t)reg, buf, &length));
        assert(length == RR_REG_CHUNK_SIZE);
    }
}

static void test_locked_without_a_valid_identity(void) {
    configure(RR_IDENTITY_NONE);
    assert(rr_identity_registers_locked());

    configure(RR_IDENTITY_CONFLICT);
    assert(rr_identity_registers_locked());

    configure(RR_IDENTITY_IO_ERROR);
    assert(rr_identity_registers_locked());

    configure(RR_IDENTITY_ALREADY_PROVISIONED);
    assert(rr_identity_registers_locked());
}

static void test_unlocked_once_provisioned(void) {
    configure(RR_IDENTITY_OK);
    assert(!rr_identity_registers_locked());
}

static void test_identity_window_readable_while_locked(void) {
    uint8_t buf[64];
    size_t length = 0u;

    configure(RR_IDENTITY_NONE);
    assert(rr_identity_registers_locked());
    assert(rr_identity_registers_read(RR_REG_IDENTITY_STATE, buf, &length));
    assert(rr_identity_registers_read(RR_REG_SERIAL, buf, &length));
    assert(rr_identity_registers_read(RR_REG_FIRMWARE_VERSION, buf, &length));
    assert(rr_identity_registers_read(RR_REG_VARIANT, buf, &length));
    assert(rr_identity_registers_read(RR_REG_FLASH_UID, buf, &length));
}

static void test_led_burst_starts_at_power_on(void) {
    /* Front-loaded deliberately: somebody who glances at the board in its
     * first seconds should see the announcement without waiting a period. */
    assert(rr_identity_led_burst_active(0u));
    assert(rr_identity_led_burst_active(RR_IDENTITY_LED_BURST_MS - 1u));
}

static void test_led_burst_ends_after_its_window(void) {
    assert(!rr_identity_led_burst_active(RR_IDENTITY_LED_BURST_MS));
    assert(!rr_identity_led_burst_active(RR_IDENTITY_LED_PERIOD_MS - 1u));
}

static void test_led_burst_repeats_every_period(void) {
    assert(rr_identity_led_burst_active(RR_IDENTITY_LED_PERIOD_MS));
    assert(rr_identity_led_burst_active(
        RR_IDENTITY_LED_PERIOD_MS + RR_IDENTITY_LED_BURST_MS - 1u));
    assert(!rr_identity_led_burst_active(
        RR_IDENTITY_LED_PERIOD_MS + RR_IDENTITY_LED_BURST_MS));

    assert(rr_identity_led_burst_active(10u * RR_IDENTITY_LED_PERIOD_MS));
    assert(!rr_identity_led_burst_active(
        (10u * RR_IDENTITY_LED_PERIOD_MS) + RR_IDENTITY_LED_BURST_MS));
}

static void test_led_burst_duty_cycle_stays_out_of_the_way(void) {
    /* The bring-up loop - load filament, read BLUE, trim, read BLUE, trim -
     * runs for minutes with somebody watching. The burst has to be short
     * enough never to obscure that readout. Sampled across a full period
     * rather than asserted against the constants, so this measures the
     * function's actual behaviour instead of restating its #defines. */
    uint32_t active = 0u;

    for (uint32_t ms = 0u; ms < RR_IDENTITY_LED_PERIOD_MS; ++ms) {
        if (rr_identity_led_burst_active(ms)) {
            ++active;
        }
    }

    assert(active == RR_IDENTITY_LED_BURST_MS);
    assert(active * 20u <= RR_IDENTITY_LED_PERIOD_MS);
}

int main(void) {
    test_reports_identity_state();
    test_reports_provisioned_serial();
    test_reports_unprovisioned_serial();
    test_pads_serial_with_nuls();
    test_reports_firmware_version();
    test_reports_variant();
    test_reports_flash_uid();
    test_reports_image_digest();
    test_reports_image_range();
    test_image_digest_register_refuses_without_a_digest();
    test_image_registers_readable_while_locked();
    test_ignores_unknown_registers();
    test_chunk_register_numbers();
    test_serial_chunks_carry_the_provisioned_serial();
    test_serial_chunks_carry_the_unprovisioned_serial();
    test_firmware_version_chunks();
    test_image_range_chunks();
    test_every_chunk_readable_while_locked();
    test_locked_without_a_valid_identity();
    test_unlocked_once_provisioned();
    test_identity_window_readable_while_locked();
    test_led_burst_starts_at_power_on();
    test_led_burst_ends_after_its_window();
    test_led_burst_repeats_every_period();
    test_led_burst_duty_cycle_stays_out_of_the_way();
    return 0;
}
