# Roadrunner RP2040-zero Firmware

## Dependencies

```sh
sudo apt install cmake gcc-arm-none-eabi libnewlib-arm-none-eabi libstdc++-arm-none-eabi-newlib
```

## Configuring

Some neopixels have RGB color ordering while others have GRB. When the filament is inserted the LED should ligth green, if it is RED then you should use the GRB ordering. Uncomment the following line in `neopixel.h` and re-run `make` to build the firmware.

```c
#define GRB_LED_ORDER 1
```

## Building

```sh
git clone https://github.com/Vylyne/roadrunner.git roadrunner-filament-sensor
cd roadrunner-filament-sensor
git submodule update --init --recursive
cd rp2040
mkdir build
cd build
cmake ..
make
```

You'll find multiple `.uf2` files in the `roadrunner-filament-sensor/rp2040/build` directory. The name contains `uart`, `i2c` or `usbserial` for the communication mode between mcu and sensor, and `rgb` vs `grb` for the neopixel type.

## Flashing

1. Connect RP2040-Zero to computer via USB
2. Press `BOOT` and `RESET` at the same time
3. Release `RESET` and 1 second later release `BOOT`
4. A drive named `RPI-RP2` will appear
5. Copy newly built `.uf2` file to the drive to flash the new firmware.

## Device identity and USB maintenance

Each image reserves the final flash sector for a persistent Roadrunner device
identity. The ROM BOOTSEL serial and the flash-derived USB ID are diagnostics,
not persistent identities.

Provisioned USB serials use the `RR-<26 base32 UUID>` namespace. An ordinary
UF2 update preserves the identity record; the direct-USB `CLEAR_IDENTITY`
maintenance command is the only way to clear the reserved-sector record. See
[`../docs/roadrunner-usb-admin-protocol.md`](../docs/roadrunner-usb-admin-protocol.md)
for the complete byte-level contract and updater rules.

Maintenance commands are available only over Roadrunner's directly connected
USB CDC port. They are never forwarded over I2C, UART, or a Klipper sensor
transport. The legacy USB sensor-register stream beginning with `0xf5`
continues unchanged.

The maintenance frame is:

```
0x52 0x52 0x01 opcode payload_length payload crc8
```

`crc8` is CRC-8/ATM over every preceding byte (polynomial `0x07`, initial
value `0x00`, no reflection, final XOR `0x00`). `INFO` is opcode `0x01`,
`REBOOT_BOOTSEL` is opcode `0x02`, `PROVISION_UUID` is opcode `0x03`, and
`CLEAR_IDENTITY` is opcode `0x04`; responses set bit `0x80` in the opcode.
The status values are `0x00` OK, `0x01` bad CRC, `0x02` bad length, `0x03`
unprovisioned, `0x04` already provisioned, `0x05` identity conflict, `0x06`
identity-store I/O error, and `0x07` confirmation required.

An `INFO` request has an empty payload. Its response payload is exactly:

1. status;
2. identity-store state (`0` none, `1` provisioned, `2` conflict, `3` already
   provisioned, `4` I/O error);
3. transport (`1` I2C, `2` UART, `3` USB);
4. LED order (`1` RGB, `2` GRB);
5. model length and ASCII model (`roadrunner-v1`);
6. firmware-version length and ASCII version (the CMake
   `ROADRUNNER_FIRMWARE_VERSION`, default `dev`);
7. serial length and ASCII serial; and
8. eight raw flash-UID bytes.

`REBOOT_BOOTSEL` has an empty payload. After the successful ACK is written,
the firmware flushes and waits for TinyUSB's transmit-completion callback and
an empty CDC transmit FIFO, then invokes the RP2040 ROM handoff
`reset_usb_boot(0, 0)`. The same physical USB connection
must then enumerate as the `RPI-RP2` mass-storage device. Copy the matching
UF2 and reconnect to query `INFO` again.

`PROVISION_UUID` has a 16-byte UUID payload. It writes the record only when
the identity store is erased, then returns `OK`, a serial length, and the
new `RR-<26 base32 UUID>` serial. `CLEAR_IDENTITY` has the four-byte ASCII
payload `RRCL`; any other four-byte payload returns `confirmation required`
without changing the store. Successful provision and clear responses are
flushed and transmitted completely before the application resets and the CDC
device re-enumerates. INFO and all error/refusal responses do not reset. On
restart,
the USB descriptor reloads the stored identity and presents the same newly
provisioned serial (or the unprovisioned fallback after a clear).

If the maintenance command is unavailable, use the manual fallback: hold
`BOOT`, press and release `RESET`, release `BOOT` after one second, then copy
the matching UF2 to `RPI-RP2`. Use only a bench RP2040-Zero with a known
recovery path for this procedure.

## Bench validation

On a bench RP2040-Zero, the direct USB BOOTSEL command was verified for the
USB-serial, I2C, and UART images in both RGB and GRB variants. Every image
returned the complete ACK `52 52 01 82 01 00 5a` before the board re-enumerated
as `RPI-RP2`. The USB-serial GRB image also returned a complete legacy
`0xf5 0x10` register-read frame, confirming the maintenance parser preserves
the sensor protocol. Provisioning, clear, UF2-retention, and refusal
behavior were validated against the dedicated bench matrix in the protocol
reference on a recoverable RP2040-Zero on 2026-09-02: all six RGB/GRB
transport images reported the correct INFO transport and LED order, retained
their serial across an ordinary UF2 update, and successfully cleared and
reprovisioned a new serial; both USB-serial images also returned the legacy
`F5 10` response; a duplicate `PROVISION_UUID` was rejected with
`ALREADY_PROVISIONED` on all six images, and a bad clear confirmation was
rejected with `CONFIRMATION_REQUIRED` without rebooting. See
[`../docs/roadrunner-usb-admin-protocol.md`](../docs/roadrunner-usb-admin-protocol.md#bench-matrix)
for the full bench matrix and results.
