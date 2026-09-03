#ifndef USB_ADMIN_H
#define USB_ADMIN_H

#include "identity_store.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define RR_USB_ADMIN_FLASH_UID_SIZE 8u

enum {
    RR_USB_ADMIN_INFO = 0x01,
    RR_USB_ADMIN_REBOOT_BOOTSEL = 0x02,
    RR_USB_ADMIN_PROVISION_UUID = 0x03,
    RR_USB_ADMIN_CLEAR_IDENTITY = 0x04,
    RR_USB_ADMIN_OK = 0x00,
    RR_USB_ADMIN_BAD_CRC = 0x01,
    RR_USB_ADMIN_BAD_LENGTH = 0x02,
    RR_USB_ADMIN_UNPROVISIONED = 0x03,
    RR_USB_ADMIN_ALREADY_PROVISIONED = 0x04,
    RR_USB_ADMIN_IDENTITY_CONFLICT = 0x05,
    RR_USB_ADMIN_IO_ERROR = 0x06,
    RR_USB_ADMIN_CONFIRMATION_REQUIRED = 0x07,
    RR_USB_ADMIN_TRANSPORT_I2C = 1,
    RR_USB_ADMIN_TRANSPORT_UART = 2,
    RR_USB_ADMIN_TRANSPORT_USB = 3,
    RR_USB_ADMIN_LED_RGB = 1,
    RR_USB_ADMIN_LED_GRB = 2,
};

struct rr_usb_admin_config {
    rr_identity_status_t identity_status;
    const struct rr_identity *identity;
    const struct rr_identity_store *identity_store;
    const uint8_t *flash_uid;
    uint8_t transport;
    uint8_t led_order;
    const char *firmware_version;
    void *context;
    void (*write)(void *context, const uint8_t *data, size_t length);
    void (*legacy_byte)(void *context, uint8_t byte);
    void (*flush)(void *context);
    bool (*transmit_complete)(void *context);
    void (*reboot_bootsel)(void *context);
    void (*reboot_application)(void *context);
};

void rr_usb_admin_init(const struct rr_usb_admin_config *config);
void rr_usb_admin_receive(uint8_t byte);
void rr_usb_admin_poll(void);

#endif
