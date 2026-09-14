# Roadrunner direct USB administration protocol

This document is the wire-level contract for the Roadrunner direct USB CDC
maintenance endpoint. It is independent of the sensor data protocols carried
over I2C, UART, and the legacy USB stream.

## Framing

Every request and response has this form:

```text
52 52 01 opcode payload_length payload... crc8
```

The two `52` bytes are the sync marker. Version `01` is the administration
protocol version. `payload_length` is one byte and must be at most 64 for a
request. CRC-8/ATM is calculated over every byte before the CRC byte, with
polynomial `07`, initial value `00`, no reflection, and final XOR `00`.

Responses copy the request opcode with bit `80` set. A response payload starts
with a one-byte status. A malformed frame receives a response for the parsed
opcode when possible; no state-changing operation is performed unless the
complete frame and CRC are valid.

The empty requests are:

```text
INFO:            52 52 01 01 00 90
REBOOT_BOOTSEL: 52 52 01 02 00 AF
```

`PROVISION_UUID` and `CLEAR_IDENTITY` successful responses cause an
application reset after the complete response is sent, the CDC transmit queue
is flushed, and transmit completion is observed; the device then
re-enumerates as its application CDC device. A successful `REBOOT_BOOTSEL`
response follows the same ACK-completion rule but enters the RP2040 ROM
BOOTSEL handoff and re-enumerates as the `RPI-RP2` mass-storage device. `INFO`
and every error or refusal response do not reset.

## Opcodes and status values

| Request | Opcode | Request payload | Successful response payload |
| --- | ---: | --- | --- |
| `INFO` | `01` | empty | INFO payload below |
| `REBOOT_BOOTSEL` | `02` | empty | `00` |
| `PROVISION_UUID` | `03` | exactly 16 raw UUID bytes | `00`, serial length, serial bytes |
| `CLEAR_IDENTITY` | `04` | exactly `52 52 43 4C` (`RRCL`) | `00` |

The status byte values are:

| Value | Name | Meaning |
| ---: | --- | --- |
| `00` | `OK` | Operation succeeded |
| `01` | `BAD_CRC` | Frame CRC did not match |
| `02` | `BAD_LENGTH` | Opcode payload length was invalid, or exceeded 64 |
| `03` | `UNPROVISIONED` | INFO status: no valid identity is stored, **or** refusal: this opcode requires a valid identity |
| `04` | `ALREADY_PROVISIONED` | Provisioning was refused because an identity exists |
| `05` | `IDENTITY_CONFLICT` | Two valid identity records disagree |
| `06` | `STORE_IO_ERROR` | Flash erase, program, or verification failed |
| `07` | `CONFIRMATION_REQUIRED` | Clear payload was not exactly `RRCL` |

`PROVISION_UUID` is accepted only when the identity store is unprovisioned.
It never replaces an existing identity, including when the UUID is identical.
`CLEAR_IDENTITY` is an explicit maintenance action and erases the reserved
identity sector only after the confirmation payload matches exactly.

### Status `03` has two roles

`03` (`UNPROVISIONED`) is overloaded, and a host must not confuse its two
shapes. As an **INFO status**, it is byte 0 of a full, multi-field INFO
payload (see below) — it means "no valid identity is stored," not a refusal.
As a **refusal**, it is the entire response payload: a single status byte,
returned instead of an opcode's normal successful payload, meaning "this
opcode requires a valid identity and was not performed." A host that reads
`03` must first check which opcode the response echoes; only an `INFO` reply
carries the longer shape.

The opcode gate, by identity-store state:

| Opcode | `NONE` | `CONFLICT` / `IO_ERROR` | `OK` |
| --- | --- | --- | --- |
| `INFO` | allowed | allowed | allowed |
| `PROVISION_UUID` | allowed | allowed (store decides) | refused `04` |
| `CLEAR_IDENTITY` | refused `03` | allowed (store decides) | allowed |
| `REBOOT_BOOTSEL` | allowed | allowed | allowed |

