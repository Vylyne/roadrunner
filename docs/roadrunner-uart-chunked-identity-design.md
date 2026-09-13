# Roadrunner chunked identity window for UART — design

Status: accepted, 2026-09-12; implemented 2026-09-13 (Stage 2 of
`roadrunner-identity-implementation-plan.md`). The chunk registers are now in
the normative table in `roadrunner-usb-admin-protocol.md`. The Problem section
below describes the state before that change.
Supersedes nothing; extends `roadrunner-usb-admin-protocol.md`, which remains
authoritative for the identity namespace and the register window, and
`roadrunner-identity-gate-design.md`, which remains authoritative for what a
locked board will and will not answer.

## Problem

A UART-wired board cannot report its serial or its firmware version, and
cannot say which bytes its image digest covers. The cause is not the
Roadrunner protocol — it is Klipper's MCU-side buffer.

1. `klipper/src/tmcuart.c` declares a fixed `uint8_t data[10]`. The response
   buffer is ten bytes and there is no path to a larger one.
2. `SensorUART.reg_read` asks the MCU for `(((4 + reg_length) * 10) + 7) // 8`
   bytes — the 10-bit framing of a 4-byte header plus the payload. That
   reaches exactly 10 at `reg_length == 4`.
3. An over-long read is not a failed read. It is
   `shutdown("tmcuart data too large")`, which takes the printer down
   mid-print. The host cannot try a long read and recover.

So four bytes is a hard per-register ceiling on this transport. SERIAL (32),
FIRMWARE_VERSION (32) and IMAGE_RANGE (8) are all out of reach in one
transaction, and the extra currently reports them as `None` rather than
attempting them. IMAGE_DIGEST (4) is exactly at the ceiling and is the one
identity field a UART board can answer today.

The consequence a user sees: a UART board is anonymous from the printer
object. It cannot be matched to a provisioning record, its firmware version
cannot be checked before a flash, and although it reports a digest it cannot
say what that digest covers — so the digest cannot be checked against a
`.uf2` file without knowing the range out of band.

## Approach: stateless chunk registers

Offer each oversized field a second time, as a contiguous run of registers
each returning exactly four bytes. Chunk `n` of a field returns bytes
`[4n, 4n+4)` of that field's fixed-width buffer, zero-filled past the end of
the field exactly as the wide register already NUL-pads.

The alternative — a cursor register a host writes and then reads repeatedly —
is rejected:

- The cursor is shared state on a bus with more than one potential master,
  and across transports. An I2C read and a UART read would fight over it.
- A retry is no longer idempotent: a lost response leaves the host unsure
  whether the cursor advanced.
- It requires the UART transport to write, which the sensor read path does
  not currently do.

Stateless chunks keep every read independently retryable, keep the existing
`uart_read_reg` retry loop meaningful, and add no device state.

## Register allocation

The identity window becomes one contiguous range, `0x30`–`0x49`. Keeping it
contiguous matters because the gate rule is normative prose that hosts
implement — "every register outside the identity window returns `0xFF`" is a
sentence, and three disjoint ranges would make it three.

| Registers | Field | Chunks | Field size | Notes |
|---|---|---|---|---|
| `0x30`–`0x36` | existing window | — | — | unchanged |
| `0x37`–`0x3E` | SERIAL | 0–7 | 32 | exact fit; see the amendment below |
| `0x3F` | — | — | — | reserved, freed by the amendment |
| `0x40`–`0x47` | FIRMWARE_VERSION | 0–7 | 32 | exact fit |
| `0x48`–`0x49` | IMAGE_RANGE | 0–1 | 8 | chunk 0 = `start`, chunk 1 = `length` |

Every chunk register returns exactly 4 bytes. A chunk index past a field's
last chunk is not assigned and is refused like any unassigned register.

**No FLASH_UID chunks.** Hosts must not persist the flash UID, and a bare
eight-byte register reads like a field worth keeping.

This was nearly a distinction without a difference. Until the amendment below,
an unprovisioned board's serial *was* the flash UID — `RR-UNPROVISIONED-`
followed by all eight bytes in hex — so chunking SERIAL would have carried it
in full regardless, as `0x30` already does over I2C and as the USB descriptor
already does. Removing the suffix is what makes this carve-out mean something:
with it gone, the UID is reachable only at `0x34` and over USB admin INFO, and
travels no sensor bus at all.

