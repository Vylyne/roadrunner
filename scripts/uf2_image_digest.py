#!/usr/bin/env python3
"""Reference host-side implementation of the Roadrunner image digest.

The board reports a digest of the bytes it is actually executing. To compare
that against a UF2 file, the host has to reconstruct the same byte range out
of the container: a .uf2 is a stream of 512-byte blocks, each carrying a
header, up to 476 bytes of payload and a target address. A CRC of the file is
not a CRC of what lands in flash.

This module is the normative reference for that reconstruction. It is
deliberately small enough to reimplement, and
`docs/roadrunner-usb-admin-protocol.md` carries a golden vector that any
implementation - this one, mcu-updater's, the firmware's - can test against
instead of testing against each other.
"""

from __future__ import annotations

import argparse
import binascii
import struct
import sys
import typing

UF2_BLOCK_SIZE = 512
UF2_MAGIC_START0 = 0x0A324655
UF2_MAGIC_START1 = 0x9E5D5157
UF2_MAGIC_END = 0x0AB16F30

#: Block is not part of the flash image (a metadata or file-container block).
UF2_FLAG_NOT_MAIN_FLASH = 0x00000001
#: `fileSize` field holds a family id rather than a size.
UF2_FLAG_FAMILY_ID_PRESENT = 0x00002000

RP2040_FAMILY_ID = 0xE48BFF56

#: Algorithm ids as they appear in the INFO payload's digest-algorithm byte.
DIGEST_NONE = 0
DIGEST_CRC32_ISO_HDLC = 1

DIGEST_SIZES : dict[int, int] = {DIGEST_NONE: 0, DIGEST_CRC32_ISO_HDLC: 4}
DIGEST_NAMES : dict[int, str] = {
    DIGEST_NONE: "none",
    DIGEST_CRC32_ISO_HDLC: "crc32-iso-hdlc",
}


class Uf2Error(Exception):
    """The container could not be reconstructed over the requested range."""


def crc32_iso_hdlc(data : bytes) -> int:
    """CRC-32/ISO-HDLC, the variant `zlib.crc32` computes.

    Reflected polynomial 0xEDB88320, initial value 0xFFFFFFFF, reflected in
    and out, final XOR 0xFFFFFFFF. Named rather than described as "CRC32"
    because "CRC32" alone names at least half a dozen incompatible things.
    """
    return binascii.crc32(data) & 0xFFFFFFFF


def extract_image(uf2 : bytes, start : int, length : int) -> bytes:
    """Reconstruct the flash bytes a UF2 places in `[start, start + length)`.

    `start` and `length` are the values the *board* reported. A host must not
    substitute its own assumption about where the image begins or how long it
    is: the linked image does not end on a block boundary, so the final UF2
    block is padded and only the reported length says where to stop.
    """
    if length <= 0:
        raise Uf2Error(f"image length must be positive, got {length}")
    if len(uf2) % UF2_BLOCK_SIZE != 0:
        raise Uf2Error(
            f"not a UF2 container: {len(uf2)} bytes is not a multiple of "
            f"{UF2_BLOCK_SIZE}")

    image = bytearray(b"\xff" * length)
    covered = bytearray(length)

    for offset in range(0, len(uf2), UF2_BLOCK_SIZE):
        block = uf2[offset:offset + UF2_BLOCK_SIZE]
        magic0, magic1, flags, target, payload_size, _block_no, _blocks, _file = \
            struct.unpack_from("<8I", block, 0)
        magic_end, = struct.unpack_from("<I", block, 508)

        if magic0 != UF2_MAGIC_START0 or magic1 != UF2_MAGIC_START1 \
                or magic_end != UF2_MAGIC_END:
            raise Uf2Error(f"block at byte {offset} has bad magic")
        if flags & UF2_FLAG_NOT_MAIN_FLASH:
            continue
        if payload_size > UF2_BLOCK_SIZE - 36:
            raise Uf2Error(
                f"block at byte {offset} claims a {payload_size}-byte payload")

        # Payloads are usually 256 bytes on an RP2040, but that is a
        # convention of the tooling, not a rule of the format. Read the field.
        for index in range(payload_size):
            address = target + index
            if not start <= address < start + length:
                continue
            image[address - start] = block[32 + index]
            covered[address - start] = 1

    if not all(covered):
        missing = covered.index(0)
        raise Uf2Error(
            f"UF2 does not cover the whole image: no block supplies "
            f"{start + missing:#010x}")

    return bytes(image)


def uf2_image_digest(uf2 : bytes, start : int, length : int,
                     algorithm : int = DIGEST_CRC32_ISO_HDLC) -> int:
    """The digest a board running this UF2 should report."""
    if algorithm != DIGEST_CRC32_ISO_HDLC:
        raise Uf2Error(f"unsupported digest algorithm {algorithm}")
    return crc32_iso_hdlc(extract_image(uf2, start, length))


def main(argv : typing.Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("uf2", help="the .uf2 file to digest")
    parser.add_argument(
        "--start", type=lambda v: int(v, 0), required=True,
        help="image start address, as reported by INFO (e.g. 0x10000000)")
    parser.add_argument(
        "--length", type=lambda v: int(v, 0), required=True,
        help="image length in bytes, as reported by INFO")
    args = parser.parse_args(argv)

    with open(args.uf2, "rb") as handle:
        uf2 = handle.read()

    try:
        digest = uf2_image_digest(uf2, args.start, args.length)
    except Uf2Error as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"crc32-iso-hdlc {digest:#010x}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