`REBOOT_BOOTSEL` is not gated on identity status at all. An earlier revision
refused it unless the status was exactly `OK`, on the reasoning that there is
no reason to reflash a board that cannot be used. That reasoning was
backwards: a board that cannot be used over USB admin is precisely the one
that needs reflashing, and a conflicted board (see below) has no other
software recovery path. Reflashing is maintenance, not the sensor-serving
behaviour the identity gate protects, and normal UF2 updates preserve the
identity sector, so allowing `REBOOT_BOOTSEL` unconditionally is not an
identity bypass.

`CLEAR_IDENTITY`'s gate is asymmetric with the other opcodes: it is refused
only when the status is `NONE` — there is genuinely nothing to erase. For
every other status the opcode is *admitted*, but the identity store still
decides whether it can actually clear:

- **`CONFLICT` is never recoverable by clear.** The store refuses to erase a
  sector it cannot safely interpret, so `CLEAR_IDENTITY` returns `05`
  (`IDENTITY_CONFLICT`) and leaves the sector byte-for-byte unchanged.
  `REBOOT_BOOTSEL` — unconditionally allowed, as above — is the recovery path
  for a conflicted board.
- **`IO_ERROR` is sometimes recoverable.** A failure while re-reading the
  sector to verify a successful erase reaches the erase step and may still
  clear the record. A failure while reading the sector in the first place
  does not reach the erase step at all, and returns `06` (`STORE_IO_ERROR`)
  with nothing changed. A host cannot tell these apart from the response
  alone; retrying `CLEAR_IDENTITY` is reasonable, and `REBOOT_BOOTSEL` remains
  available regardless.

## INFO response

The INFO response payload fields occur in this order:

| Field | Size | Values / encoding |
| --- | ---: | --- |
| status | 1 | `00` when provisioned, otherwise `03` |
| identity-store state | 1 | `0` none, `1` provisioned, `2` conflict, `3` already provisioned, `4` I/O error |
| transport | 1 | `1` I2C, `2` UART, `3` USB |
| LED order | 1 | `1` RGB, `2` GRB |
| model length + model | 1 + up to 13 | ASCII `roadrunner-v1` |
| firmware version length + version | 1 + up to 32 | ASCII, normally CMake `dev` |
| serial length + serial | 1 + up to 29 | ASCII serial namespace below |
| flash UID | 8 | Raw diagnostic bytes, not identity |
| digest algorithm | 1 | `0` none, `1` CRC-32/ISO-HDLC (see below) |
| digest length + digest | 1 + up to 4 | Digest value, little-endian |
| image start | 4 | Little-endian XIP address the digest covers from |
| image length | 4 | Little-endian byte count the digest covers |

The maximum INFO payload is 103 bytes with the current field limits, within
the 128-byte response limit. Integers other than the two explicitly
little-endian 32-bit fields are unsigned bytes; strings are not
NUL-terminated in the frame.

**The response limit was 96 bytes before the digest fields existed**, and
this revision raises it to 128. A host must parse `payload_length` and read
that many bytes rather than sizing a fixed buffer; one that hard-codes 96
truncates INFO on a board built after this revision. That is also how a host
detects the fields' absence: a pre-revision board returns a 93-byte payload
with nothing after the flash UID, not a digest algorithm of `0`. A board that
reports algorithm `0` is a current board that could not compute a digest.

## Firmware image digest

The board reports a digest of the bytes it is executing. This detects a bad
or mismatched flash write - a truncated BOOTSEL copy that boots and behaves,
or a board carrying a build nobody expected. **It is not attestation.** The
firmware computing the digest is the firmware in question, so anything able
to replace the image can replace the hasher and report whatever it likes. The
digest is evidence against accident, never against substitution; no trust
claim should be built on it.

### The algorithm

Algorithm `1` is **CRC-32/ISO-HDLC**: reflected polynomial `0xEDB88320`,
initial value `0xFFFFFFFF`, reflected input and output, final XOR
`0xFFFFFFFF`. This is what Python's `zlib.crc32` and `binascii.crc32`
compute. "CRC32" on its own names at least half a dozen mutually
incompatible functions and is not a specification; implementations must check
against the golden vector below rather than against each other.

