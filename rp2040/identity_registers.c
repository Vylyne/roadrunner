#include "identity_registers.h"

#include <string.h>

static struct rr_identity_registers_config rr_identity_registers_config;

void rr_identity_registers_init(
    const struct rr_identity_registers_config *config)
{
    if (config == NULL) {
        return;
    }
    rr_identity_registers_config = *config;
}

/* Copy `source` into `buf`, NUL-padding to `size`. `strncpy` would do this,
 * but it also silently truncates without a terminator when the source is
 * exactly `size` long - every field here is read back as a C string by the
 * host, so the terminator is not optional. */
static void rr_identity_registers_copy_string(uint8_t *buf, size_t size,
                                              const char *source)
{
    size_t length = 0u;

    memset(buf, 0, size);
    if (source == NULL) {
        return;
    }
    while (length < (size - 1u) && source[length] != '\0') {
        buf[length] = (uint8_t)source[length];
        ++length;
    }
}

bool rr_identity_registers_read(uint8_t reg, uint8_t *buf, size_t *length)
{
    struct rr_usb_descriptor_strings strings;

    if (buf == NULL || length == NULL) {
        return false;
    }

    switch (reg) {
    case RR_REG_IDENTITY_STATE:
        buf[0] = (uint8_t)rr_identity_registers_config.identity_status;
        *length = 1u;
        return true;

    case RR_REG_SERIAL:
        /* The same builder the USB string descriptor uses, so the serial a
         * host reads over I2C or UART is byte-identical to the one it reads
         * off the USB descriptor - there is only one serial. */
        rr_usb_descriptor_strings_build(
            &strings, rr_identity_registers_config.identity_status,
            rr_identity_registers_config.identity,
            rr_identity_registers_config.flash_uid);
        rr_identity_registers_copy_string(buf, RR_REG_SERIAL_SIZE,
                                          strings.serial);
        *length = RR_REG_SERIAL_SIZE;
        return true;

    case RR_REG_FIRMWARE_VERSION:
        rr_identity_registers_copy_string(
            buf, RR_REG_FIRMWARE_VERSION_SIZE,
            rr_identity_registers_config.firmware_version);
        *length = RR_REG_FIRMWARE_VERSION_SIZE;
        return true;

    case RR_REG_VARIANT:
        buf[0] = rr_identity_registers_config.transport;
        buf[1] = rr_identity_registers_config.led_order;
        *length = RR_REG_VARIANT_SIZE;
        return true;

    case RR_REG_FLASH_UID:
        if (rr_identity_registers_config.flash_uid == NULL) {
            memset(buf, 0, RR_REG_FLASH_UID_SIZE);
        } else {
            memcpy(buf, rr_identity_registers_config.flash_uid,
                   RR_REG_FLASH_UID_SIZE);
        }
        *length = RR_REG_FLASH_UID_SIZE;
        return true;

    default:
        return false;
    }
}
