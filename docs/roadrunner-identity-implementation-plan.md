# Implementation plan: identity over the sensor bus

Status: approved, 2026-09-12. Nothing here is implemented yet.

This plan executes two accepted designs:

- [roadrunner-uart-chunked-identity-design.md](roadrunner-uart-chunked-identity-design.md)
  — the read half.
- [roadrunner-sensor-bus-provisioning-design.md](roadrunner-sensor-bus-provisioning-design.md)
  — the write half.

Four stages, ordered so each is independently testable and each leaves the tree
shippable. Stage 3 is useful on its own even if stage 4 never lands.

## Global constraints

These bind every stage. They come from `AGENTS.md` and from the two designs.

- A register-map change must update the firmware, the matching Klippy transport
  code, and the README in the same change.
- Bench hardware is a spare RP2040-Zero with a known BOOTSEL recovery path.
  Never a toolhead or an installed sensor. State plainly which stages were
  exercised on hardware and which were not. The rig is specified in
  [roadrunner-bench-harness.md](roadrunner-bench-harness.md): a second
  RP2040-Zero running stock Klipper as bus master.
- Readable register space ends at `0x7F`: Klipper's `tmc_uart` sets bit 7 of the
  register byte to mean write.
- Four bytes is a hard per-register ceiling on UART. An over-long read is
  `shutdown("tmcuart data too large")`, not a failed read.
- No register may be numbered `0x52` — over `serial:` the admin parser claims it
  as its own sync byte.
- Two test suites must pass before hand-off:
  - `cmake --build rp2040/tests/build && ctest --test-dir rp2040/tests/build --output-on-failure`
  - `python -m pytest tests -q`
- Run `git diff --check` before hand-off.
- Work on short-lived `feat/...` branches off `develop`, one per stage.

## Stage 1 — the unprovisioned-serial amendment

Drops the flash UID from the unprovisioned serial. Smallest stage, no new
behaviour, and it settles the `SERIAL` length that stage 2 chunks.

Rationale is in the read design's amendment section: same-batch boards share a
`pico_get_unique_board_id()`, so the UID suffix simulated a uniqueness it never
had, and removing it takes `SERIAL` from 34 bytes to 32 — an exact eight-chunk
fit — and removes the UID from the sensor bus entirely.

**Firmware**

- `rp2040/usb_descriptor_strings.c` — `rr_usb_descriptor_strings_build` stops
  appending the hex UID. The serial becomes the bare `RR-UNPROVISIONED`,
  NUL-terminated.
- `rp2040/usb_descriptor_strings.h` — `RR_USB_SERIAL_MAX_LENGTH` 34 → 32. The
  build function loses its `flash_uid` parameter.
- `rp2040/usb_descriptors.c` — drops `pico_get_unique_board_id()`, which only
  fed the suffix.
- `rp2040/identity_registers.c`, `identity_registers.h` — the `0x31` caller and
  its width comment. `0x34` still reports the flash UID.
- `rp2040/usb_admin.c` — INFO builds the serial separately and needs the same
  change; its buffer shrinks to `RR_IDENTITY_SERIAL_LENGTH + 1`. INFO's own
  flash UID field is unchanged.

**Klippy and scripts**

- `klippy/extras/high_resolution_filament_sensor.py` — `IdentityRegister.SIZES`
  `SERIAL` 34 → 32, and the comments that quote it.
- `scripts/roadrunner_admin.py`, `scripts/oneoff_flash_and_test.py` — the
  `0x31` read width and `UNPROVISIONED_RE`. The scripts still accept the old
  suffixed form, because they read boards before reflashing them.
- `README.md` documents no `0x31` width and no unprovisioned serial, so it does
  not change.

**Docs**

- `roadrunner-provisioning-design.md` — the "Identity presentation" section.
- `roadrunner-usb-admin-protocol.md` — the INFO serial text and every `34`
  width. The INFO serial field drops to 1 + up to 29 and the maximum payload to
  103 bytes.
- `roadrunner-identity-gate-design.md` — the hazard 1 note and the `0x31` row
  lengths.
