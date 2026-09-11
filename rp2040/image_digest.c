#include "image_digest.h"

/* Nibble-wide table: 64 bytes of flash instead of the usual 1 KB, two lookups
 * per byte instead of eight shift-and-test rounds. On a 125 MHz M0+ that is
 * the difference between roughly 80 ms and roughly 16 ms for a 200 KB image,
 * which is worth 64 bytes. */
static const uint32_t rr_image_crc32_table[16] = {
    0x00000000u, 0x1db71064u, 0x3b6e20c8u, 0x26d930acu,
    0x76dc4190u, 0x6b6b51f4u, 0x4db26158u, 0x5005713cu,
    0xedb88320u, 0xf00f9344u, 0xd6d6a3e8u, 0xcb61b38cu,
    0x9b64c2b0u, 0x86d3d2d4u, 0xa00ae278u, 0xbdbdf21cu,
};

static struct rr_image_digest rr_image_digest_state = {
    .algorithm = RR_IMAGE_DIGEST_NONE,
};
static const uint8_t *rr_image_digest_image;

uint32_t rr_image_crc32(const uint8_t *data, size_t length)
{
    uint32_t crc = 0xffffffffu;
    size_t index;

    if (data == NULL) {
        return 0u;
    }

    for (index = 0; index < length; ++index) {
        crc ^= data[index];
        crc = (crc >> 4) ^ rr_image_crc32_table[crc & 0x0fu];
        crc = (crc >> 4) ^ rr_image_crc32_table[crc & 0x0fu];
    }

    return crc ^ 0xffffffffu;
}

void rr_image_digest_init(const uint8_t *image, uint32_t start,
                          uint32_t length)
{
    rr_image_digest_image = image;
    rr_image_digest_state.algorithm = RR_IMAGE_DIGEST_NONE;
    rr_image_digest_state.digest = 0u;
    rr_image_digest_state.start = start;
    rr_image_digest_state.length = length;
}

const struct rr_image_digest *rr_image_digest_get(void)
{
    if (rr_image_digest_state.algorithm == RR_IMAGE_DIGEST_NONE
            && rr_image_digest_image != NULL
            && rr_image_digest_state.length != 0u) {
        rr_image_digest_state.digest = rr_image_crc32(
            rr_image_digest_image, (size_t)rr_image_digest_state.length);
        rr_image_digest_state.algorithm = RR_IMAGE_DIGEST_CRC32;
    }

    /* A board with no image range configured reports algorithm NONE rather
     * than a zero digest: 0x00000000 is a perfectly legal CRC, so "no digest"
     * and "the digest is zero" must not look alike on the wire. */
    return &rr_image_digest_state;
}
