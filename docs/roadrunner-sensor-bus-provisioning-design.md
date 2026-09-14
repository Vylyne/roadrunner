# Provisioning over the sensor bus

Status: implemented, 2026-09-13, as Stage 4 of
[roadrunner-identity-implementation-plan.md](roadrunner-identity-implementation-plan.md),
with two deviations from the firmware rules below, marked where they occur;
the plan's Stage 4 "As built" has the rest. Proposed 2026-09-12. It depends on
[roadrunner-uart-chunked-identity-design.md](roadrunner-uart-chunked-identity-design.md)
for the read half — a host cannot decide whether to provision a board until it
can read `SERIAL` on the transport it is already using.

## Problem

`PROVISION_UUID` is reachable only over Roadrunner's direct USB CDC admin port.
A board wired for I2C or UART has no USB host attached in normal operation, so
setting its identity means unplugging it from the toolhead and carrying it to a
workstation. That is the whole cost of the current rule.

The USB path is also the one with a known defect. Gate hazard 1, confirmed on
hardware: two Roadrunners from the same batch return the same
`pico_get_unique_board_id()`, so their unprovisioned USB serials collide, udev
creates a single `/dev/serial/by-id` symlink, and mcu-updater's "refuse
ambiguous matches" guard never fires because it only ever sees one board.

The sensor bus does not have that problem and cannot acquire it. An I2C board
is addressed by bus and address; a UART board by pin. Both come from the
`[high_resolution_filament_sensor]` section that already exists in
`printer.cfg`. Provisioning over the sensor bus writes to exactly the board the
config points at, with no name resolution step in between. This is the stronger
argument for the design, and it runs the opposite direction from the objection
usually raised against it.

## The gate