- `roadrunner-uart-chunked-identity-design.md` — the one current-tense mention
  of the prefix. The amendment's historical text stays.

**Verification**

- `rp2040/tests/test_identity_record.c` and `test_identity_registers.c` cover
  the serial construction; update expectations and add a case asserting the
  unprovisioned serial is exactly `RR-UNPROVISIONED` with no suffix. INFO needs
  its own case, since it builds the serial separately.
- `tests/test_high_resolution_filament_sensor.py` — the SERIAL width follows
  `IdentityRegister.SIZES`.
- Bench: flash an unprovisioned board and confirm `lsusb -v` and
  `/dev/serial/by-id` show the bare serial. *Passed 2026-09-13* on the bench
  DUT with `roadrunner_v1_uart_rgb` from this branch. The board reported image
  digest `0x3da2b6ee`, matching the UF2 over 30892 bytes. After `clear`,
  `iSerial`, `ID_SERIAL_SHORT`, the by-id link and INFO all read exactly
  `RR-UNPROVISIONED`, and Klipper reported `identity.state: none` with sensor
  data withheld. After `provision` the board came back as
  `RR-7HZHY879879X19ZQZTJYQ7DDRB` and Klipper read the magnet again.

## Stage 2 — chunked identity reads

Makes a UART board identifiable at all. This is the stage the README TODO names.

**Firmware** — `rp2040/identity_registers.c`

- `rr_identity_registers_read()` gains chunk dispatch over one contiguous
  window:

  | Registers | Field | Chunks | Bytes |
  | --- | --- | --- | --- |
  | `0x37`–`0x3E` | `SERIAL` | 0–7 | 32 |
  | `0x3F` | — | — | reserved |
  | `0x40`–`0x47` | `FIRMWARE_VERSION` | 0–7 | 32 |
  | `0x48`–`0x49` | `IMAGE_RANGE` | 0–1 | 8 |

- Chunks are stateless: chunk *n* is always register base + *n*, with no cursor
  and no shared state between bus masters. Every chunk read is independently
  retryable.
- The existing `0x30`–`0x36` window is unchanged. Locked boards keep the
  `0xff` refusal fill across the new registers via the existing
  `prepare_register_data` path — no new gate logic.

**Host** — `klippy/extras/high_resolution_filament_sensor.py`

- `RegisterReaderGeneric` gains `CHUNK_BASES` mapping `SERIAL` → `0x37`,
  `FIRMWARE_VERSION` → `0x40`, `IMAGE_RANGE` → `0x48`.
- A `read_reg` override reads `ceil(length / 4)` chunks and concatenates. A
  long register with no chunk base still hits the four-byte guard and returns
  `None` rather than shutting the MCU down.
- Early stop at the first NUL, for `SERIAL` and `FIRMWARE_VERSION` only.
  `IMAGE_RANGE` is binary and `0x00000000` is a legal start address — carve it
  out explicitly.
- Retries stay per chunk. The accumulator clears in
  `_sensor_connected_changed`.
- `RegisterReaderUART.identity_registers` extends to include `SERIAL` and
  `FIRMWARE_VERSION`; `image_registers` gains `IMAGE_RANGE`.
- Remove the four prose passages the read design lists under "Prose this
  retracts" — they assert UART cannot carry these fields.

**Verification**

- `rp2040/tests/test_identity_registers.c`: every chunk of every field, the
  reserved `0x3F`, and the boundary at `0x49`/`0x4A`.
- `tests/test_high_resolution_filament_sensor.py`: reassembly, early stop,
  `IMAGE_RANGE` not early-stopped, a mid-sequence chunk failure returning
  `None` rather than a truncated string.
- Bench, required: a UART board reporting a full `identity.serial`. Also
  measure the first-read cost against the poll interval — 18 transactions worst
  case, 8–15 typical.

