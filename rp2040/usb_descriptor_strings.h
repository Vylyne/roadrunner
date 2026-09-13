#ifndef USB_DESCRIPTOR_STRINGS_H
#define USB_DESCRIPTOR_STRINGS_H

#include "identity_record.h"

#include <stdint.h>

#define RR_USB_MANUFACTURER "Vylyne"
#define RR_USB_PRODUCT "Roadrunner"
#define RR_USB_FLASH_UID_SIZE 8u
/* The widest serial is the provisioned RR-<26 base32>, 29 characters. 32 is an
 * exact eight-chunk fit for the four-byte UART chunk registers. */
#define RR_USB_SERIAL_MAX_LENGTH 32u

struct rr_usb_descriptor_strings {
    char serial[RR_USB_SERIAL_MAX_LENGTH + 1u];
};

void rr_usb_descriptor_strings_build(
    struct rr_usb_descriptor_strings *strings,
    rr_identity_status_t identity_status,
    const struct rr_identity *identity);

#endif
