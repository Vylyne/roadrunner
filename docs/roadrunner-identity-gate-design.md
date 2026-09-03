# Roadrunner identity gate — design

Status: accepted, 2026-09-03.
Supersedes nothing; extends `roadrunner-provisioning-design.md` and
`roadrunner-usb-admin-protocol.md`, both of which remain authoritative for the
USB admin wire format and the identity namespace.

## Problem

Provisioning today is a policy enforced by hosts, not an invariant enforced by
the device. That policy has to be re-implemented, correctly, in every host that
touches a Roadrunner, and it has already produced four separate hazards:

1. **Flash UID is not unique.** Two Roadrunners built from the same batch report
   the same `pico_get_unique_board_id()`. Their unprovisioned serials therefore
   collide (`RR-UNPROVISIONED-<uid>`), which collides their
   `/dev/serial/by-id/usb-Vylyne_Roadrunner_<serial>-if00` symlinks. udev
   creates one. mcu-updater's `_entry_candidates()` lists that directory by
   name, so with two unprovisioned boards attached it sees **one**, and the
   "refuse ambiguous matches" guard never fires because no ambiguity is visible.
   Confirmed on hardware: provisioning the visible board made the second appear.

2. **Port freedom is not safety.** mcu-updater gates Roadrunner discovery on
   `needs_ports_free = True`. For the USB-serial variant that coincides with
   safety, because Klipper holds the port. For the I2C and UART variants it is
   *inverted*: the USB admin port reads free precisely because Klipper is
   driving the board over a different bus. `PROVISION_UUID` and
   `CLEAR_IDENTITY` both reset the device, so the current code can reset a board
   mid-print.

3. **Provisioning invalidates a `serial:` config line.** A USB-serial variant
   referenced in `printer.cfg` by its by-id path stops resolving the moment its
   serial changes. mcu-updater's `serial_add()` refuses unprovisioned serials,
   but that guard only covers mcu-updater's own registry — a hand-written
   `[high_resolution_filament_sensor]` section is not covered.

4. **"Untracked" carries no information.** Because `serial_add()` structurally
   forbids tracking an unprovisioned board, an unprovisioned Roadrunner is
   always untracked — whether it is on a bench or driving filament on a
   toolhead. mcu-updater's own registry cannot distinguish those cases.

Every one of these is a host compensating for a device that will happily work
without an identity.

## Decision

**A Roadrunner refuses to do its job until it is provisioned.**

The board keeps its autonomous behaviour — it reads the AS5600 and the IR
sensor, and it drives the neopixel. What it refuses is to *answer a host* about
any of it. The identity window and the provisioning path are the only things
available on a host transport until an identity exists.

This is safe to state as an invariant because the Roadrunner is a sensor with no
autonomous actuation. A variant that ever drives something on its own must
revisit it.

### What the invariant buys

- **Unprovisioned implies not live.** No unprovisioned board can be mid-print on
  any transport, so provisioning it can never interrupt one. `assert_printer_idle`
  narrows to what it should always have been: a guard on `CLEAR_IDENTITY` and
  `REBOOT_BOOTSEL` against *provisioned* boards.
- **Hazard 3 disappears at the source.** Nobody can have a working `printer.cfg`
  that references `RR-UNPROVISIONED-…`, because that board never worked.
  Provision-before-configure stops being advice and becomes the only path.
- **Hazard 1 stops mattering for safety.** Colliding unprovisioned serials are
  still a discovery nuisance (see "Still required" below) but no longer a
  correctness risk, because none of the colliding boards is doing anything.
- **All four provisioning paths become safe by construction**, so they can
  coexist without each one needing its own carefully-reasoned guard.

### The four provisioning paths

| Path | Population served | Transport |
| --- | --- | --- |
| mcu-updater, opt-in auto | mcu-updater users | USB admin |
| mcu-updater, manual | mcu-updater users | USB admin |
| `scripts/roadrunner_usb.py` | USB wired, no mcu-updater | USB admin |
| klippy extra, on connect | Klipper-only, I2C/UART wired | I2C / UART / usbserial |

The USB admin layer is present on all six builds ({I2C, UART, USB} × {RGB, GRB}),
so the first three cover anyone with a USB cable attached. The fourth exists
because not everyone wires one — for an I2C-only installation it is the only
route, which is why it is a requirement and not a convenience.

## Firmware design

### The choke point