**As built.** `CHUNK_BASES` lives on `RegisterReaderUART`, not the generic
reader, since no other transport needs it. Locked boards answer every chunk,
the same as the wide registers. `RegisterReaderUART` gives the identity read and
the image read a budget of six chunks each per attempt. A read that runs out
returns `READ_PENDING`, and `_update_identity` carries on at the next poll
rather than the retry timer. A chunk that still fails after its five retries
makes that field `None` for the rest of the read, so a board with one bad chunk
still converges. The not-provisioned message now fires once, when the identity
is first read, instead of on every attempt at the image registers. The
unprovisioned serial takes five chunks, not the four the read design first
said, because its NUL is chunk 4.

A UART identity read is 12 to 18 register transactions, where I2C needs 4, so
a marginal bus has more chances to lose one. Two outcomes are by design. A
chunk that exhausts its retries leaves that field `None` until the sensor
reconnects, because a cached identity is not read again. A STATE read that
fails partway through discards the chunks gathered so far, and the whole read
starts again after the 10 s retry timer.

*Bench, passed 2026-09-13*, on the full Roadrunner DUT running
`roadrunner_v1_uart_rgb` from `e9587a0`, over Klipper tmcuart on a Raspberry
Pi 3:

- Flashed with `roadrunner_admin.py flash`. The copy step did not run: the
  udev automount took more than the old 10 s mount wait, so the tool gave up
  with the board in BOOTSEL. The UF2 was then copied by hand. The copy, sync
  and apply took 12 s, and the board was back on USB as soon as the volume
  went. The mount wait is now 30 s. With that change (`aadee45`), a second
  `flash` of the same image ran the whole flow in about 11 s and exited
  cleanly: reboot to BOOTSEL, mount, copy, wait for the apply, re-enumerate,
  INFO.
- After a Klipper service restart, `identity.serial` was
  `RR-7HZHY879879X19ZQZTJYQ7DDRB` and `firmware_version` was `7d22724`. That
  string is stale: it is set when CMake configures the build directory, which
  had not been reconfigured since `7d22724`.
  `firmware_image` reported digest `0x5d54b009` with start `0x10000000` and
  length 30996. The ELF's `__flash_binary_end` is `0x10007914`, which is
  30996 bytes, and `uf2_image_digest.py` over the UF2 gives `0x5d54b009`.
- Klipper's `RESTART` does not reload the extra's Python; the first check
  after one still showed `null` fields. It takes a service restart.
- First-read cost, from a timing patch that was reverted afterwards: the
  identity finished on the second poll. The two attempts took 52 ms and 47 ms,
  on top of the 19 ms sensor read, so both polls stayed inside the 100 ms poll
  interval. `reads_failed` stayed 0 and no chunk failures were logged.

## Stage 3 — boot marker at `0x25`

Lets the host tell "the board reset" from "the bus went quiet". Standalone: it
fixes the brownout discontinuity whether or not stage 4 ships.

**Firmware**

- New register `0x25`, four bytes: milliseconds since boot, saturating at
  `0xfffffffe`. Saturating rather than wrapping, because a `uint32_t` of
  milliseconds wraps at ~49.7 days and a wrap is indistinguishable from a
  reset. Capped below `0xffffffff` so it never collides with the locked-board
  refusal fill.
- Served from `prepare_register_data` alongside the other sensor registers, so
  it inherits the existing gate behaviour.

**Host**

- Read `0x25` on each poll. A value lower than the previous one means the board
  restarted.
- On a detected reset: rebase `self.position` to the new reading **without**
  emitting a `distance`, and clear the rotation helper. This is the fix for the
  spurious jump — currently a reset produces one `distance` the size of
  everything measured so far.
- Count resets in the motion statistics as their own category, not as read
  failures.

**Verification**

- Host tests: reset detected on a decreasing value; saturation never decreases;
  the rebase emits no distance; a normal increasing sequence is untouched.
- Bench, required: power-cycle a board mid-session and confirm one reset is
  counted and no phantom distance appears.

**As built (`da5b6c1`)**

- Resets are counted at `connection.resets`, not in `motion`, which is derived
  entirely from the current move.
- The rotation helper holds no state between readings, so there is nothing to
  clear. The rebase is an offset: position is held until two consecutive polls
  see the magnet detected, then carries on from its pre-reset value. Moves in
  flight at the reset are dropped, because a measurement spanning it would read
  as underextrusion.
