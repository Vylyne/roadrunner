/* The firmware's half of the image-digest golden vector.
 *
 * The board computes a number and a host computes a number, and the only
 * thing that makes them the same number is that both were written against
 * docs/roadrunner-usb-admin-protocol.md. This test and
 * tests/test_uf2_image_digest.py check the same documented vector from the
 * two sides, so a drift shows up here rather than on a bench six months from
 * now with nobody able to say which side is wrong. */

#include "image_digest.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* docs/roadrunner-usb-admin-protocol.md, "Golden vector": 600 bytes at
 * 0x10000000 where byte i is (i * 7 + 3) & 0xff. */
#define GOLDEN_START 0x10000000u
#define GOLDEN_LENGTH 600u
#define GOLDEN_DIGEST 0xbbe38aa9u

static int failures;

static void check(int condition, const char *what)
{
    if (!condition) {
        printf("FAIL: %s\n", what);
        ++failures;
    }
}

static void fill_golden(uint8_t *image)
{
    size_t index;

    for (index = 0; index < GOLDEN_LENGTH; ++index) {
        image[index] = (uint8_t)((index * 7u + 3u) & 0xffu);
    }
}

static void test_golden_vector(void)
{
    uint8_t image[GOLDEN_LENGTH];

    fill_golden(image);
    check(rr_image_crc32(image, GOLDEN_LENGTH) == GOLDEN_DIGEST,
          "CRC-32/ISO-HDLC of the documented image is 0xbbe38aa9");
}

static void test_known_crc32_vectors(void)
{
    /* The standard check values for this CRC variant. If the table or the
     * nibble loop is wrong, these fail before anything Roadrunner-specific
     * does, which says "the CRC is wrong" rather than "the image is wrong". */
    check(rr_image_crc32((const uint8_t *)"", 0u) == 0x00000000u,
          "empty input hashes to zero");
    check(rr_image_crc32((const uint8_t *)"123456789", 9u) == 0xcbf43926u,
          "the standard check string hashes to 0xcbf43926");
    check(rr_image_crc32((const uint8_t *)"a", 1u) == 0xe8b7be43u,
          "\"a\" hashes to 0xe8b7be43");
}

static void test_digest_is_cached_not_recomputed(void)
{
    uint8_t image[GOLDEN_LENGTH];
    const struct rr_image_digest *first;
    const struct rr_image_digest *second;

    fill_golden(image);
    rr_image_digest_init(image, GOLDEN_START, GOLDEN_LENGTH);

    first = rr_image_digest_get();
    check(first->algorithm == RR_IMAGE_DIGEST_CRC32, "algorithm is labelled");
    check(first->digest == GOLDEN_DIGEST, "first read computes the digest");
    check(first->start == GOLDEN_START, "start is reported as configured");
    check(first->length == GOLDEN_LENGTH, "length is reported as configured");

    /* Scribble on the buffer. A cached digest does not change; a recomputed
     * one does, and would cost 16 ms of every INFO. */
    memset(image, 0, sizeof(image));
    second = rr_image_digest_get();
    check(second->digest == GOLDEN_DIGEST, "second read is served from cache");
}

static void test_no_image_is_not_a_zero_digest(void)
{
    const struct rr_image_digest *digest;

    /* 0x00000000 is a legal CRC value, so a board that could not compute one
     * must say so with the algorithm byte rather than reporting zero and
     * letting a host compare it against a real digest. */
    rr_image_digest_init(NULL, GOLDEN_START, GOLDEN_LENGTH);
    digest = rr_image_digest_get();
    check(digest->algorithm == RR_IMAGE_DIGEST_NONE,
          "no image means algorithm NONE");

    rr_image_digest_init((const uint8_t *)"unused", GOLDEN_START, 0u);
    digest = rr_image_digest_get();
    check(digest->algorithm == RR_IMAGE_DIGEST_NONE,
          "a zero-length range means algorithm NONE");
    check(digest->start == GOLDEN_START,
          "the range is still reported when the digest is not");
}

int main(void)
{
    test_known_crc32_vectors();
    test_golden_vector();
    test_digest_is_cached_not_recomputed();
    test_no_image_is_not_a_zero_digest();

    if (failures != 0) {
        printf("%d check(s) failed\n", failures);
        return EXIT_FAILURE;
    }
    printf("image digest tests passed\n");
    return EXIT_SUCCESS;
}
