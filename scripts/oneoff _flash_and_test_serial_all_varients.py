#!/usr/bin/env python3
import glob
import os
import shutil
import struct
import subprocess
import time
import uuid
from pathlib import Path

import serial # pyright: ignore[reportMissingModuleSource]

SYNC = bytes.fromhex("52 52 01")

OPCODE_INFO = 0x01
OPCODE_REBOOT_BOOTSEL = 0x02
OPCODE_PROVISION_UUID = 0x03
OPCODE_CLEAR_IDENTITY = 0x04

STATUS_OK = 0x00
STATUS_UNPROVISIONED = 0x03
STATUS_ALREADY_PROVISIONED = 0x04
STATUS_CONFIRMATION_REQUIRED = 0x07

CLEAR_CONFIRMATION = b"RRCL"

PORT_GLOB = "/dev/serial/by-id/usb-Vylyne_Roadrunner_RR*"
BOOTSEL_MOUNT = Path("/media/klipper/RPI-RP2")


def crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def build_frame(opcode: int, payload: bytes = b"") -> bytes:
    body = SYNC + bytes([opcode, len(payload)]) + payload
    return body + bytes([crc8(body)])


def send_admin(port: str, opcode: int, payload: bytes = b"", timeout: float = 2) -> tuple[int, bytes]:
    frame = build_frame(opcode, payload)
    with serial.Serial(port, 115200, timeout=timeout) as dev:
        dev.reset_input_buffer()
        time.sleep(0.05)
        dev.reset_input_buffer()  # catch any boot-log bytes that trickled in
        dev.write(frame)
        dev.flush()

        header = dev.read(5)
        if len(header) != 5 or header[:3] != SYNC:
            raise SystemExit(f"malformed response header: {header.hex(' ')}")
        resp_opcode, length = header[3], header[4]
        if resp_opcode != (opcode | 0x80):
            raise SystemExit(f"unexpected response opcode: {resp_opcode:#04x}")

        rest = dev.read(length + 1)
        if len(rest) != length + 1:
            raise SystemExit(f"truncated response body: {rest.hex(' ')}")
        resp_payload, crc = rest[:-1], rest[-1]
        if crc8(header + resp_payload) != crc:
            raise SystemExit(f"bad response CRC: {(header + rest).hex(' ')}")

        if length < 1:
            raise SystemExit("response payload missing status byte")
        status = resp_payload[0]
        return status, resp_payload[1:]


def _read_length_prefixed(data: bytes, offset: int) -> tuple[str, int]:
    n = data[offset]
    value = data[offset + 1 : offset + 1 + n].decode("ascii")
    return value, offset + 1 + n


def info(port: str) -> dict:
    status, payload = send_admin(port, OPCODE_INFO)
    store_state, transport, led_order = payload[0], payload[1], payload[2]
    offset = 3
    model, offset = _read_length_prefixed(payload, offset)
    fw_version, offset = _read_length_prefixed(payload, offset)
    serial_str, offset = _read_length_prefixed(payload, offset)
    flash_uid = payload[offset : offset + 8]
    return {
        "provisioned": status == STATUS_OK,
        "store_state": store_state,
        "transport": transport,
        "led_order": led_order,
        "model": model,
        "fw_version": fw_version,
        "serial": serial_str,
        "flash_uid": flash_uid,
    }


def clear_identity(port: str) -> None:
    status, _ = send_admin(port, OPCODE_CLEAR_IDENTITY, CLEAR_CONFIRMATION)
    if status != STATUS_OK:
        raise SystemExit(f"CLEAR_IDENTITY failed with status {status:#04x}")


def clear_identity_bad_confirmation(port: str) -> None:
    status, _ = send_admin(port, OPCODE_CLEAR_IDENTITY, b"NOPE")
    if status != STATUS_CONFIRMATION_REQUIRED:
        raise SystemExit(
            f"CLEAR_IDENTITY with bad confirmation expected CONFIRMATION_REQUIRED, "
            f"got status {status:#04x}"
        )


def provision_uuid(port: str) -> str:
    new_uuid = uuid.uuid4().bytes
    status, payload = send_admin(port, OPCODE_PROVISION_UUID, new_uuid)
    if status != STATUS_OK:
        raise SystemExit(f"PROVISION_UUID failed with status {status:#04x}")
    serial_str, _ = _read_length_prefixed(payload, 0)
    return serial_str


def provision_uuid_expect_rejected(port: str) -> None:
    status, _ = send_admin(port, OPCODE_PROVISION_UUID, uuid.uuid4().bytes)
    if status != STATUS_ALREADY_PROVISIONED:
        raise SystemExit(
            f"second PROVISION_UUID expected ALREADY_PROVISIONED, got status {status:#04x}"
        )


def reboot_bootsel(port: str) -> None:
    status, _ = send_admin(port, OPCODE_REBOOT_BOOTSEL)
    if status != STATUS_OK:
        raise SystemExit(f"REBOOT_BOOTSEL failed with status {status:#04x}")


def test_usbserial(port: str) -> None:
    with serial.Serial(port, 115200, timeout=0.2) as dev:
        dev.reset_input_buffer()
        dev.write(bytes([0xF5, 0x10]))
        dev.flush()

        received = bytearray()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            received.extend(dev.read(dev.in_waiting or 1))
            marker = received.find(b"\x05\xff\x10")
            if marker >= 0 and len(received) >= marker + 13:
                frame = received[marker : marker + 13]
                magnet, filament, turns, angle = struct.unpack("<BBll", frame[3:])
                print("frame:   ", frame.hex(" "))
                print(
                    f"magnet={magnet} filament={filament} turns={turns} angle={angle}"
                )
                if magnet == 0xFF:
                    raise SystemExit(
                        "sensor replied, but AS5600 magnet state is unknown"
                    )
                return
        raise SystemExit(f"no complete legacy reply; received: {received.hex(' ')}")