All three host transports already funnel through a single function:

```c
void prepare_register_data(uint8_t reg, uint8_t *buf, size_t *length);
```

- `i2c_target.c` calls it from `I2C_SLAVE_REQUEST`.
- `usbserial.c` calls it from `usbserial_send_response()`.
- `tmcuart.c` calls it from `tmcuart_send_response()`.

The gate therefore lives in exactly one place. No transport-specific policy.

### Refuse audibly, never go dark

An I2C target that stops ACKing looks like a wiring fault. A silent TMC-UART
looks like a dead board. Both send someone hunting hardware problems that do not
exist. The transport loops keep running unprovisioned, and a blocked read
**returns a response of the expected length, filled with `0xFF`**.

`0xFF` is out of range for almost every existing field: it is not a defined
`MAGNET_STATE_*`, and `0xFFFFFFFF` is not a plausible angle or turn count. An
unpatched vendor extra therefore reads *visibly broken*, not plausibly wrong —
which is the correct failure. A patched extra reads the identity-state register
and knows exactly why.

**One exception, and it is real:** `state.filament_present` is a `bool`, so
`0xFF` at `READ_FILAMENT_PRESENCE` (`0x22`) is *truthy*. A host reading that
register in isolation on a locked board sees "filament present" — plausibly
wrong, not visibly broken. This is accepted rather than fixed with a
per-register refusal value, because a table of per-field sentinels would be
more code, more to keep in step, and still not authoritative. The mitigations
are that `READ_ALL` carries `magnet_state = 0xFF` in the same payload, which
*is* out of range and which the vendor extra surfaces as `magnet_state`, and
that `READ_IDENTITY_STATE` is the authoritative answer any host is expected to
consult. The protocol doc must state the exception explicitly so nobody
concludes a locked board is reporting real filament.

Zero-length responses are explicitly rejected as a refusal encoding, and so is a
bare status byte: `0x03` collides with `MAGNET_STATE_TOO_STRONG` on a one-byte
register.

### Identity register window

A new register range, chosen to clear the existing sensor map (`0x10`,
`0x20`–`0x24`, with `0x20` reserved for the commented-out `READ_HEALTH`):

| Register | Name | Size | Content |
| ---: | --- | ---: | --- |
| `0x30` | `READ_IDENTITY_STATE` | 1 | `rr_identity_status_t` — 0 none, 1 ok, 2 conflict, 3 already provisioned, 4 I/O error |
| `0x31` | `READ_SERIAL` | 34 | ASCII, NUL-padded. `RR_USB_SERIAL_MAX_LENGTH` is 34; the longest real serial, `RR-UNPROVISIONED-<16 hex>`, is 33 |
| `0x32` | `READ_FIRMWARE_VERSION` | 32 | ASCII, NUL-padded |
| `0x33` | `READ_VARIANT` | 2 | transport byte, LED-order byte — same values as the USB `INFO` payload |
| `0x34` | `READ_FLASH_UID` | 8 | Raw diagnostic bytes. **Hosts must not persist this** |

These five registers are **always readable**, provisioned or not. They are the
unprovisioned allow-list and the steady-state identity source, and one
definition serves both — there is no separate "provisioning mode."

Field order and encoding mirror the USB `INFO` response deliberately, so
`roadrunner-usb-admin-protocol.md` stays the single source of truth for what a
Roadrunner's identity *is*, with `INFO` and this window as two encodings of it.

### Status `0x03` serves two roles

`RR_USB_ADMIN_UNPROVISIONED` (`0x03`) currently documents as "INFO status: no
valid identity is stored" — a state. It now also encodes a refusal, returned by
the USB admin layer for any opcode other than `INFO` and `PROVISION_UUID` while
unprovisioned. The protocol doc must say so, or a host reading `0x03` off a
blocked command misreads it as an `INFO` reply.

`REBOOT_BOOTSEL` is included in the refusal set while unprovisioned: there is no
reason to reflash a board that cannot be used, and allowing it widens the
attack surface of an interface that is otherwise read-plus-provision only.

`CLEAR_IDENTITY` is **not** gated the same way, and the difference matters.
`RR_IDENTITY_CONFLICT` and `RR_IDENTITY_IO_ERROR` are exactly the states a clear
exists to recover from, so gating clear on `status == RR_IDENTITY_OK` would lock
a conflicted board out of its own repair path. Clear is refused only when the
status is `RR_IDENTITY_NONE` — there is genuinely nothing to erase.

