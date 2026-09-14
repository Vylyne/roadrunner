#ifndef IDENTITY_REGISTERS_H
#define IDENTITY_REGISTERS_H

#include "identity_record.h"
#include "image_digest.h"
#include "usb_descriptor_strings.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* The identity window. Chosen to clear the sensor map, which uses 0x10 and
 * 0x20-0x25 (0x20 is reserved for the commented-out READ_HEALTH). These
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
    /* Not identity, but the same window and the same rule: readable whether
     * or not the board is provisioned, because "which build is this" is a
     * question you most need answered about a board that is refusing. */
    RR_REG_IMAGE_DIGEST = 0x35,
    RR_REG_IMAGE_RANGE = 0x36,

    /* The wide fields again, as runs of four-byte chunks: chunk n of a field
     * is register base + n and returns bytes [4n, 4n + 4) of the register
     * above. Four bytes is Klipper's tmcuart ceiling, so these are what let a
     * UART host read the fields at all. Stateless on purpose - no cursor to
     * fight over between bus masters, and every chunk read can be retried on
     * its own. 0x3F is reserved and unassigned; nothing in the window may be
     * 0x52 (the admin sync byte) or pass 0x7F (tmc_uart's write bit). See
     * docs/roadrunner-uart-chunked-identity-design.md. */
    RR_REG_SERIAL_CHUNK_BASE = 0x37,
    RR_REG_FIRMWARE_VERSION_CHUNK_BASE = 0x40,
    RR_REG_IMAGE_RANGE_CHUNK_BASE = 0x48,
    RR_REG_IDENTITY_WINDOW_LAST = 0x49,
};

#define RR_REG_CHUNK_SIZE 4u

/* RR_USB_SERIAL_MAX_LENGTH is 32; the longest real serial, a provisioned
 * RR-<26 base32>, is 29. NUL-padded to the full width so a host
 * reads a fixed-size register rather than having to frame a variable one. */
#define RR_REG_SERIAL_SIZE RR_USB_SERIAL_MAX_LENGTH
#define RR_REG_FIRMWARE_VERSION_SIZE 32u
#define RR_REG_VARIANT_SIZE 2u
#define RR_REG_FLASH_UID_SIZE RR_USB_FLASH_UID_SIZE
/* Four bytes is the Klipper tmcuart ceiling, so 0x35 is fixed to one
 * algorithm by its definition instead of spending a byte labelling it - that
 * is what lets a UART host read the digest at all. A different algorithm
 * would take a different register number. 0x36 is eight bytes; UART hosts read
 * it through its chunks at 0x48-0x49. */
#define RR_REG_IMAGE_DIGEST_SIZE RR_IMAGE_DIGEST_SIZE
#define RR_REG_IMAGE_RANGE_SIZE RR_IMAGE_RANGE_SIZE

#define RR_REG_SERIAL_CHUNKS (RR_REG_SERIAL_SIZE / RR_REG_CHUNK_SIZE)
#define RR_REG_FIRMWARE_VERSION_CHUNKS \
    (RR_REG_FIRMWARE_VERSION_SIZE / RR_REG_CHUNK_SIZE)
#define RR_REG_IMAGE_RANGE_CHUNKS (RR_REG_IMAGE_RANGE_SIZE / RR_REG_CHUNK_SIZE)

/* Each chunk run must cover its field exactly and stop short of the next run.
 * A field that grows past its run would silently lose its tail over UART. */
_Static_assert(RR_REG_SERIAL_SIZE % RR_REG_CHUNK_SIZE == 0u
               && RR_REG_SERIAL_CHUNK_BASE + RR_REG_SERIAL_CHUNKS
                  <= RR_REG_FIRMWARE_VERSION_CHUNK_BASE,
               "SERIAL must fit its chunk run");
_Static_assert(RR_REG_FIRMWARE_VERSION_SIZE % RR_REG_CHUNK_SIZE == 0u
               && RR_REG_FIRMWARE_VERSION_CHUNK_BASE
                  + RR_REG_FIRMWARE_VERSION_CHUNKS
                  <= RR_REG_IMAGE_RANGE_CHUNK_BASE,
               "FIRMWARE_VERSION must fit its chunk run");
_Static_assert(RR_REG_IMAGE_RANGE_SIZE % RR_REG_CHUNK_SIZE == 0u
               && RR_REG_IMAGE_RANGE_CHUNK_BASE + RR_REG_IMAGE_RANGE_CHUNKS - 1u
                  == RR_REG_IDENTITY_WINDOW_LAST,
               "IMAGE_RANGE must end the window");

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
 * register or chunk. Returns false and touches neither argument otherwise, so
 * a caller can fall through to its own register handling. `buf` must hold the
 * widest register, RR_REG_SERIAL_SIZE bytes. */
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
