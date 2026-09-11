"""The golden vector both sides of the image digest test against.

Comment from the mcu-updater side, and the reason this file exists: "both
numbers belong in the spec rather than in two implementations that happen to
agree today." The vector below is synthetic and fully described by
`docs/roadrunner-usb-admin-protocol.md`, so the firmware, this reference and
mcu-updater each check against the document rather than against each other.
"""

import pathlib
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import uf2_image_digest as digest  # noqa: E402

import pytest  # noqa: E402


GOLDEN_START = 0x10000000
GOLDEN_LENGTH = 600
GOLDEN_DIGEST = 0xBBE38AA9


def golden_image():
    """The image bytes: `image[i] = (i * 7 + 3) & 0xff`."""
    return bytes((index * 7 + 3) & 0xFF for index in range(GOLDEN_LENGTH))


def make_uf2(chunks, family_id=digest.RP2040_FAMILY_ID):
    """Pack (address, payload) pairs into UF2 blocks."""
    blocks = bytearray()
    for number, (address, payload) in enumerate(chunks):
        block = bytearray(digest.UF2_BLOCK_SIZE)
        struct.pack_into(
            "<8I", block, 0,
            digest.UF2_MAGIC_START0, digest.UF2_MAGIC_START1,
            digest.UF2_FLAG_FAMILY_ID_PRESENT, address, len(payload),
            number, len(chunks), family_id)
        block[32:32 + len(payload)] = payload
        struct.pack_into("<I", block, 508, digest.UF2_MAGIC_END)
        blocks += block
    return bytes(blocks)


def golden_uf2():
    """The vector's container: three 256-byte payloads, the last one padded.

    600 is not a multiple of 256, so the final block carries 88 image bytes
    followed by 168 bytes of padding that never reach the digest. That is the
    whole point of the vector - a host that hashes the container, or that
    rounds the length up to the block size, gets a different number.
    """
    image = golden_image()
    chunks = []
    for offset in range(0, GOLDEN_LENGTH, 256):
        payload = bytearray(image[offset:offset + 256])
        payload += b"\xaa" * (256 - len(payload))
        chunks.append((GOLDEN_START + offset, bytes(payload)))
    return make_uf2(chunks)


def test_golden_vector_matches_the_documented_number():
    """If this fails, the document and the reference have diverged."""
    assert digest.crc32_iso_hdlc(golden_image()) == GOLDEN_DIGEST
    assert digest.uf2_image_digest(
        golden_uf2(), GOLDEN_START, GOLDEN_LENGTH) == GOLDEN_DIGEST


def test_padding_in_the_final_block_is_not_hashed():
    """The failure this vector is shaped to catch."""
    padded = digest.extract_image(golden_uf2(), GOLDEN_START, 768)
    assert digest.crc32_iso_hdlc(padded) != GOLDEN_DIGEST
    assert digest.extract_image(
        golden_uf2(), GOLDEN_START, GOLDEN_LENGTH) == golden_image()


def test_blocks_outside_the_reported_range_are_skipped():
    """A UF2 may carry more than the image - boot stages, metadata, padding."""
    stray = (0x10100000, bytes(range(256)))
    uf2 = golden_uf2() + make_uf2([stray])

    assert digest.uf2_image_digest(
        uf2, GOLDEN_START, GOLDEN_LENGTH) == GOLDEN_DIGEST


def test_a_not_main_flash_block_never_contributes():
    """Flag bit 0 means the block is not part of the flash image."""
    image = golden_image()
    chunks = [(GOLDEN_START + offset, image[offset:offset + 256].ljust(256, b"\xaa"))
              for offset in range(0, GOLDEN_LENGTH, 256)]
    uf2 = bytearray(make_uf2(chunks))

    # Append a block that overwrites the start of the image, but marked as
    # not-main-flash. Honouring it would corrupt the digest.
    poison = bytearray(make_uf2([(GOLDEN_START, b"\x00" * 256)]))
    struct.pack_into("<I", poison, 8,
                     digest.UF2_FLAG_FAMILY_ID_PRESENT
                     | digest.UF2_FLAG_NOT_MAIN_FLASH)
    uf2 += poison

    assert digest.uf2_image_digest(
        bytes(uf2), GOLDEN_START, GOLDEN_LENGTH) == GOLDEN_DIGEST


def test_a_gap_in_coverage_is_an_error_not_a_guess():
    """Filling a hole with 0xff would produce a plausible wrong number."""
    image = golden_image()
    chunks = [(GOLDEN_START, image[:256]),
              (GOLDEN_START + 512, image[512:].ljust(256, b"\xaa"))]

    with pytest.raises(digest.Uf2Error, match="0x10000100"):
        digest.uf2_image_digest(make_uf2(chunks), GOLDEN_START, GOLDEN_LENGTH)


def test_payload_size_is_read_not_assumed():
    """256 is a convention of the tooling, not a rule of the format."""
    image = golden_image()
    chunks = [(GOLDEN_START + offset, image[offset:offset + 100])
              for offset in range(0, GOLDEN_LENGTH, 100)]

    assert digest.uf2_image_digest(
        make_uf2(chunks), GOLDEN_START, GOLDEN_LENGTH) == GOLDEN_DIGEST


def test_a_truncated_container_is_rejected():
    assert digest.UF2_BLOCK_SIZE == 512
    with pytest.raises(digest.Uf2Error, match="multiple of"):
        digest.uf2_image_digest(golden_uf2()[:-1], GOLDEN_START, GOLDEN_LENGTH)


def test_digest_sizes_and_names_agree_on_their_keys():
    """The INFO payload carries the algorithm id; a host maps it with these."""
    assert digest.DIGEST_SIZES.keys() == digest.DIGEST_NAMES.keys()
    assert digest.DIGEST_SIZES[digest.DIGEST_CRC32_ISO_HDLC] == 4
