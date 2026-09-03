# Roadrunner provisioning and identity-maintenance design

## Purpose

This design adds explicit, direct-USB provisioning and identity clearing to
Roadrunner. It completes the path from a blank identity sector to an
independently discoverable board without making ordinary discovery or firmware
updates mutate identity.

## Identity presentation

The persistent identity is exactly a raw, host-generated, random 16-byte UUID.
It is stored only in the two existing validated identity records.

`RR-` is a non-versioned display namespace. Firmware renders a provisioned
UUID as `RR-` followed by 26 uppercase Crockford-base32 characters. A blank
board renders `RR-UNPROVISIONED-` followed by its eight-byte flash UID in
uppercase hexadecimal. The flash UID is diagnostic only: it is never stored
by mcu-updater as the board identity and never used to select a write.

These values are deliberately distinct:

- `RRID` / identity-record format `1` version the on-flash record;
- admin-frame version `0x01` versions the maintenance wire format;
- `roadrunner-v1` identifies the hardware design in INFO; and
- `RR-` namespaces a human-readable serial and contains no version.

USB descriptors and INFO use the same serial formatter.

## Direct-USB protocol additions

All maintenance traffic remains available only on Roadrunner's direct USB CDC
port. The existing frame remains:

```c
0x52 0x52 0x01 opcode payload_length payload crc8
```

CRC is CRC-8/ATM over all preceding bytes. Existing INFO (`0x01`) and
REBOOT_BOOTSEL (`0x02`) keep their current meaning. New requests are:

| Opcode | Name | Payload | Success response | Effect |
| --- | --- | --- | --- | --- |
| `0x03` | `PROVISION_UUID` | exactly 16 raw UUID bytes | status, serial length, rendered serial | writes a blank identity sector, then application-reboots |
| `0x04` | `CLEAR_IDENTITY` | ASCII `RRCL` | status | erases the identity sector, then application-reboots |

Responses set opcode bit `0x80`. Successful provisioning and clear responses
are sent and their transmit completion is observed before an application reset
and CDC re-enumeration. `REBOOT_BOOTSEL` instead enters the ROM BOOTSEL
handoff and re-enumerates as `RPI-RP2`; INFO and all error/refusal responses do
not reset. `CLEAR_IDENTITY` uses a fixed four-byte confirmation to prevent an
empty or malformed request from clearing identity accidentally; it is not an
authentication mechanism.

New status values are:

| Value | Name | Meaning |
| --- | --- | --- |
| `0x04` | `ALREADY_PROVISIONED` | a valid identity already exists; provision did not write |
| `0x05` | `IDENTITY_CONFLICT` | two valid but different records exist; no mutation occurred |
| `0x06` | `STORE_IO_ERROR` | erase, program, or verification failed; no success is claimed |
| `0x07` | `CONFIRMATION_REQUIRED` | `CLEAR_IDENTITY` payload was not exactly `RRCL` |

The existing status values retain their meanings. Bad payload lengths return
`BAD_LENGTH` (`0x02`). Provisioning is allowed only when the store reports no
valid identity. It never replaces an existing identity, even when the supplied
UUID matches it. Clearing is an explicit physical-maintenance operation;
ordinary UF2 updates retain the reserved sector.

## mcu-updater lifecycle

Discovery is read-only. It recognizes a candidate only when both the USB
descriptor and an INFO response agree that it is an unprovisioned Roadrunner.
The resolved USB path is a transient handoff address only.

The untracked-device row exposes **Provision Roadrunner**. After confirmation,
the agent locks the device, generates its UUID with Python's
`secrets.token_bytes(16)`, sends `PROVISION_UUID`, receives the successful ACK,
and waits for the same physical USB connection to re-enumerate. It then sends
INFO again and requires the expected `RR-` serial before releasing the lock.
The board is left visible as a provisioned, untracked Roadrunner; no type,
firmware definition, or persistent registry entry is created automatically.

**Clear identity** is likewise explicit. The agent sends `CLEAR_IDENTITY` only
after user confirmation, waits for re-enumeration, verifies the unprovisioned
INFO state, and leaves the device untracked. Automatic provisioning is not a
default behavior; it may be introduced later as an explicit setting.

## Refusal and recovery rules

- Refuse a candidate whose descriptor, INFO model, protocol version, or
  identity state is inconsistent.
- Refuse if more than one candidate matches an action request.
- Refuse a successful ACK followed by a missing, mismatched, or still
  unprovisioned re-enumeration.
- Do not persist the flash UID or the physical path as board identity.
- On timeout or error, report the last confirmed state and require a fresh
  discovery; do not retry a write blindly.
- Manual BOOTSEL remains the recovery path for an interrupted firmware update.
  A failed provisioning or clear operation must not make the normal application
  flash region inaccessible.

## Verification

The exact byte-level contract is maintained in
[`roadrunner-usb-admin-protocol.md`](roadrunner-usb-admin-protocol.md).

Firmware host tests cover valid and invalid provision/clear frames, every
store result, exact response bytes, identity serial rendering, and the
ACK-to-reset ordering. Firmware builds must link all six RGB/GRB transport
images.

Bench validation uses only a recoverable RP2040-Zero. The 2026-09-02 run
covered initial unprovisioned INFO; provisioning and descriptor/INFO transition
to `RR-`; ordinary UF2 retention; rejection of a second provisioning request;
clear and reprovisioning; bad-clear rejection without reboot; manual BOOTSEL
recovery; and legacy USB reads. Interrupted record-write behavior remains
covered by host tests rather than bench fault injection. mcu-updater tests
cover discovery classification, UUID generation, the confirmed physical-path
handoff, re-enumeration mismatch refusal, and leaving successful devices
untracked.