The resulting opcode table while unprovisioned:

| Opcode | `NONE` | `CONFLICT` / `IO_ERROR` | `OK` |
| --- | --- | --- | --- |
| `INFO` | allowed | allowed | allowed |
| `PROVISION_UUID` | allowed | allowed (store decides) | refused `04` |
| `CLEAR_IDENTITY` | refused `03` | allowed | allowed |
| `REBOOT_BOOTSEL` | refused `03` | refused `03` | allowed |

### LED behaviour

Current main-loop behaviour, unchanged as the steady state:

| Condition | Indication |
| --- | --- |
| `magnet_state != MAGNET_STATE_DETECTED` | blink RED, 100 ms |
| `!filament_present` | solid BLUE |
| otherwise | solid GREEN |

The bring-up procedure depends on this: load filament, read BLUE, trim the lever
arm, read BLUE, trim again, until GREEN. That is an iterative physical fitting
loop lasting minutes with a human watching the LED — so a boot-only
unprovisioned announcement would be missed, and a continuous one would fight the
readout the human is actually using.

**Unprovisioned boards therefore burst amber periodically**: a fast amber blink
(50 ms half-period) for 1500 ms, repeating every 30000 ms, with the first burst
at power-on. Between bursts the diagnostic indication owns the LED completely.
At a 5% duty cycle the fitting loop is never meaningfully obscured, and nobody
works for minutes without seeing several bursts.

The 50 ms rate is deliberately faster than the 100 ms RED magnet blink, so the
two read as different channels rather than as a new sensor state — hue alone is
too weak a signal between amber and red on a WS2812.

Because `PROVISION_UUID` resets the board, **the absence of amber on the next
boot is the success confirmation** — observable with no host, no tool, and no
serial console. For an I2C-only user running an unpatched vendor extra, that is
the entire feedback loop.

## Host design

> **Cross-repo intent, not verified against this tree.** Everything below names
> code in `mcu-updater` and `buffer_manager`, read on 2026-09-03 but not
> reachable from this repository. A firmware reviewer cannot check these
> references and should not try; they are recorded here so the three repos argue
> from one document. Treat any specific symbol name as a pointer to re-verify,
> not as a fact about the current state of those repos. The firmware sections
> above are verifiable in this tree and are binding.

### Klippy extra

`high_resolution_filament_sensor.py` reads the identity window at connect,
caches it (identity is immutable between reboots — it must not ride the 100 ms
`CHECK_RUNOUT_TIMEOUT` path), and publishes `serial`, `firmware_version` and
`variant` through `get_status()`. Moonraker then exposes them in
`printer.objects`, and mcu-updater reads them exactly as it already reads
`print_stats` and `idle_timeout`.

That is what finally answers `tracked_by` for I2C and UART boards: Klipper
states the fact rather than mcu-updater inferring it from a transport byte.

On-connect provisioning, when enabled:

- Opt-in config key on the section, default off. Fires only when that section
  reads unprovisioned; never touches a provisioned board.
- **Provision, then raise a config error** — do not block a klippy connect
  handler waiting on the device reset. The message names the new serial:
  `provisioned RR-…, restart Klipper`.
- For a `serial:`-configured USB variant the message must also name the new
  by-id path, `usb-Vylyne_Roadrunner_<serial>-if00`, because that config line is
  now stale. I2C addresses and UART pins are identity-free and survive
  untouched, which is why only this variant needs the longer message.
- The flow is loop-safe **by the protocol's own refusal rule**: `PROVISION_UUID`
  "never replaces an existing identity, including when the UUID is identical",
  so the restart reads a valid identity and proceeds. A provisioning loop is
  structurally impossible.
- The write path belongs in a helper the extra calls, **not** in
  `high_resolution_filament_sensor.py` itself. That module is vendored from
  `roadrunner-filament-sensor`; keeping the flash write outside it holds the
  vendor patch down to a few lines of read-and-report.

### mcu-updater

- **Discovery must stop enumerating `/dev/serial/by-id` by name.** Use
  `usb.collect()` or `/dev/serial/by-path` so colliding unprovisioned serials
  are still individually addressable. `RoadrunnerDevice.topology` already exists
  for this handoff.
