#!/usr/bin/env python3
import os
import re
import shutil
import string
import struct
import subprocess
import time
import uuid
from pathlib import Path

import serial  # pyright: ignore[reportMissingModuleSource]
import serial.tools.list_ports  # pyright: ignore[reportMissingModuleSource]

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

# Boards this script must never touch. Every step here is destructive - it
# reboots to BOOTSEL and reflashes - so anything installed in a printer belongs
# on this list. Entries are the serial as reported over USB, with or without
# the "RR-" prefix.
#
# A typo here is the failure that reflashes a live board, so `check_exclusions`
# refuses to run on a malformed entry rather than silently protecting nothing.
EXCLUDED_SERIALS = ("5K3DNTFCR1B3C9D0RZMYA3Y720", "16NVDWH76ET5WG3TTEQN8HQ73A")

USB_MANUFACTURER = "Vylyne"
USB_PRODUCT = "Roadrunner"

# A provisioned serial is RR- plus 26 Crockford base32 characters (no I/L/O/U);
# an unprovisioned one is RR-UNPROVISIONED- plus the 16-hex flash UID.
PROVISIONED_RE = re.compile(r"^RR-[0-9A-HJKMNP-TV-Z]{26}$")
UNPROVISIONED_RE = re.compile(r"^RR-UNPROVISIONED-[0-9A-F]{16}$")

# The RP2040 boot ROM's volume label, and the file every UF2 bootloader
# publishes at its root. The marker is what actually identifies the drive - an
# unrelated volume can share the label.
BOOTSEL_VOLUME_NAME = "RPI-RP2"
BOOTSEL_MARKER = "INFO_UF2.TXT"


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


def send_admin(
    port: str, opcode: int, payload: bytes = b"", timeout: float = 2
) -> tuple[int, bytes]:
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


def read_register(port: str, reg: int, size: int, timeout: float = 2) -> bytes:
    """One legacy 0xf5-framed register read, returning just the payload.

    The reply is 0x05 0xff <reg> followed by `size` bytes. `size` has to be
    supplied because the framing carries no length - the caller and the
    firmware have to agree, which is why the register table is documented.
    """
    with serial.Serial(port, 115200, timeout=0.2) as dev:
        dev.reset_input_buffer()
        dev.write(bytes([0xF5, reg]))
        dev.flush()

        header = bytes([0x05, 0xFF, reg])
        received = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            received.extend(dev.read(dev.in_waiting or 1))
            marker = received.find(header)
            if marker >= 0 and len(received) >= marker + 3 + size:
                return bytes(received[marker + 3 : marker + 3 + size])
    raise SystemExit(
        f"no complete reply for register {reg:#04x}; received: {received.hex(' ')}"
    )


def test_usbserial_locked(port: str) -> None:
    """A board with no valid identity must refuse sensor data but still answer.

    This is the one behaviour on the identity-gate branch that no host test can
    reach: the firmware answers with the register's own length filled with
    0xff, rather than going silent. Silence would be indistinguishable from a
    wiring fault, so "it replied, and the reply is refusal-shaped" is the
    assertion that matters.
    """
    payload = read_register(port, 0x10, 10)
    print("locked READ_ALL:  ", payload.hex(" "))
    if payload != b"\xff" * 10:
        raise SystemExit(
            f"expected ten 0xff bytes from a board with no valid identity, got "
            f"{payload.hex(' ')} - the register gate is not refusing"
        )

    # The identity window must stay readable while everything else is refused;
    # it is both the allow-list and the diagnostic that explains the refusal.
    state = read_register(port, 0x30, 1)
    if state[0] != 0x00:
        raise SystemExit(
            f"READ_IDENTITY_STATE reported {state[0]:#04x} on a board that "
            f"should have no identity (expected 0x00 / RR_IDENTITY_NONE)"
        )

    serial_bytes = read_register(port, 0x31, 34)
    reported = serial_bytes.split(b"\x00", 1)[0].decode("ascii", "replace")
    print("locked serial:    ", reported)
    if not UNPROVISIONED_RE.match(reported):
        raise SystemExit(
            f"READ_SERIAL returned {reported!r}, which is not an unprovisioned "
            f"Roadrunner serial"
        )
    print("gate holds: sensor registers refused, identity window readable")