- The last marker survives disconnects, since a power cycle over UART is a
  disconnect followed by a reset. A swapped board that has been up longer than
  the old one is not caught.
- The marker is asked of a board only once it has answered the identity
  window; after ten consecutive failed reads the extra stops asking until the
  board reconnects. Once a board has answered, a poll whose marker read fails
  moves nothing.

**Bench, UART DUT, 2026-09-13**

- Earlier firmware without `0x25`: the extra gave up after ten reads and logged
  it once; position reporting carried on.
- `da5b6c1`: `reads_failed` 0 and no UART errors in steady state.
- Soft reset (reflash of the same image, no power loss): `resets` went 0 to 1,
  the log shows the reset at 1192 ms since boot and the rebase, and position
  was unchanged at -1.3314 before and after. The 102 read failures were the
  board being away while it rebooted.
- Power cycle (DUT unplugged and replugged mid-session): logged as a
  disconnect, then a reset at 1167 ms since boot and the rebase. `resets` went
  1 to 2 and position was still -1.3314, so no phantom distance was recorded.

## Stage 4 — provisioning

**Firmware** — I2C and UART only

- Staging registers `0x50`–`0x53`, four bytes each, indexed by register rather
  than by a cursor.
- Commit register `0x54`: CRC-8/ATM over the sixteen staged bytes in the low
  byte.
- Writes are refused unless `rr_identity_registers_locked()` — which is
  `identity_status != RR_IDENTITY_OK`, so locked and unprovisioned are the same
  state. NACK on I2C; silently dropped on UART, which has no reply channel.
- Staged bytes live in RAM, zeroed at boot and cleared after every commit
  attempt, success or failure.
- A four-bit present-mask; `COMMIT` refuses unless all four chunks are present.
- Writing chunk 0 clears the stage first.
- On success: write the identity record and reboot through
  `rr_usb_admin_acknowledge_before_application_reboot`.
- Every other register stays read-only on every transport.

The CRC is load-bearing, not belt-and-braces. Stage chunks 0–2, die, and a later
attempt stages chunk 3 from a different UUID and commits: the present-mask reads
complete and the board accepts a spliced identity that validates cleanly
forever. The host computes the CRC over the UUID it intends, so a splice fails.

**Host**

- `RegisterReaderGeneric` and its three subclasses gain a write counterpart.
  The UART one must not pretend to return anything — `reg_write` is
  fire-and-forget.
- `serial:` does **not** use staging registers. It writes
  `0x52 0x52 0x01 PROVISION_UUID …` down the handle it already holds; the admin
  protocol carries its own length and CRC.
- Force `_sensor_connected` false while the identity says unprovisioned. This
  also fixes a current wrong state: the `0xff` refusal fill decodes to
  `full_turns = -1`, which is not `None`, so `SensorRegister.connected` calls an
  unprovisioned board connected and poisons `self.position`.
- `auto_provision` config option, default on, `auto_provision: False` opts out.
- Provision only on the first identity read after `klippy:ready`. Later is
  unsafe: `self.position` is no longer `0.0` and the reboot injects a false
  move.
- Do not block waiting for the board. Write, commit, return; the poll timer
  sees the disconnect and reconnect, and `_sensor_connected_changed` already
  drops the identity cache. The re-read is the acknowledgement.
- Report by transport: I2C and UART log the new serial at INFO and continue —
  their config names a pin or an address, never the serial, so nothing needs
  editing. `serial:` raises a config error naming the new serial, because the
  `/dev/serial/by-id` path it was configured with has ceased to exist.
- Raise on failure: the board never returns, returns a serial that is not the
  one sent, or returns still unprovisioned.

**Docs**

- `roadrunner-provisioning-design.md`: retract "all maintenance traffic remains
  available only on Roadrunner's direct USB CDC port" — it becomes all
  maintenance traffic except `PROVISION_UUID` on a locked board.