Algorithm `0` means the board has no digest to report. Its digest length is
`0` and no digest bytes follow, but `image start` and `image length` are
still present and still valid.

### The byte range

The digest covers `[image start, image start + image length)` as the board
reports them, which on current firmware is `__flash_binary_start` through
`__flash_binary_end` - the linked application image, from the XIP base at
`0x10000000` to the end of the last linked section.

The reserved identity sector at flash offset `0x1FF000` is **outside** that
range: the application region is capped at `0x1FF000`
(`ROADRUNNER_APPLICATION_FLASH_SIZE`), so the sector begins exactly where the
image must already have ended. Provisioning, clearing and re-provisioning a
board therefore never change its digest, and two boards flashed from the same
UF2 report the same number whatever their identity.

**A host must use the reported start and length and must not substitute its
own.** The linked image does not end on a 256-byte boundary, so the final UF2
block is padded; only the reported length says where the image stops.

### Reconstructing the range from a UF2

A `.uf2` is a container - 512-byte blocks each carrying a 32-byte header, up
to 476 bytes of payload, a target address and a trailing magic - so a digest
of the file is not a digest of what lands in flash. A host comparing a board
against a UF2 must, for each block:

1. Verify the three magic values; reject the container otherwise.
2. Skip the block if flag bit `0` (`NOT_MAIN_FLASH`) is set.
3. Read `payloadSize` from the header. **Do not assume 256** - that is a
   convention of the tooling, not a rule of the format.
4. Place payload byte `i` at address `targetAddr + i`, discarding any byte
   that falls outside `[start, start + length)`.

Then require that every byte of the range was supplied by some block, and
digest exactly `length` bytes. A gap is an error, not a hole to fill with
`0xFF`: filling produces a plausible wrong number instead of a visible
failure.

`scripts/uf2_image_digest.py` is the reference implementation of exactly
this, and is small enough to reimplement.

### Golden vector

Both sides test against this vector, not against each other.

The image is 600 bytes at start address `0x10000000`, where byte `i` is
`(i * 7 + 3) & 0xff`. Its CRC-32/ISO-HDLC is:

```text
0xBBE38AA9
```

Packed into a UF2 as three blocks with 256-byte payloads at `0x10000000`,
`0x10000100` and `0x10000200`, the third block's payload is 88 image bytes
followed by 168 bytes of padding. A correct reconstruction discards that
padding and reports `0xBBE38AA9`; a host that digests the container, or that
rounds the length up to 768, does not. `tests/test_uf2_image_digest.py`
builds the vector and checks both outcomes.

## Identity register window

A fixed range of registers exposes the same identity fields as `INFO`,
directly on the sensor transport itself:

