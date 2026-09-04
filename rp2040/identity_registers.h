#ifndef IDENTITY_REGISTERS_H
#define IDENTITY_REGISTERS_H

#include "identity_record.h"
#include "usb_descriptor_strings.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* The identity window. Chosen to clear the sensor map, which uses 0x10 and
 * 0x20-0x24 (0x20 is reserved for the commented-out READ_HEALTH). These five
 * registers are readable whether or not the board is provisioned - they are
 * both the unprovisioned allow-list and the steady-state identity source.
 * Field order mirrors the USB admin INFO payload deliberately, so
 * docs/roadrunner-usb-admin-protocol.md stays the single definition of what a
 * Roadrunner's identity is, with INFO and this window as two encodings. */
enum {
    RR_REG_IDENTITY_STATE = 0x30,
    RR_REG_SERIAL = 0x31,
    RR_REG_FIRMWARE_VERSION = 0x32,
    RR_REG_VARIANT = 0x33,
    RR_REG_FLASH_UID = 0x34,
};

/* RR_USB_SERIAL_MAX_LENGTH is 34; the longest real serial,
 * RR-UNPROVISIONED-<16 hex>, is 33. NUL-padded to the full width so a host
 * reads a fixed-size register rather than having to frame a variable one. */
#define RR_REG_SERIAL_SIZE RR_USB_SERIAL_MAX_LENGTH
#define RR_REG_FIRMWARE_VERSION_SIZE 32u
#define RR_REG_VARIANT_SIZE 2u
#define RR_REG_FLASH_UID_SIZE RR_USB_FLASH_UID_SIZE

struct rr_identity_registers_config {
    rr_identity_status_t identity_status;
    const struct rr_identity *identity;
    const uint8_t *flash_uid; /* RR_REG_FLASH_UID_SIZE bytes */
    uint8_t transport;
    uint8_t led_order;
    const char *firmware_version;
};

void rr_identity_registers_init(
    const struct rr_identity_registers_config *config);

/* Fills `buf` and `length` and returns true when `reg` is an identity
 * register. Returns false and touches neither argument otherwise, so a caller
 * can fall through to its own register handling. */
bool rr_identity_registers_read(uint8_t reg, uint8_t *buf, size_t *length);

/* True when the board has no valid identity and must not serve sensor data to
 * a host. Every status other than RR_IDENTITY_OK locks: NONE has no identity,
 * CONFLICT cannot say which of two it is, and IO_ERROR could not find out.
 * ALREADY_PROVISIONED is not a load result, but it locks too rather than
 * defaulting open - see docs/roadrunner-identity-gate-design.md. */
bool rr_identity_registers_locked(void);

/* Unprovisioned announcement cadence. The bring-up procedure reads the LED for
 * minutes at a time - load filament, read BLUE, trim the lever arm, read BLUE,
 * trim again, until GREEN - so a boot-only announcement would be missed and a
 * continuous one would fight the readout somebody is actually using. A short
 * burst on a long period is seen several times without ever obscuring it.
 *
 * RR_IDENTITY_LED_BLINK_MS is deliberately faster than the 100 ms RED magnet
 * blink in main.c, so the two read as different channels rather than as a new
 * sensor state - hue alone is too weak a signal between amber and red. */
#define RR_IDENTITY_LED_BURST_MS 1500u
#define RR_IDENTITY_LED_PERIOD_MS 30000u
#define RR_IDENTITY_LED_BLINK_MS 50u

bool rr_identity_led_burst_active(uint32_t now_ms);

#endif