**Where the window cannot grow.** Klipper's `tmc_uart` sets bit 7 of the
register byte to mean write, so the readable register space ends at `0x7F`,
not `0xFF`. The window has room to `0x7F` and no further.

## Locked boards

Chunk registers are inside the identity window and follow the window's rule
exactly: they answer whether or not the board is provisioned, and whether or
not it is locked. That is the same reasoning that put the wide registers
inside the window — "which build is this, and which board is this" is the
question you most need answered about a board that is refusing everything
else.

The gate's normative sentence needs its range widened from `0x30`–`0x36` to
`0x30`–`0x49`. Nothing else about the gate changes.

`IMAGE_DIGEST` refusal semantics carry over unchanged: a board with no digest
refuses `0x35`, and correspondingly answers `0x48`/`0x49` (the range is
meaningful even when there is no digest).

## Host side

`RegisterReaderUART` grows a `read_reg` override and a mapping:

```
CHUNK_BASES = {IdentityRegister.SERIAL: 0x37,
               IdentityRegister.FIRMWARE_VERSION: 0x40,
               IdentityRegister.IMAGE_RANGE: 0x48}
```

When `read_reg` is asked for a register longer than four bytes and that
register has a chunk base, it reads `ceil(length / 4)` chunks and
concatenates, truncating to the field length. When the register is long and
has no chunk base, the existing four-byte guard still fires and returns
`None`. This keeps chunking entirely inside the transport: `read_identity`
and `read_firmware_image` in `RegisterReaderGeneric` are untouched, and
`RegisterReaderUART.identity_registers` / `image_registers` go back to the
full tuples. (As built, `RegisterReaderUART` wraps both reads only to set the
per-attempt budget described under Pacing.)

**Retries stay per chunk**, never per field. One flaky slice must not restart
the other seven.

**Stop at the first NUL — text fields only.** SERIAL and FIRMWARE_VERSION are
NUL-padded strings, so a chunk containing a terminator is the last chunk worth
asking for. The reader stops there. This is purely a host read strategy:
firmware still serves every chunk as a plain slice, and a host that reads all
of them gets the same bytes.

It matters most for FIRMWARE_VERSION, which is the worst ratio in the
allocation. `ROADRUNNER_FIRMWARE_VERSION` is a free-form build-time string
(`rp2040/CMakeLists.txt`, default `dev`) and 32 is a ceiling, not a payload —
unlike SERIAL, where every byte of the encoded UUID is significant.

| Field | Chunks allocated | Typical chunks read |
|---|---|---|
| SERIAL, provisioned (29 chars) | 8 | 8 |
| SERIAL, unprovisioned (16 chars) | 8 | 5 |
| FIRMWARE_VERSION, `dev` | 8 | 1 |
| FIRMWARE_VERSION, `v0.3.1-12-gab34cd7` | 8 | 5 |
| IMAGE_RANGE | 2 | 2 |

**IMAGE_RANGE is never early-stopped.** It is binary, and `0x00000000` is a
legal start address — a zero byte there is a value, not a terminator. This is
the same trap the protocol doc already names for the digest, where a zero CRC
is a real CRC and "no digest" had to be signalled some other way. Both chunks
are always read in full.

**Pacing.** A full identity read is 18 chunk transactions worst case, and 8-15
typical with early stop — 8 for the unprovisioned board that the provisioning
flow most needs to read quickly, each with up to 5 retries. They must not all
land in one sensor-poll callback. The reader carries a per-attempt budget
measured in chunks, keeps successfully-read chunks in an accumulator between
attempts, and completes the field across several polls. `_update_identity`
already has a retry timer and caches, so this is a bounded amount of new state.

As built: the budget is six chunks per attempt, and the identity and image
reads each get their own. A read that spends its budget returns a pending
marker, and `_update_identity` tries again on the next poll instead of waiting
out the retry timer. A field whose chunk fails after its retries is recorded as
failed for the rest of that read, so later attempts report it `None` instead of
spending their budget on it again. The accumulator empties when a read
completes.

**The accumulator clears on `_sensor_connected_changed`**, alongside
`_identity` and `_firmware_image`.

## Tearing

Can an identity change between the first chunk and the last?

No, on current firmware. Both commands that can change it — `PROVISION_UUID`
and `CLEAR_IDENTITY` in `rp2040/usb_admin.c` — acknowledge and then reboot the
application (`rr_usb_admin_acknowledge_before_application_reboot`). The board
goes away, the in-flight chunk reads fail, and the reconnect path discards the
accumulator. A torn assembly is not reachable.