- **Auto-provision is opt-in**, and `auto_provision` belongs in
  `RegistryMethods.LOUD_SETTINGS` alongside `enable_flashing` and
  `allow_flash_while_printing` — "the agent log is the only audit trail there
  is," and this authorizes unattended flash writes.
- **Auto-provision must fail closed.** `assert_printer_idle` deliberately warns
  and continues when the activity query throws, and is short-circuited by
  `allow_flash_while_printing`. Both are right for a flash a human just clicked
  and is watching; both are wrong for an unattended write. Auto-provision
  honours neither escape hatch.
- **Pre-provision printer.cfg check.** Before provisioning a USB-variant board,
  read `configfile.settings` via Moonraker and warn if the board's current by-id
  path appears as a `serial:` value. `CfgDocument` exists if the rewrite is
  offered.
- **Row labelling.** A row whose identity window reports transport I2C or UART
  should say so. Those boards render as untracked while being driven by the
  printer, and the row is the only place that can correct the impression.

### BOOTSEL closed loop

Separate work, unblocked by this design but not part of it. Notes so they are
not rediscovered:

- `REBOOT_BOOTSEL` (`0x02`) already exists in the admin protocol.
- Flash UID **cannot** correlate a board across the reboot — it is not unique.
  Topology is the only key. `/dev/disk/by-path` ↔ `/dev/serial/by-path`, matched
  on the USB port prefix: the CDC path ends `…usb-0:1.2:1.0` while the MSC path
  ends `…usb-0:1.2:1.0-scsi-0:0:0:0`, so the match is prefix-normalized, never
  string equality.
- The udev rule hardcodes `/media/%USER%/RPI-RP2`, so a second BOOTSEL board
  loses. Mount at `/media/%USER%/BOOTSEL/by-path/$env{ID_PATH_TAG}` instead —
  `ID_PATH_TAG` is the filename-safe variant, and the rule installs as
  `99-mcu-updater-bootsel.rules`, after `60-persistent-storage.rules`, so it is
  populated. `bootsel_scan()` then globs and leans on its existing
  `INFO_UF2.TXT` marker check, which stops being belt-and-braces and becomes the
  only thing distinguishing a real bootloader volume.
- That removes `_find_mount()`'s ">1 volume" refusal, which today also fires
  when an unrelated spare Pico is attached.
- `Bootsel.needs_services_stopped` flips to `True` — its own comment predicts
  this — and `settled()` becomes a real wait, because the returning serial is
  finally knowable.
- `flashers/bootsel.py`'s `target_for()` claims the flash UID records "a real
  identity". That claim is false and goes with the rest.

## Still required, and explicitly not solved here

- **Provisioning writes over I2C and UART.** The identity window is read-only in
  this design, and `i2c_target.c` currently discards all writes ("we do not
  support I2C writes"). Until a write path exists, the klippy extra can
  provision over usbserial only, and an I2C-only or UART-only installation still
  needs a USB cable once. This is deliberately deferred: the read window is the
  harder decision and the easier build, and the write path benefits from the
  register map already existing.
- **The `scripts/roadrunner_usb.py` gap.** It is stdlib-only at the top level
  and speaks the protocol directly, but "deliberately neither scans ports nor
  creates identities" — today it needs mcu-updater to hand it a port and a UUID.
  A port scan and a `--generate` flag turn it into the one-file provisioning
  tool the README can point at, which is what makes the firmware's hard
  dependency on a provisioning host acceptable.
- **Upstreaming.** The identity fields in `get_status()` are a patch to a
  vendored module. Carrying it forever or upstreaming it is a decision to make
  deliberately, not at the next vendor sync.
- **A model register.** The USB `INFO` payload carries a model field
  (`roadrunner-v1`), and this document's own "Identity and updater rules" require
  an updater to validate it. The register window has no equivalent, so a host
  reading identity over I2C or UART cannot perform that validation. Deferred to
  the klippy-extra plan rather than added speculatively: the model string is
  file-static in `usb_admin.c`, so exposing it means hoisting it across a module
  boundary, and that encoding is better chosen once a real consumer defines what
  it needs. Until then, model validation is available only over USB.

## Accepted costs

A board flashed with this firmware and never provisioned is inert. That is the
point, but it lands hardest on someone running this firmware with the unpatched
vendor extra and no USB cable, who gets a board that never works. The amber
burst and the standalone script are the two mitigations; both need to be
documented prominently in the README, not just in this file.