def test_bootsel_while_locked(uf2: Path, port: str) -> str:
    """REBOOT_BOOTSEL must work on a board with no valid identity.

    This is the one path that recovers a board the firmware otherwise refuses
    to serve: `rr_identity_clear` will not erase a conflicted sector, so a
    board in that state cannot be repaired by CLEAR_IDENTITY either. An earlier
    revision gated BOOTSEL on a valid identity and thereby closed every
    software route out. Ungating it is the fix, and this is the only test of
    that fix which touches real hardware.

    Leaves the board back in application mode and still unprovisioned, by
    reflashing the same image - a normal UF2 update preserves the identity
    sector, so an erased sector stays erased. Returns the new port.
    """
    status, _ = send_admin(port, OPCODE_REBOOT_BOOTSEL)
    if status == STATUS_UNPROVISIONED:
        raise SystemExit(
            "REBOOT_BOOTSEL was refused with 0x03 on a board with no valid "
            "identity. That gate was removed deliberately: with it, a "
            "conflicted board has no software recovery path at all, because "
            "CLEAR_IDENTITY cannot erase a conflicted sector either. This "
            "firmware predates that fix, or it has regressed."
        )
    if status != STATUS_OK:
        raise SystemExit(
            f"REBOOT_BOOTSEL on an unprovisioned board failed with status {status:#04x}"
        )

    volume = wait_for_bootsel_mount()
    print("BOOTSEL reachable without an identity")

    shutil.copy(uf2, volume / uf2.name)
    new_port = wait_for_serial_port()
    result = info(new_port)
    if result["provisioned"]:
        raise SystemExit(
            f"reflashing should preserve an erased identity sector, but the "
            f"board came back provisioned as {result['serial']}"
        )
    print(f"reflashed, still unprovisioned; port: {new_port}")
    return new_port


def test_usbserial(port: str) -> None:
    payload = read_register(port, 0x10, 10)
    magnet, filament, turns, angle = struct.unpack("<BBll", payload)
    print("frame:   ", (b"\x05\xff\x10" + payload).hex(" "))
    print(f"magnet={magnet} filament={filament} turns={turns} angle={angle}")

    # 0xff means two very different things now, and conflating them sends
    # somebody hunting a wiring fault that does not exist. A locked board fills
    # every sensor register with 0xff; a provisioned board with no magnet
    # attached reports MAGNET_STATE_UNKNOWN (0). Ask the identity register
    # which case this is rather than guessing from the sensor value.
    if magnet == 0xFF:
        state = read_register(port, 0x30, 1)
        if state[0] != 0x01:
            raise SystemExit(
                f"sensor registers are refused: the board has no valid identity "
                f"(READ_IDENTITY_STATE = {state[0]:#04x}). This is the gate "
                f"working, not a sensor fault - provision the board first."
            )
        raise SystemExit(
            "board is provisioned and the gate is open, but AS5600 magnet state "
            "reads 0xff - a genuine sensor fault"
        )
    if magnet == 0x00:
        print("note: magnet state is UNKNOWN - expected on a bare board with no AS5600")


def wait_for_disconnect(port: str, timeout: float = 5) -> None:
    time.sleep(1)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Not os.path.exists: a COM port is not a filesystem path on Windows,
        # and a by-id symlink can outlive the device on Linux.
        if port not in [device for device, _ in roadrunner_ports()]:
            return
        time.sleep(0.05)
    raise SystemExit(f"{port} did not disconnect within {timeout}s")


def normalise_serial(value: str) -> str:
    """Strip the RR- prefix so the exclusion list can be written either way."""
    return value[3:] if value.startswith("RR-") else value


def check_exclusions() -> None:
    """Refuse to run on a malformed exclusion entry.

    A typo in this list silently protects nothing, and every step in this
    script is destructive. A well-formed entry that simply is not attached is
    fine and only warned about - that is the ordinary "board is unplugged"
    case, and it cannot be distinguished from a typo by absence alone, which
    is exactly why the shape is checked instead.
    """
    for entry in EXCLUDED_SERIALS:
        full = entry if entry.startswith("RR-") else f"RR-{entry}"
        if not (PROVISIONED_RE.match(full) or UNPROVISIONED_RE.match(full)):
            raise SystemExit(
                f"excluded serial {entry!r} is not a valid Roadrunner serial "
                f"({len(normalise_serial(entry))} characters; a provisioned "
                f"serial has 26). Refusing to run: an unmatched exclusion "
                f"protects nothing, and every step here reflashes the board."
            )

    attached = {
        normalise_serial(s) for _, s in roadrunner_ports(apply_exclusions=False)
    }
    for entry in EXCLUDED_SERIALS:
        if normalise_serial(entry) not in attached:
            print(f"note: excluded board {entry} is not currently attached")