def wait_for_disconnect(port: str, timeout: float = 5) -> None:
    time.sleep(1)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not os.path.exists(port):
            return
        time.sleep(0.05)
    raise SystemExit(f"{port} did not disconnect within {timeout}s")


def wait_for_serial_port(timeout: float = 10, settle: float = 0.3) -> str:
    """Wait for exactly one matching port, then confirm it stays put for
    `settle` seconds before trusting it — right after a reset, udev can
    briefly report a stale or about-to-change symlink."""
    def live_matches() -> list[str]:
        # glob lists directory entries even for a symlink whose target is
        # already gone (udev can lag in cleaning up after a reset); only
        # trust entries that actually resolve.
        return [m for m in glob.glob(PORT_GLOB) if os.path.exists(m)]

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = live_matches()
        if len(matches) > 1:
            raise SystemExit(f"multiple Roadrunner serial ports found: {matches}")
        if len(matches) == 1:
            candidate = matches[0]
            settle_deadline = time.monotonic() + settle
            stable = True
            while time.monotonic() < settle_deadline:
                time.sleep(0.05)
                if live_matches() != [candidate]:
                    stable = False
                    break
            if stable:
                return candidate
        time.sleep(0.1)
    raise SystemExit(f"no Roadrunner serial port found matching {PORT_GLOB}")


def wait_for_bootsel_mount(timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.ismount(BOOTSEL_MOUNT):
            if not os.access(BOOTSEL_MOUNT, os.W_OK):
                raise SystemExit(
                    f"{BOOTSEL_MOUNT} is mounted but not writable by this user "
                    f"(uid={os.getuid()}); check the automount's uid/gid options" # pyright: ignore[reportAttributeAccessIssue]
                )
            return
        time.sleep(0.1)
    raise SystemExit(f"{BOOTSEL_MOUNT} did not appear within {timeout}s")


def picotool_info() -> None:
    result = subprocess.run(
        ["picotool", "info"], capture_output=True, text=True, check=False
    )
    print(result.stdout)
    if result.returncode != 0:
        raise SystemExit(f"picotool info failed:\n{result.stderr}")


def flash_and_test(uf2: Path) -> None:
    print(f"=== {uf2.name} ===")

    wait_for_bootsel_mount()
    shutil.copy(uf2, BOOTSEL_MOUNT / uf2.name)

    port = wait_for_serial_port()
    print(f"port: {port}")

    result = info(port)
    print(f"INFO: {result}")
    if result["provisioned"]:
        clear_identity(port)
        wait_for_disconnect(port)  # CLEAR_IDENTITY resets and re-enumerates
        port = wait_for_serial_port()
        print(f"port: {port}")
        result = info(port)
        if result["provisioned"]:
            raise SystemExit(
                f"CLEAR_IDENTITY reported OK but device is still provisioned "
                f"as {result['serial']} after reset"
            )
        print("cleared existing identity")

    new_serial = provision_uuid(port)
    wait_for_disconnect(port)  # PROVISION_UUID resets and re-enumerates
    port = wait_for_serial_port()
    print(f"port: {port}")
    result = info(port)
    if not result["provisioned"] or result["serial"] != new_serial:
        raise SystemExit(
            f"PROVISION_UUID reported success as {new_serial} but device "
            f"re-enumerated as {result['serial']!r} (provisioned={result['provisioned']}); "
            "identity did not persist across reset"
        )
    print(f"provisioned: {new_serial}")

    provision_uuid_expect_rejected(port)
    print("second PROVISION_UUID correctly rejected (ALREADY_PROVISIONED)")

    clear_identity_bad_confirmation(port)
    result = info(port)
    if not result["provisioned"] or result["serial"] != new_serial:
        raise SystemExit(
            f"bad CLEAR_IDENTITY confirmation should not change identity, but "
            f"device is now {result['serial']!r} (provisioned={result['provisioned']})"
        )
    print("bad CLEAR_IDENTITY confirmation correctly rejected (CONFIRMATION_REQUIRED), no reboot")

    if "usbserial" in uf2.name:
        test_usbserial(port)

    reboot_bootsel(port)

    wait_for_bootsel_mount()
    picotool_info()


def ensure_bootsel() -> None:
    if os.path.ismount(BOOTSEL_MOUNT):
        return

    matches = [m for m in glob.glob(PORT_GLOB) if os.path.exists(m)]
    if len(matches) > 1:
        raise SystemExit(f"multiple Roadrunner serial ports found: {matches}")
    if len(matches) == 1:
        print(f"device in application mode on {matches[0]}, rebooting to BOOTSEL")
        reboot_bootsel(matches[0])
        wait_for_bootsel_mount()
        return

    raise SystemExit(
        f"no device found in BOOTSEL ({BOOTSEL_MOUNT}) or application mode "
        f"({PORT_GLOB}); connect the board and put it in BOOTSEL"
    )


def main() -> None:
    uf2_files = sorted(Path.cwd().glob("*.uf2"))
    if not uf2_files:
        raise SystemExit("no .uf2 files found in current directory")

    ensure_bootsel()

    for uf2 in uf2_files:
        flash_and_test(uf2)


if __name__ == "__main__":
    main()