- `roadrunner-identity-gate-design.md`: add the write rule — every register is
  read-only on every transport except the staging registers, which accept writes
  only while the board is locked — and note that the sensor-bus path is not
  exposed to hazard 1.
- README: mark the TODO done.

**Verification**

- Firmware tests: staging refused on a provisioned board; `COMMIT` with a
  missing chunk; `COMMIT` with a wrong CRC; the splice case; chunk 0 clearing
  the stage.
- Host tests: `auto_provision: False` leaves the board alone; the `serial:`
  path raises and the I2C/UART paths do not; a serial mismatch after reboot
  raises.
- Bench, required, and this is the stage that most needs it: a full round trip
  on each of I2C and UART with a second RP2040-Zero as bus master.

**As built (`728978d` firmware, `b0aae51` Klippy)**

- Refusal is silent on I2C as well as UART. The pico-sdk slave handler has
  ACKed every byte before the write completes at Stop, so a NACK is not
  available where the decision is made; the re-read is the acknowledgement on
  both transports anyway.
- The I2C target forwards a write at Stop with the first four data bytes and
  the true length, so a write of the wrong length is ignored rather than
  truncated into a valid one. The UART loop branches on the write bit before
  the CRC byte, since a write datagram is eight bytes and a read four; writes
  get no reply.
- The commit is parked by the bus handler and applied from the main loop, with
  interrupts disabled only around taking it, because it writes flash.
- On success the board reboots through `rr_usb_admin_reboot_application(NULL)`,
  not `rr_usb_admin_acknowledge_before_application_reboot`: there is no USB
  requester, and waiting for CDC to drain could hang.
- Staged bytes are kept in wire order, and the `COMMIT` CRC byte is the last
  wire byte. Klipper's `tmc_uart` packs a register value MSB-first, so the UART
  host sends each chunk as `int.from_bytes(chunk, 'big')` and both transports
  carry identical bytes.
- Two CRC-8s, not to be confused: the commit uses CRC-8/ATM, MSB-first, the
  same as the USB admin frame (`"123456789"` → `0xF4`, UUID `00 01 … 0f` →
  `0x41`); the UART datagram's own CRC is Trinamic's LSB-first one.
- `0x50`–`0x54` are not in `scripts/roadrunner_admin.py`'s `REGISTERS`: `0x52`
  collides with the register-read sync byte there, and the tool never writes.
- The write path lives in `high_resolution_filament_sensor.py`, in the register
  readers, against the identity gate design's note that it belongs in a
  helper. `RegisterReaderGeneric.provision()` writes four chunks and the
  commit; I2C and UART set `bus_provisioning`.
- The failure "raise" at runtime is `printer.invoke_shutdown`, since it happens
  in the poll timer after `klippy:ready`; the `serial:` path raises a real
  config error from `klippy:connect`. The `serial:` error suggests the old
  `serial:` path with the old serial replaced, or
  `/dev/serial/by-id/usb-Vylyne_Roadrunner_<serial>-if00`.
- Timing: the identity is re-read every 2 s while a commit is pending, and the
  commit fails after 20 s. The `serial:` path waits 2 s for the admin reply.
- A locked board is held disconnected (`_identity_locked`), and its identity
  is re-read every 10 s, so one provisioned over USB mid-session is picked up.
  A UART read that answers with the `0xff` refusal fill now counts as no
  reading, so it no longer decodes as `full_turns = -1`.
- An I2C write that fails at the bus fails provisioning at once rather than
  waiting out the timeout.
- Fixed after the first I2C bench run (`d005faa`). "The poll timer sees the
  disconnect" does not hold on I2C: Klipper's `bus.py` invokes a shutdown on
  any I2C bus error, so the first poll during the board's reboot shut the
  printer down. While a commit is pending, `RegisterReaderI2C` reads through
  its own `i2c_transfer` query and treats a bus error as the board being away
  (`expect_reboot()`), so the read-back and the 20 s timeout decide as on UART.
  Outside that window a vanished I2C board still shuts the printer down, as
  before. MCU code old enough to have `i2c_read` fails a bad read on the MCU
  itself, which the host cannot catch, so on such an MCU the provisioning
  reboot still shuts the printer down; the board is provisioned and reads back
  after `FIRMWARE_RESTART`. A read `bus.py` has already failed now returns
  nothing instead of raising `TypeError` in the poll timer.