def roadrunner_ports(apply_exclusions: bool = True) -> list[tuple[str, str]]:
    """Every attached Roadrunner as (device, serial), minus the excluded ones.

    Replaces the Linux-only /dev/serial/by-id glob: pyserial reports the USB
    descriptor fields on every platform, and matching them directly avoids
    depending on udev naming. It also sidesteps the by-id collision two
    unprovisioned boards would cause - they share a flash UID, so they share a
    serial, and only one symlink can exist.
    """
    found = []
    for port in serial.tools.list_ports.comports():
        if (port.manufacturer or "") != USB_MANUFACTURER:
            continue
        if (port.product or "") != USB_PRODUCT:
            continue
        number = port.serial_number or ""
        if not number.startswith("RR-"):
            continue
        if apply_exclusions and normalise_serial(number) in {
            normalise_serial(e) for e in EXCLUDED_SERIALS
        }:
            continue
        found.append((port.device, number))
    return sorted(found)


def wait_for_serial_port(timeout: float = 10, settle: float = 0.3) -> str:
    """Wait for exactly one matching port, then confirm it stays put for
    `settle` seconds before trusting it — right after a reset the OS can
    briefly report a stale or about-to-change device."""

    def live_matches() -> list[str]:
        return [device for device, _ in roadrunner_ports()]

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
    raise SystemExit(
        "no Roadrunner serial port found "
        f"(manufacturer={USB_MANUFACTURER!r}, product={USB_PRODUCT!r}, "
        f"serial starting RR-, excluding {len(EXCLUDED_SERIALS)} board(s))"
    )


def bootsel_volume() -> Path | None:
    """The mounted RP2040 boot ROM volume, or None.

    Identified by INFO_UF2.TXT at the root rather than by volume label: an
    unrelated drive can be labelled RPI-RP2, and on Windows the label is not
    part of the path anyway. Searches drive letters on Windows and the usual
    automount roots elsewhere.
    """
    if os.name == "nt":
        roots = [Path(f"{letter}:/") for letter in string.ascii_uppercase]
    else:
        roots = [
            Path(parent) / user / BOOTSEL_VOLUME_NAME
            for parent in ("/media", "/run/media")
            for user in _listdir(parent)
        ]
    for root in roots:
        try:
            if (root / BOOTSEL_MARKER).is_file():
                return root
        except OSError:
            continue
    return None


def _listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def wait_for_bootsel_mount(timeout: float = 10) -> Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        volume = bootsel_volume()
        if volume is not None:
            if not os.access(volume, os.W_OK):
                raise SystemExit(
                    f"{volume} is mounted but not writable by this user; "
                    f"on Linux check the automount's uid/gid options"
                )
            return volume
        time.sleep(0.1)
    raise SystemExit(
        f"no {BOOTSEL_VOLUME_NAME} volume (a directory containing "
        f"{BOOTSEL_MARKER}) appeared within {timeout}s"
    )


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
    shutil.copy(uf2, wait_for_bootsel_mount() / uf2.name)

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

    # The board has no valid identity at exactly this point, and only here.
    # Everything after provisioning tests an open gate, so this is the one
    # window in the run where the refusal behaviour is observable at all.
    if "usbserial" in uf2.name:
        test_usbserial_locked(port)
        port = test_bootsel_while_locked(uf2, port)

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
    print(
        "bad CLEAR_IDENTITY confirmation correctly rejected (CONFIRMATION_REQUIRED), no reboot"
    )

    if "usbserial" in uf2.name:
        test_usbserial(port)

    reboot_bootsel(port)

    wait_for_bootsel_mount()
    picotool_info()


def ensure_bootsel() -> None:
    if bootsel_volume() is not None:
        return

    matches = roadrunner_ports()
    if len(matches) > 1:
        # Refuse rather than pick. Every step after this reflashes whatever it
        # chose, so an ambiguous bus is not something to resolve by guessing.
        listing = ", ".join(f"{device} ({number})" for device, number in matches)
        raise SystemExit(
            f"multiple Roadrunner boards attached after exclusions: {listing}. "
            f"Unplug all but the bench board, or add the others to "
            f"EXCLUDED_SERIALS."
        )
    if len(matches) == 1:
        device, number = matches[0]
        print(f"device in application mode on {device} ({number}), rebooting to BOOTSEL")
        reboot_bootsel(device)
        wait_for_bootsel_mount()
        return

    raise SystemExit(
        f"no device found in BOOTSEL (no directory containing "
        f"{BOOTSEL_MARKER}) or in application mode; connect the bench board "
        f"and put it in BOOTSEL"
    )


def main() -> None:
    uf2_files = sorted(Path.cwd().glob("*.uf2"))
    if not uf2_files:
        raise SystemExit("no .uf2 files found in current directory")

    check_exclusions()
    ensure_bootsel()

    for uf2 in uf2_files:
        flash_and_test(uf2)


if __name__ == "__main__":
    main()
