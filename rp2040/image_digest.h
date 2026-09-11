#ifndef IMAGE_DIGEST_H
#define IMAGE_DIGEST_H

#include <stddef.h>
#include <stdint.h>

/* A digest of the bytes this board is executing.
 *
 * The point is to catch a bad or mismatched flash write: a truncated BOOTSEL
 * copy that boots and behaves, or a board carrying a build nobody expected.
 * It is NOT attestation. The firmware computing the digest is the firmware in
 * question, so anything able to replace the image can replace this code and
 * report whatever it likes. Evidence against accident, never against
 * substitution - see docs/roadrunner-usb-admin-protocol.md. */

#define RR_IMAGE_DIGEST_NONE 0u
/* CRC-32/ISO-HDLC: reflected polynomial 0xEDB88320, init 0xFFFFFFFF,
 * reflected in and out, final XOR 0xFFFFFFFF. What zlib.crc32 computes.
 * "CRC32" alone names several mutually incompatible functions, which is why
 * the wire value is a labelled algorithm id and not a bare number. */
#define RR_IMAGE_DIGEST_CRC32 1u

#define RR_IMAGE_DIGEST_SIZE 4u
#define RR_IMAGE_RANGE_SIZE 8u

struct rr_image_digest {
    uint8_t algorithm;
    uint32_t digest;
    uint32_t start;  /* XIP address the digest covers from */
    uint32_t length; /* bytes covered */
};

uint32_t rr_image_crc32(const uint8_t *data, size_t length);

/* `image` is where this process can read the bytes; `start` is the address a
 * host should record for them. On the device they are the same XIP pointer,
 * but keeping them separate is what lets a host test digest a plain buffer
 * while claiming a realistic flash address. */
void rr_image_digest_init(const uint8_t *image, uint32_t start,
                          uint32_t length);

/* Computed on first call and cached. The cost is one pass over the image -
 * around 16 ms for a 200 KB build on a 125 MHz M0+ - which is why it is not
 * paid at boot for a board nobody ever asks. */
const struct rr_image_digest *rr_image_digest_get(void);

#endif
