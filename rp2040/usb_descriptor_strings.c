#include "usb_descriptor_strings.h"

#include <string.h>

void rr_usb_descriptor_strings_build(
    struct rr_usb_descriptor_strings *strings,
    rr_identity_status_t identity_status,
    const struct rr_identity *identity,
    const uint8_t flash_uid[RR_USB_FLASH_UID_SIZE]) {
    static const char hex[] = "0123456789ABCDEF";

    if (strings == NULL) {
        return;
    }
    if (identity_status == RR_IDENTITY_OK && identity != NULL) {
        rr_identity_serial(identity->uuid, strings->serial);
        return;
    }

    memcpy(strings->serial, "RR1-UNPROVISIONED-", 18u);
    for (unsigned int index = 0; index < RR_USB_FLASH_UID_SIZE; ++index) {
        strings->serial[18u + index * 2u] = hex[flash_uid[index] >> 4u];
        strings->serial[19u + index * 2u] = hex[flash_uid[index] & 0x0fu];
    }
    strings->serial[RR_USB_SERIAL_MAX_LENGTH] = '\0';
}