This is load-bearing: it is what makes keeping partial chunks across polls
safe, which is what lets a multi-chunk serial converge on a marginal bus. **If a
future change ever lets an identity change without a reboot, this decision
must be revisited** — the fix then is a one-byte generation counter in the
window, read before and after the sequence, not a re-read of chunk 0 (chunk 0
of a serial is `RR-U`/`RR-0` and does not discriminate).

## What this unblocks: provisioning from Klipper

The planned next step is for the extra to provision a board that turns up
unprovisioned. That flow splits across two transports, and this design is the
missing half of the first one:

- **Detect** over the sensor bus. The extra learns a board is unprovisioned by
  reading its serial and seeing `RR-UNPROVISIONED`. Over I2C that
  works today. Over UART it does not, because SERIAL is unreadable — so a
  UART-wired board cannot currently be *detected* as needing provisioning at
  all. These chunks are what make that detection possible.
- **Act** over USB CDC. `PROVISION_UUID` is a USB admin opcode, and
  `roadrunner-provisioning-design.md` is explicit that all maintenance traffic
  stays on the direct USB port. Nothing about this design changes that.

Three consequences fall out, and belong in that work rather than here:

1. **Detection and action can be on different wires.** A UART-wired board's USB
   port may not be connected to the host at all. The extra can then detect the
   condition and not be able to fix it; the right outcome is a message naming
   the board and asking for the USB connection, not a silent failure.
2. **The flash UID is the handoff key.** Matching the board found on the sensor
   bus to the right CDC port is exactly the "transient, confirmed handoff" that
   `AGENTS.md` permits — and forbids persisting afterwards. Once the board is
   provisioned its serial changes and the UID must not be kept.
3. **`PROVISION_UUID` reboots the board.** It must never fire mid-print. Gate it
   to idle.

There is a gap here today that predates this design: the extra reports whatever
serial it reads straight into `get_status()`, with no `UNPROVISIONED` handling,
and Moonraker persists printer objects. A transient diagnostic handle is
therefore already being written somewhere durable. Worth closing as part of the
provisioning work.

## Amendment: drop the flash UID from the unprovisioned serial

Accepted 2026-09-12, alongside this design. A blank board renders its serial as
exactly `RR-UNPROVISIONED`, with no hex suffix, identically on I2C, UART and the
USB descriptor.

**The suffix buys no distinctness.** `roadrunner-identity-gate-design.md`
hazard 1 records it, confirmed on hardware: two Roadrunners from the same batch
return the same `pico_get_unique_board_id()`, so their `RR-UNPROVISIONED-<uid>`
serials already collide and so do their `/dev/serial/by-id` symlinks. udev
creates one. mcu-updater's `_entry_candidates()` lists that directory by name
and therefore sees one board, and its "refuse ambiguous matches" guard never
fires because no ambiguity is visible.

That makes the suffix worse than useless. It simulates uniqueness it does not
have, turning a collision that would be obvious into one that is hidden.

Nothing real is lost by removing it:

- **Distinctness** was already absent.
- **Addressing** never used it. `roadrunner-provisioning-design.md` states the
  UID is never used to select a write, `AGENTS.md` makes USB topology the
  transient handoff key, and the gate's *unprovisioned implies not live*
  invariant means there is no live board to mis-address.
- **Diagnostics** keep it. The UID is still readable at `0x34` and in USB admin
  INFO. It simply stops being baked into the displayed serial, and so stops
  travelling the sensor bus at all — which retires the exposure discussed under
  "No FLASH_UID chunks" rather than managing it.

`RR-UNPROVISIONED` is 16 characters, exactly four chunks with no padding. Its
NUL terminator is therefore the first byte of chunk 4, so a host that stops at
the first NUL reads five chunks, not four:

| Chunk | Register | Bytes |
|---|---|---|
| 0 | `0x37` | `RR-U` |
| 1 | `0x38` | `NPRO` |
| 2 | `0x39` | `VISI` |
| 3 | `0x3A` | `ONED` |
| 4 | `0x3B` | `\0\0\0\0` |

The widest serial becomes the provisioned form at 29 characters, so:

- `RR_USB_SERIAL_MAX_LENGTH` and `RR_REG_SERIAL_SIZE` drop from 34 to 32.
- SERIAL takes 8 chunks, `0x37`–`0x3E`. `0x3F` is freed; the window's other
  allocations are unchanged.
