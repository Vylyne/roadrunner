# Roadrunner chunked identity window for UART — design

Status: accepted, 2026-09-12. The design is settled; nothing implements it
yet. No firmware, host code, or normative protocol text has been changed for
it — see "Still required" at the end.
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

So four bytes is a hard per-register ceiling on this transport. SERIAL (34),
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
| `0x37`–`0x3F` | SERIAL | 0–8 | 34 | last chunk carries 2 bytes + 2 zero |
| `0x40`–`0x47` | FIRMWARE_VERSION | 0–7 | 32 | exact fit |
| `0x48`–`0x49` | IMAGE_RANGE | 0–1 | 8 | chunk 0 = `start`, chunk 1 = `length` |

Every chunk register returns exactly 4 bytes. A chunk index past a field's
last chunk is not assigned and is refused like any unassigned register.

**No FLASH_UID chunks.** Hosts must not persist the flash UID
(`roadrunner-usb-admin-protocol.md`), and widening its reach to the one
transport that most wants a stable identity invites exactly the misuse the
rule exists to prevent.

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
full tuples.

**Retries stay per chunk**, never per field. One flaky slice must not restart
the other eight.

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
| SERIAL, provisioned (29 chars) | 9 | 8 |
| SERIAL, unprovisioned (33 chars) | 9 | 9 |
| FIRMWARE_VERSION, `dev` | 8 | 1 |
| FIRMWARE_VERSION, `v0.3.1-12-gab34cd7` | 8 | 5 |
| IMAGE_RANGE | 2 | 2 |

**IMAGE_RANGE is never early-stopped.** It is binary, and `0x00000000` is a
legal start address — a zero byte there is a value, not a terminator. This is
the same trap the protocol doc already names for the digest, where a zero CRC
is a real CRC and "no digest" had to be signalled some other way. Both chunks
are always read in full.

**Pacing.** A full identity read is 20 chunk transactions worst case, 11-15
typical with early stop, each with up to 5 retries. They must not all land in one sensor-poll callback. The reader
carries a per-attempt budget measured in chunks, keeps successfully-read
chunks in an accumulator between attempts, and completes the field across
several polls. `_update_identity` already has a retry timer and caches, so
this is a bounded amount of new state.

**The accumulator clears on `_sensor_connected_changed`**, alongside
`_identity` and `_firmware_image`.

## Tearing

Can an identity change between chunk 0 and chunk 8?

No, on current firmware. Both commands that can change it — `PROVISION_UUID`
and `CLEAR_IDENTITY` in `rp2040/usb_admin.c` — acknowledge and then reboot the
application (`rr_usb_admin_acknowledge_before_application_reboot`). The board
goes away, the in-flight chunk reads fail, and the reconnect path discards the
accumulator. A torn assembly is not reachable.

This is load-bearing: it is what makes keeping partial chunks across polls
safe, which is what lets a 9-chunk serial converge on a marginal bus. **If a
future change ever lets an identity change without a reboot, this decision
must be revisited** — the fix then is a one-byte generation counter in the
window, read before and after the sequence, not a re-read of chunk 0 (chunk 0
of a serial is `RR-U`/`RR-0` and does not discriminate).

## Serial length — is a shorter serial worth it?

Asked directly: dropping the `RR-` prefix saves one chunk out of nine. The
arithmetic, since the chunk count is driven by the *widest* form the register
must hold, not by the typical one:

| Form | Today | Without `RR-` |
|---|---|---|
| Provisioned (`RR-` + 26 Crockford base32) | 29 | 26 |
| Unprovisioned (`RR-UNPROVISIONED-` + 16 hex) | 33 | 30 |
| Register width | 34 | 32 |
| Chunks | 9 | 8 |

So it is real but small: one transaction saved out of twenty, and the width
only drops to 32 because the unprovisioned form — not the identity itself —
sets the ceiling. The cost is not small: `RR-` is the namespace marker that
makes a Roadrunner recognizable in `/dev/serial/by-id`, in host matching, and
in three documents; changing it changes the USB serial descriptor of every
board already in the field.

If the chunk count is genuinely worth optimizing, the better lever is the
`UNPROVISIONED-` literal, which is what actually sets the width and carries no
identity at all. `RR-UNPROV-` + 16 hex is 26, making the provisioned form the
widest at 29 — still 8 chunks, but without touching the namespace prefix.
Getting to 7 chunks (28 bytes) needs both changes.

**Recommendation: neither.** Keep `RR-` and keep the register at 34. Nine
chunks versus eight does not change the pacing design, and the prefix is doing
identification work that a saved transaction does not pay for.

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

Four passages currently explain why these fields cannot exist over UART. All
four become wrong the moment this is implemented, and must be updated in the
same change:

- `klippy/extras/high_resolution_filament_sensor.py`, the `RegisterReaderUART`
  class comment — "SERIAL (34) and FIRMWARE_VERSION (32) ... are reported as
  None rather than attempted".
- `docs/roadrunner-usb-admin-protocol.md`, the ceiling paragraph listing which
  registers fit over UART.
- `docs/roadrunner-usb-admin-protocol.md`, "a UART host therefore ... cannot
  reconstruct the range from a UF2" — retracted by the `0x48`/`0x49` chunks.
- `README.md`, "Over UART the digest is present but `start` and `length` are
  `null`".

The normative register table in `roadrunner-usb-admin-protocol.md` is
deliberately left alone until firmware exists; the table above is written to be
lifted into it at that point.

## Still required

- Firmware: chunk dispatch in `rr_identity_registers_read()`, driven from the
  existing wide-register buffers so there is one source of each field.
- Host: the `read_reg` override, the accumulator, and the per-attempt budget.
- Bench: a UART-wired board reporting a serial that matches what the same board
  reports over USB, byte for byte.