**Bench, UART DUT, 2026-09-13** — firmware `0defa59` (`roadrunner_v1_uart_rgb`)

- The build directory's `ROADRUNNER_FIRMWARE_VERSION` was a stale cache value
  (`da5b6c1`), so the first flash reported the old version; rebuilt with
  `-DROADRUNNER_FIRMWARE_VERSION=0defa59` and reflashed so the running image is
  identifiable.
- `roadrunner_admin.py clear` left the board `RR-UNPROVISIONED`, identity
  `NONE`. Klipper restarted with `auto_provision` at its default.
- The first identity read after ready logged `provisioning it as
  RR-6CWMKS36PQ9V9RN2ZW9B8901N1`; the board rebooted, 47 UART read errors were
  logged while it was away, then `provisioned as
  RR-6CWMKS36PQ9V9RN2ZW9B8901N1`, and `sensor_connected` went false to true.
  The status object reported that serial with `state: ok`, and USB enumerated
  as `usb-Vylyne_Roadrunner_RR-6CWMKS36PQ9V9RN2ZW9B8901N1-if00`, so the
  identity written over UART is the one the board serves on USB too.
- No UART errors in steady state afterwards. `resets` stayed 0 and position
  held at -1.3314: this Klipper process never had a marker from before the
  reboot, since a locked board answers `0x25` with the locked fill, which the
  extra reads as no marker. So the provisioning reboot is not counted as a
  reset, and it injected no move.
- A second Klipper restart read the provisioned board and connected with no
  provisioning, no UART errors and `reads_failed` 0.

**Bench, I2C DUT, 2026-09-13** — firmware `1085d1e` (`roadrunner_v1_i2c_rgb`,
the same firmware sources), Klipper MCU on `i2c0b` at address 64, 100 kHz

- The bench config now includes one of `roadrunner-i2c.cfg` or
  `roadrunner-uart.cfg` for the `dut` section, so switching transport is one
  line.
- First run, extra at `1085d1e`: the board took the commit over I2C, rebooted
  and enumerated on USB as `RR-57K05Y10T88JF9RPRGE5S128S5`, but the next poll
  got `BUS_TIMEOUT` and `bus.py` shut the printer down, followed by a
  `TypeError` in the poll timer. After `FIRMWARE_RESTART` the board read back
  over I2C as that serial with `state: ok`, so the firmware half had worked.
- Second run, extra at `d005faa`, board cleared again: `provisioning it as
  RR-21B0YQ8Y4K9DBT2GTJVSE4HR3G`, then one `BUS_TIMEOUT` and one
  `START_READ_NACK` logged as the board rebooting, then `provisioned as
  RR-21B0YQ8Y4K9DBT2GTJVSE4HR3G`, `sensor_connected` false to true, printer
  `ready`. `reads_failed` 2, both during the reboot; position -1.3314, `resets`
  0. USB enumerated under the same serial.
- A further Klipper restart connected with no provisioning and `reads_failed`
  0.
- Not exercised on hardware: the `serial:` admin path, and every failure path
  (refused commit, wrong serial, board never returns). Those rest on the host
  tests. The bench MCU's Klipper declares only `i2c_transfer`; I2C
  provisioning on MCU code that still has `i2c_read` was not tried.

## Carried over, not part of this plan

Outstanding from earlier work and unaffected by these stages:

- Bench validation of the image digest — `__flash_binary_end` against what the
  UF2 actually covers, and whether `scripts/uf2_image_digest.py` reproduces a
  real board's report.
- Confirm with mcu-updater that its INFO reader parses `payload_length` rather
  than sizing a fixed 96-byte buffer. Stage 1 changes the serial width it sees.
- LED burst-rate observation over two 30-second cycles.
- The `_unhealthy` latch that survives a sensor re-enable (README TODO).