- An unprovisioned board costs 5 chunk reads with early stop, not 9.

**Rejected: a sentinel value** such as all-`0xFF` or all-zero in place of the
text. `0xFF` is already the gate's filler for a refused register, so a sentinel
serial would be ambiguous with a refusal, and it needs a decode rule on every
host. The spelled word is self-describing on every transport and needs none.

This is a wire-format change and reaches beyond this document. At implementation
time it also requires:

- `rr_usb_descriptor_strings_build` to stop appending the hex UID, and the
  `RR_USB_SERIAL_MAX_LENGTH` change in `rp2040/usb_descriptor_strings.h`.
- `roadrunner-provisioning-design.md`, "Identity presentation" — the sentence
  describing `RR-UNPROVISIONED-` followed by the flash UID in hex.
- `roadrunner-usb-admin-protocol.md` — the same description near the INFO
  serial text, and the `34` widths in its register table.
- `roadrunner-identity-gate-design.md` — hazard 1 keeps its history but gains a
  note that the suffix was removed, and the `0x31` row's `34`/`33` lengths.
- Hosts that match on the prefix keep working; hosts that parse the suffix do
  not, which is the intended breakage.

## Serial length — is a shorter serial worth it?

The question that led to the amendment above: is the `RR-` prefix worth its
three bytes? The arithmetic matters because the chunk count is driven by the
*widest* form the register must hold, not by the typical one.

| Form | Before | After the amendment | Also dropping `RR-` |
|---|---|---|---|
| Provisioned (`RR-` + 26 Crockford base32) | 29 | 29 | 26 |
| Unprovisioned | 33 | 16 | 13 |
| Register width | 34 | 32 | 28 |
| Chunks | 9 | 8 | 7 |

The amendment takes the first step and gets the larger share of the benefit,
because the suffix it removes was both the thing setting the width and the
thing exposing the flash UID.

**Dropping `RR-` as well is rejected.** It would save one further chunk out of
twenty. Against that, `RR-` is the namespace marker that makes a Roadrunner
recognizable in `/dev/serial/by-id`, in host matching, and in four documents,
and removing it changes the USB serial descriptor of every board in the field
for a 5% saving in transactions. The prefix is doing identification work that a
saved transaction does not pay for.

The `UNPROVISIONED` literal is likewise not shortened further. The provisioning
work above matches on it to detect a blank board, so it is load-bearing rather
than cosmetic, and it now sits comfortably inside the width the provisioned
serial already requires.

## Tests

- A chunked assembly is byte-identical to the I2C single-register read for the
  same serial — asserted against the values in this document, not against
  whatever the other implementation happens to produce.
- A failed chunk yields `None` for the whole field, never a truncated or
  zero-padded string.
- A short FIRMWARE_VERSION stops after one chunk, and the assembled value
  equals the wide-register read.
- IMAGE_RANGE with a `start` of `0x00000000` reads both chunks and reports a
  zero start, not a truncated field.
- `read_identity` completes across N polls under a per-attempt chunk budget.
- A reconnect drops partial chunks.
- The 4-byte guard still fires — and still returns `None` — for a long
  register with no chunk base.
- Firmware: chunk `n` of each field equals bytes `[4n, 4n+4)` of the wide
  register's buffer, including the zero fill in SERIAL's last chunk.
- Firmware: a locked board answers every chunk register in the window.

## Prose this retracts

Retracted in the implementing change. Four passages explained why these fields
could not exist over UART:

- `klippy/extras/high_resolution_filament_sensor.py`, the `RegisterReaderUART`
  class comment — "SERIAL (34) and FIRMWARE_VERSION (32) ... are reported as
  None rather than attempted".
- `docs/roadrunner-usb-admin-protocol.md`, the ceiling paragraph listing which
  registers fit over UART.
- `docs/roadrunner-usb-admin-protocol.md`, "a UART host therefore ... cannot
  reconstruct the range from a UF2" — retracted by the `0x48`/`0x49` chunks.
- `README.md`, "Over UART the digest is present but `start` and `length` are
  `null`".

The table above has been lifted into the normative register table in
`roadrunner-usb-admin-protocol.md`.

## Still required

- Bench: a UART-wired board reporting a serial that matches what the same board
  reports over USB, byte for byte. Tracked in
  `roadrunner-identity-implementation-plan.md`, Stage 2.