`rr_identity_registers_locked()` returns `identity_status != RR_IDENTITY_OK`
([rp2040/identity_registers.c:124-127](../rp2040/identity_registers.c#L124-L127)).
Locked and unprovisioned are the same state — there is no provisioned-and-locked
board for a rule to fall through. So one sentence covers it:

> Admin commands over I2C, UART or the sensor serial framing are accepted only
> while the board is locked. A provisioned board refuses every write on every
> sensor transport.

Two consequences worth stating rather than leaving to be inferred:

- `CLEAR_IDENTITY` is unreachable over the sensor bus by construction. Any board
  you would want to clear is provisioned, therefore not locked, therefore
  refusing. It is not "not implemented yet" — the gate makes it unimplementable
  without changing the gate. Same for `REBOOT_BOOTSEL`.
- `PROVISION_UUID` is reachable exactly once per board. It reboots into
  `RR_IDENTITY_OK`, which closes the gate behind it.

This preserves the invariant the gate design is built on: a board that is
unprovisioned cannot be mid-print on any transport, so provisioning it can never
interrupt one.

The gate's existing rule governs reads — registers outside the window return
`0xFF`. Writes need their own sentence, because the read rule does not imply
one:

> Every register is read-only on every transport, except the provisioning
> staging registers below, which accept writes only while the board is locked.

## Write path: staged chunks plus a checked commit

A UUID is sixteen bytes and the UART datagram carries four, so this is
unavoidably a multi-transaction sequence with state between the transactions —
the cursor shape that the read design rejected. It is accepted here for a
reason that does not apply to reads: a write cannot be made idempotent per
chunk. Saying so plainly is better than letting a reader find the
inconsistency.

| Register | Meaning |
| --- | --- |
| `0x50`–`0x53` | UUID staging chunks 0–3, four bytes each |
| `0x54` | `COMMIT`: four bytes, CRC-8/ATM over the sixteen staged bytes in the low byte, three bytes zero |

Chunks are indexed by register, not by an implicit cursor — chunk 2 is `0x52`
whatever happened before it. Register space stays under the `0x7F` ceiling that
Klipper's `tmc_uart` imposes by using bit 7 of the register byte to mean write.

Firmware rules:

1. A write to any of these registers while unlocked is refused: NACK on I2C,
   silently dropped on UART (which has no reply channel to refuse on).
   **As built:** dropped silently on I2C too. The pico-sdk slave handler has
   already ACKed each byte by the time the write is complete at Stop, and the
   host's re-read of the identity is the acknowledgement either way.
2. Staged bytes live in RAM. They are zeroed at boot, and cleared after every
   commit attempt whether it succeeded or failed.
3. A four-bit present-mask records which chunks have arrived. `COMMIT` refuses
   unless all four are present.
4. `COMMIT` recomputes CRC-8/ATM over the sixteen staged bytes and applies the
   identity only on a match. Mismatch writes nothing and clears the stage.
5. Writing chunk 0 clears the stage first. Any sequence that starts at the
   beginning discards whatever a previous attempt left behind.
6. On success the board writes the identity record and reboots through
   `rr_usb_admin_acknowledge_before_application_reboot`, the same path the USB
   opcode takes.
   **As built:** it reboots directly through
   `rr_usb_admin_reboot_application(NULL)`. The acknowledge path waits for a
   USB CDC reply to drain, and a sensor-bus commit has no USB requester to
   drain to. The commit is applied from the main loop, never from the I2C or
   UART handler, because it writes flash.

### Why the commit is checked

The failure this guards against is a torn stage, and the present-mask alone does
not catch it. Host stages chunks 0–2, dies. A later attempt — a different host,
a Klipper restart, a retry with a freshly generated UUID — stages chunk 3 and
commits. The mask reads complete. The board accepts a UUID spliced out of two
different ones, and it validates as a perfectly well-formed identity forever
after.

The CRC catches it because the host computes it over the UUID it intends, not
over what happens to be in the buffer: a splice fails the check. Rule 5 is the
belt to that braces — in the common case the second attempt restarts at chunk 0
and the leftovers are gone before the CRC ever has to do its job.

### Why not one wide write

I2C could carry sixteen bytes in a single transaction. UART cannot: the tmcuart
datagram's payload is fixed at four bytes. One shape that works identically on
both transports is worth more than saving three transactions on one of them,
and it keeps a single firmware path to test.

## Host flow at Klipper start

1. The extra reads `SERIAL` in chunks (per the read design). A serial beginning
   `RR-UNPROVISIONED` means the board is locked.
2. If `auto_provision` is enabled — default on, `auto_provision: False` opts out
   — the host generates a UUID v4, writes chunks `0x50`–`0x53`, then `COMMIT`.
3. The board reboots. Wait for it to answer, re-read `SERIAL`, and confirm it is
   the Crockford encoding of the UUID that was sent.
4. Report the outcome.

Step 3 is the only acknowledgement the sequence gets. UART writes are
fire-and-forget, so the host cannot learn that a staging write was refused or a
CRC rejected — it learns only that the serial did or did not change. That is
sufficient: the check is end-to-end and does not depend on any transport having
a reply channel.

### The commit reboots the board, and that is already handled

Nothing in step 3 needs to be built. The extra reads identity lazily from
`_update_identity` on the sensor poll timer, which only starts at
`klippy:ready` — not during `klippy:connect`. So the board drops off the bus
while Klipper is live and idle, and the existing reconnect path absorbs it:
`_sensor_connected` flips false, then true, and
[`_sensor_connected_changed`](../klippy/extras/high_resolution_filament_sensor.py#L1128)
clears `_identity`, `_firmware_image` and `_identity_next_attempt`. Its comment
already names this exact case — "the board that came back is not necessarily
the board that went away — it may have been swapped, reflashed or provisioned
while it was gone."

Two rules follow:

- **Do not block waiting for the board.** Write the chunks, write `COMMIT`,
  return. The poll timer notices the disconnect, then the reconnect, then
  re-reads `SERIAL` because the cache was dropped. That re-read *is* the
  acknowledgement from step 3 — there is no separate wait to implement and no
  `reactor.pause` inside a handler.
- **Provision only on the first identity read after `klippy:ready`, never on a
  later one.** Position is derived from the board's absolute turn and angle
  counters
  ([line 1232-1237](../klippy/extras/high_resolution_filament_sensor.py#L1232-L1237)):
  `distance = new_position - self.position`. A reboot zeros those counters, so
  `new_position` snaps to zero while `self.position` keeps its old value,
  producing one spurious reverse `distance` the size of everything measured so
  far. On the first read `self.position` is still `0.0` and the discontinuity is
  zero-sized. Any later reboot would inject a false reverse move, which the
  runout logic would be right to believe.

That second rule is a constraint on this design, not a bug in the extra —
though it does mean an unrelated mid-print reconnect already carries the same
discontinuity, since `_sensor_connected_changed` resets the identity cache but
not `self.position`. Worth a separate look; out of scope here.

The `serial:` transport cannot recover from the reboot at all. `_handle_connect`
opens the port once and is not re-run, and the `/dev/serial/by-id` path the
config named stops existing the moment the identity changes. That transport
raises rather than reconnecting — see below.

### What "report" means depends on the transport

This is where the cost is smaller than the worst case suggests:

- **I2C and UART.** The config section names an address or a pin. It never names
  the serial. Provisioning changes nothing the config refers to, so there is no
  config edit and no reason to stop: log the new serial at INFO and continue
  into the print. These are precisely the boards this feature exists for.
- **`serial:` (USB CDC).** The config names
  `/dev/serial/by-id/…RR-UNPROVISIONED…`, which stops existing the moment the
  board reboots under its new identity. Here the extra must raise a config error
  naming the new serial and saying which line to change. That board needed a
  restart either way.

So "worst case one stop per unprovisioned device" is the USB-variant case only,
and USB boards already have the direct admin path.

Failures raise rather than continuing quietly: the board never comes back, or it
comes back with a serial that is not the one sent, or it comes back still
unprovisioned (a refused commit). A board that silently kept its old identity is
worse than a startup error, because the next thing that happens is a print
against a board nobody can name.

## Holding `sensor_connected` false until the identity arrives

The transports diverge on when provisioning can happen, because they diverge on
when the board is reachable:

- **`serial:`** is opened in `_handle_connect` at `klippy:connect`, so it can
  provision and raise there. The raise is the whole flow — the port is about to
  stop existing anyway.
- **I2C and UART** are not reachable until the parent MCU is up, so the earliest
  point is the first poll after `klippy:ready`. Nothing is printing yet, and the
  board reboots under us, so the following poll talks to a freshly provisioned
  device.

The extra should hold `_sensor_connected` false from the moment it decides to
provision until it has read back an identity that says provisioned. Identity
acquisition does not depend on that flag — `_update_identity` runs before the
`if not self._sensor_connected: return` early-out in `_update_state_from_sensor`
— so the poll loop keeps retrying the read on its own. `_check_print_issues`
returns early when not printing, so a false `sensor_connected` in this window
costs nothing.

### This also fixes a wrong state that exists today

An unprovisioned board is not silent. `prepare_register_data`
([rp2040/main.c:279-280](../rp2040/main.c#L279-L280)) fills the reply with
`0xff` when locked, deliberately, so that a refusing board does not read as a
wiring fault. But `SensorRegister.connected`
([high_resolution_filament_sensor.py:238-242](../klippy/extras/high_resolution_filament_sensor.py#L238-L242))
is only `not (... is None ...)` — it asks whether four reads returned bytes, not
whether those bytes mean anything. The fill decodes to `magnet_state=255`,
`filament_presence=255`, `full_turns=-1`, `angle=-1`, none of which are `None`.

So the extra currently calls an unprovisioned board **connected**, and feeds
`full_turns = -1` into the rotation helper, which puts `self.position` at a large
negative value. When that board is then provisioned and reboots, the counters
zero and `distance` comes out large and **positive** — which passes the
`if distance > 0.` gate and toggles the virtual motion callbacks. The refusal
fill turns the benign case into a false-motion case.

Forcing `sensor_connected` false while the identity says unprovisioned fixes
this at the same time, and is worth doing whether or not auto-provisioning is
enabled.

## A boot marker, for reboot detection in general

Distinguishing "the board reset" from "the bus went quiet" is not a
provisioning-specific need. A brownout or a loose power wire produces the same
counter discontinuity, and the host has no way to tell the two apart today —
which is why `_sensor_connected_changed` clears the identity cache but cannot
safely rebase `self.position`.

A register carrying milliseconds since boot solves it: on every poll, a value
lower than the previous one means the board restarted. Four bytes, inside the
UART ceiling.

Prefer this over a sticky "first read since boot" flag that clears on read. The
UART reader retries up to five times
([`uart_read_reg`](../klippy/extras/high_resolution_filament_sensor.py#L443-L452)),
so a clear-on-read flag can be consumed by an attempt whose reply was lost and
then never observed — the one case where it mattered is the case it misses. A
monotonic value is idempotent: every read gives the same answer, retries are
free, and two bus masters do not race.

Two details worth fixing in the definition rather than discovering later:

- **Saturate, do not wrap.** A `uint32_t` of milliseconds wraps at ~49.7 days
  (the LED burst code already carries a comment about this), and a wrap looks
  exactly like a reboot. Clamping at the top means "lower than last time" stays
  an unambiguous reset for the life of the board.
- **Leave `0xffffffff` alone.** It is the locked-board refusal fill, so cap the
  saturation at `0xfffffffe` and the sentinel stays distinguishable from a real
  reading.

What the host does with it is a separate piece of work: rebase `self.position`
to the new reading without emitting a `distance`, and count the reset in the
motion statistics as its own category rather than as a read failure. Noted here
because the provisioning reboot is the first consumer, not the only one.

## The `serial:` transport needs no write form

The `0xf5 <reg>` framing is a two-byte state machine
([rp2040/usbserial.c:61-69](../rp2040/usbserial.c#L61-L69)): `0xf5`, then any
byte is a register number to answer. There is no write form and none needs
adding, because the board has exactly one CDC interface (`CFG_TUD_CDC 1`) and
the USB admin protocol is already on it.

`rr_usb_admin_poll` drains the port and `rr_usb_admin_receive`
([rp2040/usb_admin.c:347-359](../rp2040/usb_admin.c#L347-L359)) is the
demultiplexer: `0x52 0x52` opens an admin frame, and every other byte is handed
to `usbserial_receive_byte` as a legacy byte. The two protocols already share
the port, with the admin parser as the front door.

So the `serial:` transport provisions by writing
`0x52 0x52 0x01 PROVISION_UUID …` down the same `serial.Serial` handle it
already uses for reads. No new framing, no staging registers, no commit
register — the existing USB admin protocol carries the length and CRC itself.
The staging registers at `0x50`–`0x54` exist for I2C and UART only.

### Constraint this creates: no register may be numbered `0x52`

Over `serial:`, a read of register `0x52` sends `0xf5 0x52`. The admin parser
sees `0xf5`, forwards it (usbserial sets `waiting_for_register`), then sees
`0x52`, recognises it as `RR_USB_ADMIN_SYNC` and opens an admin frame instead of
forwarding it. The register is never read and both state machines are left
mid-sequence.

No current or planned register hits this — the sensor registers are
`0x20`–`0x25`, identity is `0x30`–`0x49`, staging is `0x50`–`0x54` and is not
used on this transport. It is written down because the next person to allocate a
register number has no other way to know.

## Register numbers

- Staging chunks `0x50`–`0x53`, commit `0x54`. I2C and UART only.
- Boot marker `0x25`, four bytes: milliseconds since boot, saturating at
  `0xfffffffe` so the value never wraps into looking like a reset and never
  collides with the `0xffffffff` locked-board refusal fill. Placed next to the
  sensor registers rather than in the identity window: it is read on every poll,
  and the identity window is for things read once.

## What this retracts

[roadrunner-provisioning-design.md](roadrunner-provisioning-design.md): "All
maintenance traffic remains available only on Roadrunner's direct USB CDC port."
That becomes: all maintenance traffic except `PROVISION_UUID`, which a locked
board also accepts over the sensor bus. Anyone reading the provisioning design
must be able to find this document from it, which is why this is a separate file
rather than an amendment to the read design.

[roadrunner-identity-gate-design.md](roadrunner-identity-gate-design.md) gains
the write rule quoted above, and hazard 1 gains a note that the sensor-bus path
is not exposed to it.

## Still to verify before implementing

- `MCU_TMC_uart_bitbang.reg_write` sends an eight-byte datagram (sync, addr,
  reg, four data, CRC), comfortably inside the ten-byte MCU buffer that caps
  reads — so there is no ceiling problem on the write side. Confirm against the
  installed Klipper rather than from memory.
- `MCU_I2C.i2c_write` exists and is used elsewhere in Klipper.
  `RegisterReaderGeneric` and its three subclasses are read-only today; each
  needs a write counterpart, and `RegisterReaderUART` needs one that does not
  pretend to have a return value.
- ~~Whether the sensor serial framing has a write form.~~ Resolved, see "The
  `serial:` transport needs no write form" below.
- UUID generation is host-side here, chosen so the host knows the serial to
  print in the error message before the board reboots. The alternative — firmware
  generates, host reads back — removes the staging buffer entirely but gives the
  host no way to distinguish "provisioned to X" from "was already X".

## Tests

- Staging writes are refused on a provisioned board, on both transports.
- `COMMIT` with one chunk missing writes nothing.
- `COMMIT` with a wrong CRC writes nothing and clears the stage.
- A spliced sequence — stage 0–2, restage 3 from a different UUID, commit —
  fails the CRC.
- Writing chunk 0 clears previously staged chunks.
- A full round trip: unprovisioned board, stage, commit, reboot, `SERIAL` reads
  back as the Crockford encoding of the UUID sent.
- The extra raises on the `serial:` transport after provisioning, and does not
  raise on I2C or UART.
- `auto_provision: False` leaves an unprovisioned board alone.
