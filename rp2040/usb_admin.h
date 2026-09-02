#ifndef USB_ADMIN_H
#define USB_ADMIN_H

#include "identity_store.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define RR_USB_ADMIN_FLASH_UID_SIZE 8u

enum {
    RR_USB_ADMIN_TRANSPORT_I2C = 1,
    RR_USB_ADMIN_TRANSPORT_UART = 2,
    RR_USB_ADMIN_TRANSPORT_USB = 3,
    RR_USB_ADMIN_LED_RGB = 1,
    RR_USB_ADMIN_LED_GRB = 2,
};

struct rr_usb_admin_config {
    rr_identity_status_t identity_status;
    const struct rr_identity *identity;
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
};

void rr_usb_admin_init(const struct rr_usb_admin_config *config);
void rr_usb_admin_receive(uint8_t byte);
void rr_usb_admin_poll(void);

#endif