| Register | Name | Size | Content |
| ---: | --- | ---: | --- |
| `0x30` | `READ_IDENTITY_STATE` | 1 | `rr_identity_status_t` — `0` none, `1` ok, `2` conflict, `4` I/O error (this register reports `rr_identity_load()`'s result; `3` already provisioned is part of the enum but is only returned by `rr_identity_provision()`, so it is never observable here) |
| `0x31` | `READ_SERIAL` | 32 | ASCII, NUL-padded |
| `0x32` | `READ_FIRMWARE_VERSION` | 32 | ASCII, NUL-padded |
| `0x33` | `READ_VARIANT` | 2 | transport byte, LED-order byte |
| `0x34` | `READ_FLASH_UID` | 8 | Raw diagnostic bytes |
| `0x35` | `READ_IMAGE_DIGEST` | 4 | CRC-32/ISO-HDLC of the image, little-endian |
| `0x36` | `READ_IMAGE_RANGE` | 8 | Image start, then image length, both little-endian |
| `0x37`–`0x3E` | `READ_SERIAL` chunks 0–7 | 4 each | Chunk `n` is bytes `[4n, 4n+4)` of `0x31` |
| `0x3F` | — | — | Reserved; refused like any unassigned register |
| `0x40`–`0x47` | `READ_FIRMWARE_VERSION` chunks 0–7 | 4 each | Chunk `n` is bytes `[4n, 4n+4)` of `0x32` |
| `0x48`–`0x49` | `READ_IMAGE_RANGE` chunks 0–1 | 4 each | Chunk 0 is the start, chunk 1 the length |

These registers are readable over I2C, UART, and usbserial, whether or
not the board is provisioned — they are both the unprovisioned allow-list and
the steady-state identity source. Field order and encoding mirror the USB
`INFO` payload deliberately, so this document stays the single definition of
what a Roadrunner's identity is, with `INFO` and this window as two
encodings of it.

**A UART host reads no register wider than four bytes.** Klipper's MCU-side
`tmcuart` buffer is ten bytes (`uint8_t data[10]` in `klipper/src/tmcuart.c`),
and a request for more is `shutdown("tmcuart data too large")` — an MCU
shutdown, not a failed read. The bit-banged read asks for
`(((4 + reg_length) * 10) + 7) // 8` bytes, which reaches exactly ten at a
four-byte register, so four bytes is a hard ceiling on that transport.
`READ_IDENTITY_STATE` (1), `READ_VARIANT` (2) and `READ_IMAGE_DIGEST` (4)
fit. `READ_SERIAL`, `READ_FIRMWARE_VERSION` and `READ_IMAGE_RANGE` do not, and
a UART host reads them through their chunk registers (`0x37`–`0x49`) instead,
never through the wide register. `READ_FLASH_UID` (8) has no chunks and is
not readable over UART at all. I2C and usbserial carry the whole window and
can read either form.

Chunks are stateless: each is a plain slice of its wide register, so any one
can be read or retried on its own and there is no cursor for two bus masters
to fight over. A host may stop reading `READ_SERIAL` or
`READ_FIRMWARE_VERSION` chunks after the one holding the first NUL, since the
rest is padding. It must always read both `READ_IMAGE_RANGE` chunks: the range
is binary, and a zero byte there is a value. See
`roadrunner-uart-chunked-identity-design.md`.

**The identity register window is read-only.** The only writable registers
on I2C or UART are the provisioning staging registers `0x50`–`0x54`, and only
while the board is locked; see
`roadrunner-sensor-bus-provisioning-design.md`. A board with no USB cable
attached can be provisioned through them, but every admin opcode, including
`PROVISION_UUID` itself, is reachable only over USB.

`READ_FIRMWARE_VERSION` (`0x32`) is 32 bytes **including** the NUL
terminator, so it holds at most 31 characters of version string — one fewer
than the unterminated 32-byte version field `INFO` can carry. A 32-character
version therefore survives `INFO` intact but truncates to 31 characters in
the register. This is a known, accepted divergence between the two
encodings, not a bug.

`READ_IMAGE_DIGEST` (`0x35`) is fixed to CRC-32/ISO-HDLC by this register's
definition rather than carrying an algorithm byte, which is what lets it fit
the four-byte UART ceiling. INFO's digest field is self-describing because
INFO is a versioned payload where a future algorithm change has to be
expressible; a different register algorithm would take a different register
number instead. Together with the range chunks at `0x48`–`0x49`, that gives a
UART host everything it needs to reconstruct the digested range from a UF2 and
check the board against the file.

A board that has no digest to report — algorithm `NONE` in `INFO` — declines
`READ_IMAGE_DIGEST` outright rather than answering, because `0x00000000` is
itself a legal CRC and this register has no room to say "none". The refusal
reads as a zero-length answer, not as a value: hosts must treat a short or
empty read of `0x35` as "no digest", never as a digest of zero.
`READ_IMAGE_RANGE` (`0x36`) and its chunks still answer, since which bytes a
digest would cover is useful even when there is no digest.

Hosts must not persist the flash UID (`0x34`) as an identity; it is a
diagnostic value only and is not guaranteed unique across boards from the
same batch.

### The gate on every other register

While the board has no valid identity — `READ_IDENTITY_STATE` is anything
other than `1` (`OK`), which includes `CONFLICT` and `IO_ERROR`, not only
`NONE` — every register outside the identity window (`0x30`–`0x49`) returns
its normal length filled with `0xFF`. A host must read `READ_IDENTITY_STATE`
(`0x30`) to distinguish a locked board from a genuine sensor fault — `0xFF`
is out of range for almost every field, so an unpatched host reads visibly
broken rather than plausibly wrong.

**One exception:** `state.filament_present` is a `bool`, so `0xFF` at
`READ_FILAMENT_PRESENCE` (`0x22`) is *truthy*. A host reading that register
in isolation on a locked board sees "filament present" — plausibly wrong,
not visibly broken, which is the opposite of what the refusal encoding is
meant to achieve. This is accepted rather than fixed with a per-register
sentinel table, because that would be more code, more to keep in step, and
still not authoritative. Two mitigations exist: `READ_ALL` carries
`magnet_state = 0xFF` in the same payload, which *is* out of range for every
defined `MAGNET_STATE_*`, and `READ_IDENTITY_STATE` (`0x30`) is the
authoritative answer any host is expected to consult. No host should
conclude a locked board is reporting real filament from `0x22` alone.

The board never returns a zero-length response, provisioned or not. Silence
is indistinguishable from a wiring fault on I2C or a dead board on
TMC-UART, so a blocked read always answers at its normal length. Separately,
over I2C a read that runs past a register's payload returns `0x00` filler —
that is the transport's end-of-payload behaviour, distinct from the `0xFF`
refusal encoding, and the two never occupy the same byte position.

## Identity and updater rules

A provisioned serial is `RR-` followed by 26 uppercase Crockford-base32
characters encoding the 16-byte UUID. An unprovisioned device reports exactly
`RR-UNPROVISIONED`, with no suffix: boards from one batch share a flash UID,
so a UID suffix only disguised their collision. The flash UID stays readable
in its own INFO field and at `0x34`. Firmware before this change appended
`-` and the flash UID in uppercase hexadecimal. The UUID is generated by the host with a cryptographic
random source and stored in the reserved final flash sector. The flash UID is
never used as the persistent identity.

The updater must validate the descriptor and INFO model `roadrunner-v1`, keep
the USB path only as a transient physical-path handoff, and refuse ambiguous
matches or mismatched re-enumeration. It must not persist the flash UID or
physical path, automatically adopt a device, or automatically provision one.
After a confirmed manual provision or clear, the device remains untracked.
Normal UF2 updates preserve the identity sector. ROM BOOTSEL is the manual
recovery path if an update or maintenance operation is interrupted.

## Legacy USB compatibility

The administration parser is present on every USB-serial image, including
images whose sensor transport is I2C or UART. Bytes that do not form an admin
frame continue to the legacy sensor stream. A legacy register read begins with
`F5 10` and remains unchanged.

## Bench matrix

The following is the required recoverable-bench validation for provisioning:

1. Record INFO and the unprovisioned descriptor.
2. Send a known 16-byte UUID; receive the complete successful ACK before disconnect.
3. Confirm the re-enumerated descriptor and INFO contain the expected `RR-` serial.
4. Flash a normal UF2 and confirm the serial is retained.
5. Send a second provision request and confirm `ALREADY_PROVISIONED`.
6. Clear with `RRCL`; confirm the unprovisioned descriptor and INFO return.
7. Send a bad clear confirmation and confirm `CONFIRMATION_REQUIRED` without reboot.
8. Verify manual BOOTSEL recovery and repeat a legacy `F5 10` read on USB serial.

Bench validation completed on a recoverable RP2040-Zero on 2026-09-02. All
six RGB/GRB transport images reported the correct INFO transport and LED
order, retained the current serial across the next ordinary UF2 update, and
successfully cleared and reprovisioned a new serial. Both USB-serial images
also returned the legacy `F5 10` response. A second `PROVISION_UUID` was
rejected with `ALREADY_PROVISIONED` on all six images; a bad clear confirmation
was rejected with `CONFIRMATION_REQUIRED` without rebooting.
