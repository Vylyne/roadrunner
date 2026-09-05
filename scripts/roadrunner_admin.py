#!/usr/bin/env python3
"""Manual admin operations on a Roadrunner over USB serial.

Subcommands for scripting, or a menu when run with no arguments.  This is the
hands-on counterpart to `oneoff_flash_and_test.py`: that script drives a fixed
destructive sequence across every UF2 in a directory, this one does one thing
at a time to one board you name.

Every command acts on one board you named, or on the only one attached, so
there is no exclusion list here: the bulk script needs one because it walks
whatever it finds, and this tool never does.  What stands in for it is the
confirmation on `provision`, `clear`, `bootsel` and `flash` - a reset drops
whatever Klipper connection the board is serving, and on the I2C and UART
variants that connection is invisible from here, because there is no serial
lock to trip over.

Register and opcode values follow docs/roadrunner-identity-gate-design.md.
"""

import argparse
import os
import re
import shutil
import string
import struct
import sys
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

STATUS_NAMES = {
    0x00: "OK",
    0x01: "BAD_CRC",
    0x02: "BAD_LENGTH",
    0x03: "UNPROVISIONED",
    0x04: "ALREADY_PROVISIONED",
    0x05: "IDENTITY_CONFLICT",
    0x06: "IO_ERROR",
    0x07: "CONFIRMATION_REQUIRED",
}
STATUS_OK = 0x00
STATUS_UNPROVISIONED = 0x03
STATUS_ALREADY_PROVISIONED = 0x04
STATUS_CONFIRMATION_REQUIRED = 0x07

CLEAR_CONFIRMATION = b"RRCL"

IDENTITY_STATE_NAMES = {
    0x00: "NONE",
    0x01: "OK",
    0x02: "CONFLICT",
    0x03: "ALREADY_PROVISIONED",
    0x04: "IO_ERROR",
}
IDENTITY_OK = 0x01

TRANSPORT_NAMES = {1: "i2c", 2: "uart", 3: "usbserial"}
LED_ORDER_NAMES = {1: "RGB", 2: "GRB"}

USB_MANUFACTURER = "Vylyne"
USB_PRODUCT = "Roadrunner"

# An unprovisioned serial is RR-UNPROVISIONED- plus the 16-hex flash UID; a
# provisioned one is RR- plus 26 Crockford base32 characters.
UNPROVISIONED_RE = re.compile(r"^RR-UNPROVISIONED-[0-9A-F]{16}$")

# Placeholder serial for an explicit --port that USB discovery cannot see.
# Destructive commands resolve it against the board before proceeding.
UNKNOWN_SERIAL = "<unknown>"

BOOTSEL_VOLUME_NAME = "RPI-RP2"
BOOTSEL_MARKER = "INFO_UF2.TXT"

# Register map.  `size` is not carried by the legacy framing - caller and
# firmware have to agree, which is why this table is the documented contract.
REGISTERS = {
    0x10: ("READ_ALL", 10),
    0x21: ("READ_MAGNET_STATE", 1),
    0x22: ("READ_FILAMENT_PRESENCE", 1),
    0x23: ("READ_FULL_TURNS", 4),
    0x24: ("READ_ANGLE", 4),
    0x30: ("READ_IDENTITY_STATE", 1),
    0x31: ("READ_SERIAL", 34),
    0x32: ("READ_FIRMWARE_VERSION", 32),
    0x33: ("READ_VARIANT", 2),
    0x34: ("READ_FLASH_UID", 8),
}
SENSOR_REGISTERS = (0x10, 0x21, 0x22, 0x23, 0x24)
IDENTITY_REGISTERS = (0x30, 0x31, 0x32, 0x33, 0x34)

# Exit codes.  Distinct enough that a wrapper script can tell "no board" from
# "the board refused" without scraping stderr.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_BOARD = 3
EXIT_AMBIGUOUS = 4
EXIT_REFUSED = 5
EXIT_PROTOCOL = 6


class RoadrunnerError(Exception):
    """Anything that should end a command without ending the process.

    The menu loop catches these and returns to the prompt.  The oneoff script
    raises SystemExit from the equivalent helpers, which is right for a linear
    run and wrong here - one register timeout would close the tool.
    """

    exit_code = EXIT_ERROR


class ProtocolError(RoadrunnerError):
    exit_code = EXIT_PROTOCOL


class NoBoardError(RoadrunnerError):
    exit_code = EXIT_NO_BOARD


class AmbiguousBoardError(RoadrunnerError):
    exit_code = EXIT_AMBIGUOUS


class RefusedError(RoadrunnerError):
    """The firmware answered, and the answer was no."""

    exit_code = EXIT_REFUSED


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


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


def status_name(status: int) -> str:
    return STATUS_NAMES.get(status, "UNKNOWN")


def send_admin(
    port: str, opcode: int, payload: bytes = b"", timeout: float = 2
) -> tuple[int, bytes]:
    frame = build_frame(opcode, payload)
    try:
        dev = serial.Serial(port, 115200, timeout=timeout)
    except serial.SerialException as exc:
        raise NoBoardError(f"cannot open {port}: {exc}") from exc

    with dev:
        dev.reset_input_buffer()
        time.sleep(0.05)
        dev.reset_input_buffer()  # catch any boot-log bytes that trickled in
        dev.write(frame)
        dev.flush()

        header = dev.read(5)
        if len(header) != 5 or header[:3] != SYNC:
            raise ProtocolError(f"malformed response header: {header.hex(' ')}")
        resp_opcode, length = header[3], header[4]
        if resp_opcode != (opcode | 0x80):
            raise ProtocolError(f"unexpected response opcode: {resp_opcode:#04x}")

        rest = dev.read(length + 1)
        if len(rest) != length + 1:
            raise ProtocolError(f"truncated response body: {rest.hex(' ')}")
        resp_payload, crc = rest[:-1], rest[-1]
        if crc8(header + resp_payload) != crc:
            raise ProtocolError(f"bad response CRC: {(header + rest).hex(' ')}")

        if length < 1:
            raise ProtocolError("response payload missing status byte")
        return resp_payload[0], resp_payload[1:]


def _read_length_prefixed(data: bytes, offset: int) -> tuple[str, int]:
    n = data[offset]
    value = data[offset + 1 : offset + 1 + n].decode("ascii", "replace")
    return value, offset + 1 + n


def read_register(port: str, reg: int, size: int, timeout: float = 2) -> bytes:
    """One legacy 0xf5-framed register read, returning just the payload.

    The reply is 0x05 0xff <reg> followed by `size` bytes.
    """
    try:
        dev = serial.Serial(port, 115200, timeout=0.2)
    except serial.SerialException as exc:
        raise NoBoardError(f"cannot open {port}: {exc}") from exc

    with dev:
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
    raise ProtocolError(
        f"no complete reply for register {reg:#04x}; received: {received.hex(' ')}"
    )


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


def info(port: str) -> dict:
    status, payload = send_admin(port, OPCODE_INFO)
    identity_status, transport, led_order = payload[0], payload[1], payload[2]
    offset = 3
    model, offset = _read_length_prefixed(payload, offset)
    fw_version, offset = _read_length_prefixed(payload, offset)
    serial_str, offset = _read_length_prefixed(payload, offset)
    return {
        "provisioned": status == STATUS_OK,
        "status": status,
        "identity_status": identity_status,
        "transport": transport,
        "led_order": led_order,
        "model": model,
        "fw_version": fw_version,
        "serial": serial_str,
        "flash_uid": payload[offset : offset + 8],
    }


def identity_state(port: str) -> int:
    """The board's own answer to "why am I refusing?", read over the gate.

    The identity window stays readable when everything else is locked, which
    is what makes it usable as the explanation for a refusal.
    """
    return read_register(port, 0x30, 1)[0]


def provision(port: str, new_uuid: bytes | None = None) -> str:
    status, payload = send_admin(
        port, OPCODE_PROVISION_UUID, new_uuid or uuid.uuid4().bytes
    )
    if status == STATUS_ALREADY_PROVISIONED:
        raise RefusedError(
            "board already has an identity; clear it first if you really mean to"
        )
    if status != STATUS_OK:
        raise RefusedError(f"PROVISION_UUID failed: {status_name(status)} ({status:#04x})")
    serial_str, _ = _read_length_prefixed(payload, 0)
    return serial_str


def clear_identity(port: str) -> None:
    status, _ = send_admin(port, OPCODE_CLEAR_IDENTITY, CLEAR_CONFIRMATION)
    if status != STATUS_OK:
        message = f"CLEAR_IDENTITY failed: {status_name(status)} ({status:#04x})"
        if status == STATUS_ALREADY_PROVISIONED:
            # rr_identity_clear refuses a conflicted sector, so CLEAR is not a
            # recovery path for one.  BOOTSEL is - it is deliberately ungated.
            message += (
                "\nA conflicted identity sector cannot be erased by CLEAR_IDENTITY. "
                "Recover with `bootsel` and reflash."
            )
        raise RefusedError(message)


def reboot_bootsel(port: str) -> None:
    status, _ = send_admin(port, OPCODE_REBOOT_BOOTSEL)
    if status != STATUS_OK:
        raise RefusedError(
            f"REBOOT_BOOTSEL failed: {status_name(status)} ({status:#04x})"
        )


# --------------------------------------------------------------------------
# Board discovery
# --------------------------------------------------------------------------


def normalise_serial(value: str) -> str:
    """Strip the RR- prefix so the exclusion list can be written either way."""
    return value[3:] if value.startswith("RR-") else value


def roadrunner_ports() -> list[tuple[str, str]]:
    """Every attached Roadrunner as (device, serial).

    Matches the USB descriptor fields rather than a udev by-id name: it works
    on every platform, and it sidesteps the by-id collision two unprovisioned
    boards cause - they share a flash UID, so they share a serial, and only
    one symlink can exist.
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
        found.append((port.device, number))
    return sorted(found)


def select_port(args) -> tuple[str, str]:
    """Resolve --port / --serial / sole attached board to (device, serial).

    --serial is only unambiguous for provisioned boards: two unprovisioned
    boards share a flash UID and therefore a USB serial, so a serial match
    that hits more than one board is refused rather than resolved by picking.
    """
    attached = roadrunner_ports()

    if getattr(args, "port", None):
        for device, number in attached:
            if device == args.port:
                return device, number
        # Honour an explicit --port even when discovery does not see it: a
        # board can be there under a descriptor this tool does not recognise,
        # and the user naming a port is naming it deliberately.
        return args.port, UNKNOWN_SERIAL

    if getattr(args, "serial", None):
        wanted = normalise_serial(args.serial)
        matches = [(d, n) for d, n in attached if normalise_serial(n) == wanted]
        if not matches:
            raise NoBoardError(f"no attached board with serial {args.serial}")
        if len(matches) > 1:
            listing = ", ".join(d for d, _ in matches)
            raise AmbiguousBoardError(
                f"serial {args.serial} matches {len(matches)} boards ({listing}). "
                f"Unprovisioned boards share a flash UID and therefore a serial - "
                f"select one with --port."
            )
        return matches[0]

    if not attached:
        raise NoBoardError(
            f"no Roadrunner found (manufacturer={USB_MANUFACTURER!r}, "
            f"product={USB_PRODUCT!r}, serial starting RR-)"
        )
    if len(attached) > 1:
        listing = "\n".join(f"  {d}  {n}" for d, n in attached)
        raise AmbiguousBoardError(
            f"{len(attached)} Roadrunners attached; name one with --port or "
            f"--serial:\n{listing}"
        )
    return attached[0]


def guard_destructive(device: str, number: str, args) -> str:
    """Name the board out loud, then ask before resetting it.

    Returns the serial it confirmed.  `select_port` honours an explicit --port
    that discovery cannot see, which would otherwise put an unnamed board in
    the confirmation prompt - so if the serial is unknown, ask the board.
    """
    if number == UNKNOWN_SERIAL:
        number = info(device)["serial"]
        print(f"{device} identifies as {number}")

    print(
        f"\nThis resets {number} on {device}.\n"
        f"If the board is wired to Klipper over I2C or UART, that connection\n"
        f"drops and does not come back on its own - there is no serial lock\n"
        f"here to warn you the board is in use. Stop Klipper first if it is."
    )
    if not getattr(args, "yes", False):
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            raise RoadrunnerError("cancelled")
    return number


# --------------------------------------------------------------------------
# Reconnection
# --------------------------------------------------------------------------


def wait_for_disconnect(port: str, timeout: float = 5) -> None:
    time.sleep(1)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Not os.path.exists: a COM port is not a filesystem path on Windows,
        # and a by-id symlink can outlive the device on Linux.
        if port not in [device for device, _ in roadrunner_ports()]:
            return
        time.sleep(0.05)
    raise RoadrunnerError(f"{port} did not disconnect within {timeout}s")


def wait_for_serial_port(
    known_before: set[str] | None = None,
    prefix: str | None = None,
    timeout: float = 10,
    settle: float = 0.3,
    expect_disconnect: bool = False,
) -> str:
    """Wait for a board to come back after a reset.

    With a USB port `prefix` the wait is for that port specifically, which is
    exact: a board plugged in elsewhere while we waited cannot be mistaken for
    ours.  Without one - Windows, or a host with no /dev/serial/by-path - it
    falls back to `known_before`, the devices present beforehand that are not
    the board we reset; a port outside that set is the one that just
    re-enumerated.  Right after a reset the OS can briefly report a stale
    device, so a candidate has to hold still for `settle` seconds.

    `expect_disconnect` says the board is mid-reboot and has not gone away
    yet. Without it, a port still listed from the previous enumeration looks
    like a match, holds still for `settle` because udev has not removed it
    yet, and gets returned - and then the caller opens a handle that vanishes
    underneath it. Waiting for the port to go before waiting for it to come
    back is the difference.
    """
    known_before = known_before or set()

    def candidates() -> list[str]:
        if prefix is not None:
            return [d for d, _ in roadrunner_ports() if port_topology(d) == prefix]
        return [d for d, _ in roadrunner_ports() if d not in known_before]

    deadline = time.monotonic() + timeout
    while expect_disconnect and time.monotonic() < deadline and candidates():
        time.sleep(0.05)

    while time.monotonic() < deadline:
        matches = candidates()
        if len(matches) == 1:
            candidate = matches[0]
            settle_deadline = time.monotonic() + settle
            stable = True
            while time.monotonic() < settle_deadline:
                time.sleep(0.05)
                if candidates() != [candidate]:
                    stable = False
                    break
            if stable:
                return candidate
        elif len(matches) > 1:
            raise AmbiguousBoardError(
                f"multiple new Roadrunner ports appeared: {matches}"
            )
        time.sleep(0.1)
    raise NoBoardError(f"no Roadrunner re-enumerated within {timeout}s")


def _listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def usb_port_prefix(by_path: str) -> str | None:
    """The USB port part of a /dev/*/by-path name, interface segment dropped.

    The two names a board carries either side of a BOOTSEL reboot are not
    string-equal - the config.interface differs, and the block device adds a
    -scsi- suffix on top of that:

        serial  pci-0000:00:14.0-usb-0:1.2:1.0
        block   pci-0000:00:14.0-usb-0:1.2:0.0-scsi-0:0:0:0

    What survives the reboot is the port. It cannot be found by pattern - the
    port itself contains dots ("0:1.2"), so it is indistinguishable from a
    config.interface ("1.0") by shape alone. Position is what separates them:
    the interface is the last colon-separated component of the USB tail. Take
    the tail after the last "-usb-" (so the PCI address's own ":14.0" is out
    of reach), cut any "-scsi-…"/"-part1"/"-port0" suffix, and drop that last
    component.
    """
    marker = by_path.rfind("-usb-")
    if marker < 0:
        return None
    cut = marker + len("-usb-")
    parts = by_path[cut:].split("-", 1)[0].split(":")
    if len(parts) > 1 and re.fullmatch(r"\d+\.\d+", parts[-1]):
        parts.pop()
    tail = ":".join(parts)
    return by_path[:cut] + tail if tail else None


def port_topology(device: str) -> str | None:
    """The USB port prefix serving `device`, from /dev/serial/by-path.

    None off Linux, or when nothing in by-path resolves to `device`. Both are
    ordinary; every caller has a scan to fall back on.
    """
    try:
        entries = list(Path("/dev/serial/by-path").iterdir())
        target = os.path.realpath(device)
    except OSError:
        return None
    for entry in entries:
        try:
            if os.path.realpath(entry) == target:
                return usb_port_prefix(entry.name)
        except OSError:
            continue
    return None


def _mount_points() -> dict[str, Path]:
    """Resolved block device -> mount point, from /proc/mounts."""
    mounts: dict[str, Path] = {}
    try:
        text = Path("/proc/mounts").read_text()
    except OSError:
        return mounts
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2 or not fields[0].startswith("/dev/"):
            continue
        # /proc/mounts octal-escapes the characters that would otherwise break
        # its own field splitting.
        point = (
            fields[1]
            .replace("\\040", " ")
            .replace("\\011", "\t")
            .replace("\\012", "\n")
            .replace("\\134", "\\")
        )
        try:
            mounts[os.path.realpath(fields[0])] = Path(point)
        except OSError:
            continue
    return mounts


def bootsel_block_devices(prefix: str) -> list[str]:
    """Block devices at USB port `prefix`, partitions before whole disks."""
    try:
        entries = sorted(
            Path("/dev/disk/by-path").iterdir(),
            key=lambda entry: ("-part" not in entry.name, entry.name),
        )
    except OSError:
        return []
    found = []
    for entry in entries:
        if usb_port_prefix(entry.name) != prefix:
            continue
        try:
            found.append(os.path.realpath(entry))
        except OSError:
            continue
    return found


def bootsel_volume_at(prefix: str) -> Path | None:
    """The mounted boot ROM volume of the board at USB port `prefix`.

    Follows the block device to wherever it is actually mounted, rather than
    looking in a path this tool predicted. That is now the only thing that
    works: mcu-updater's udev rule mounts each board under its own topology
    path, so the mountpoint is no longer a constant - and guessing it again
    would break the same way the next time a rule changes.
    """
    mounts = _mount_points()
    for device in bootsel_block_devices(prefix):
        point = mounts.get(device)
        if point is not None and (point / BOOTSEL_MARKER).is_file():
            return point
    return None


def _descend(base: Path, depth: int = 3) -> list[Path]:
    """`base` and its subdirectories down to `depth`, skipping unreadable ones.

    Three levels below /media/<user> reaches mcu-updater's
    BOOTSEL/by-path/<tag> layout without walking a whole volume.
    """
    found = [base]
    if depth <= 0:
        return found
    for name in _listdir(str(base)):
        child = base / name
        try:
            if child.is_dir():
                found.extend(_descend(child, depth - 1))
        except OSError:
            continue
    return found


def bootsel_volume() -> Path | None:
    """Any mounted RP2040 boot ROM volume, by scanning the automount roots.

    The fallback for Windows, and for "a board was already sitting in BOOTSEL
    when the tool started" - there is no serial port left to take a topology
    from. Identified by INFO_UF2.TXT, never by volume label or directory name:
    an unrelated drive can carry the label, and under the per-topology mount
    layout the directory is named after the USB port instead.
    """
    if os.name == "nt":
        roots = [Path(f"{letter}:/") for letter in string.ascii_uppercase]
    else:
        roots = [
            root
            for parent in ("/media", "/run/media")
            for user in _listdir(parent)
            for root in _descend(Path(parent) / user)
        ]
    for root in roots:
        try:
            if (root / BOOTSEL_MARKER).is_file():
                return root
        except OSError:
            continue
    return None


def require_writable(volume: Path) -> Path:
    """An unwritable automount is a configuration problem, not a traceback."""
    if not os.access(volume, os.W_OK):
        raise RoadrunnerError(
            f"{volume} is mounted but not writable by this user; "
            f"on Linux check the automount's uid/gid options"
        )
    return volume


def wait_for_bootsel_mount(prefix: str | None = None, timeout: float = 10) -> Path:
    """Wait for the boot ROM volume, following `prefix` when we have one."""
    saw_device = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        volume = bootsel_volume_at(prefix) if prefix else bootsel_volume()
        if volume is not None:
            return require_writable(volume)
        if prefix and not saw_device and bootsel_block_devices(prefix):
            saw_device = True
        time.sleep(0.1)

    if saw_device:
        # Worth separating from "nothing appeared": the board did what it was
        # told and the host did not mount it. Retrying will not help.
        raise NoBoardError(
            f"the board at {prefix} is in BOOTSEL - its block device is there - "
            f"but nothing mounted it within {timeout}s. Install mcu-updater's "
            f"udev rule, or mount the volume by hand."
        )
    raise NoBoardError(
        f"no {BOOTSEL_VOLUME_NAME} volume (a directory containing "
        f"{BOOTSEL_MARKER}) appeared within {timeout}s"
        + (f" for the board at {prefix}" if prefix else "")
    )


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------


def describe_transport(value: int) -> str:
    return TRANSPORT_NAMES.get(value, f"unknown ({value})")


def describe_led_order(value: int) -> str:
    return LED_ORDER_NAMES.get(value, f"unknown ({value})")


def describe_identity_state(value: int) -> str:
    return f"{IDENTITY_STATE_NAMES.get(value, 'UNKNOWN')} ({value:#04x})"


def print_info(result: dict) -> None:
    print(f"serial:      {result['serial']}")
    print(f"provisioned: {result['provisioned']}")
    print(f"identity:    {describe_identity_state(result['identity_status'])}")
    print(f"model:       {result['model']}")
    print(f"firmware:    {result['fw_version']}")
    print(f"transport:   {describe_transport(result['transport'])}")
    print(f"led order:   {describe_led_order(result['led_order'])}")
    print(f"flash uid:   {result['flash_uid'].hex(' ')}   (diagnostic only - not unique)")


def decode_register(reg: int, payload: bytes) -> str:
    if reg == 0x10:
        magnet, filament, turns, angle = struct.unpack("<BBll", payload)
        return f"magnet={magnet} filament={filament} turns={turns} angle={angle}"
    if reg in (0x21, 0x22):
        return str(payload[0])
    if reg in (0x23, 0x24):
        return str(struct.unpack("<l", payload)[0])
    if reg == 0x30:
        return describe_identity_state(payload[0])
    if reg in (0x31, 0x32):
        return payload.split(b"\x00", 1)[0].decode("ascii", "replace")
    if reg == 0x33:
        return f"{describe_transport(payload[0])}, {describe_led_order(payload[1])}"
    return payload.hex(" ")


def dump_registers(port: str, regs) -> None:
    """Read and print registers, saying plainly when a value is a refusal.

    0xff means two different things on this firmware.  A locked board fills
    every sensor register with it; a provisioned board with no magnet reports
    MAGNET_STATE_UNKNOWN (0), never 0xff.  Ask register 0x30 which case this
    is rather than leaving somebody to hunt a wiring fault that is not there.
    """
    state = identity_state(port)
    locked = state != IDENTITY_OK
    if locked:
        print(
            f"identity state is {describe_identity_state(state)} - the gate is "
            f"closed. Sensor registers answer 0xff by design; the identity "
            f"window below is still real.\n"
        )

    sensor_fault = False
    for reg in regs:
        name, size = REGISTERS[reg]
        payload = read_register(port, reg, size)
        refused = locked and reg in SENSOR_REGISTERS and payload == b"\xff" * size
        decoded = "refused (no valid identity)" if refused else decode_register(reg, payload)
        print(f"{reg:#04x}  {name:<22} {payload.hex(' '):<26} {decoded}")
        if not locked and reg in (0x10, 0x21) and payload[0] == 0xFF:
            sensor_fault = True

    if sensor_fault:
        print(
            "\nwarning: the gate is open but magnet state reads 0xff - that is "
            "a genuine sensor fault, not a refusal"
        )


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_list(args) -> int:
    attached = roadrunner_ports()
    if not attached:
        print("no Roadrunner attached")
        volume = bootsel_volume()
        if volume is not None:
            print(f"a board is in BOOTSEL at {volume}")
        return EXIT_NO_BOARD
    for device, number in attached:
        marks = []
        if UNPROVISIONED_RE.match(number):
            marks.append("unprovisioned")
        topology = port_topology(device)
        if topology:
            marks.append(topology)
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        print(f"{device}  {number}{suffix}")
    return EXIT_OK


def cmd_info(args) -> int:
    device, _ = select_port(args)
    print_info(info(device))
    return EXIT_OK


def cmd_identity(args) -> int:
    device, _ = select_port(args)
    dump_registers(device, IDENTITY_REGISTERS)
    return EXIT_OK


def cmd_read(args) -> int:
    regs = []
    for token in args.registers:
        try:
            value = int(token, 0)
        except ValueError:
            raise RoadrunnerError(f"{token!r} is not a register number") from None
        if value not in REGISTERS:
            raise RoadrunnerError(
                f"unknown register {token}; known: "
                + ", ".join(f"{r:#04x}" for r in sorted(REGISTERS))
            )
        regs.append(value)

    device, _ = select_port(args)
    dump_registers(device, regs)
    return EXIT_OK


def cmd_dump(args) -> int:
    device, _ = select_port(args)
    dump_registers(device, sorted(REGISTERS))
    return EXIT_OK


def cmd_provision(args) -> int:
    device, number = select_port(args)
    prefix = port_topology(device)
    others = {d for d, _ in roadrunner_ports() if d != device}
    guard_destructive(device, number, args)

    wanted = getattr(args, "uuid", None)
    new_serial = provision(device, uuid.UUID(wanted).bytes if wanted else None)
    print(f"provisioned as {new_serial}; waiting for the board to come back")

    wait_for_disconnect(device)
    port = wait_for_serial_port(known_before=others, prefix=prefix)
    result = info(port)
    if not result["provisioned"] or result["serial"] != new_serial:
        raise RoadrunnerError(
            f"PROVISION_UUID reported success as {new_serial} but the board "
            f"re-enumerated as {result['serial']!r} "
            f"(provisioned={result['provisioned']}); identity did not persist"
        )
    print(f"confirmed after reset on {port}")
    return EXIT_OK


def cmd_clear(args) -> int:
    device, number = select_port(args)
    prefix = port_topology(device)
    others = {d for d, _ in roadrunner_ports() if d != device}
    guard_destructive(device, number, args)

    clear_identity(device)
    print("identity cleared; waiting for the board to come back")

    wait_for_disconnect(device)
    port = wait_for_serial_port(known_before=others, prefix=prefix)
    result = info(port)
    if result["provisioned"]:
        raise RoadrunnerError(
            f"CLEAR_IDENTITY reported OK but the board is still provisioned as "
            f"{result['serial']} after reset"
        )
    print(f"confirmed unprovisioned after reset on {port}: {result['serial']}")
    return EXIT_OK


def cmd_bootsel(args) -> int:
    device, number = select_port(args)
    prefix = port_topology(device)
    guard_destructive(device, number, args)

    reboot_bootsel(device)
    volume = wait_for_bootsel_mount(prefix)
    print(f"BOOTSEL volume at {volume}")
    if prefix is None:
        print(
            "note: found by scanning, not by USB port - this is the right "
            "volume only if no other board is in BOOTSEL"
        )
    return EXIT_OK


def cmd_flash(args) -> int:
    uf2 = Path(args.uf2)
    if not uf2.is_file():
        raise RoadrunnerError(f"{uf2} does not exist")
    if uf2.suffix.lower() != ".uf2":
        raise RoadrunnerError(f"{uf2} is not a .uf2 image")

    # A board named on the command line wins over one already sitting in
    # BOOTSEL: naming it is the whole point, and routing it there ourselves is
    # the only way to know which volume is which afterwards.
    named = bool(getattr(args, "port", None) or getattr(args, "serial", None))
    volume = None if named else bootsel_volume()

    others: set[str] = set()
    prefix: str | None = None
    if volume is None:
        device, number = select_port(args)
        prefix = port_topology(device)
        others = {d for d, _ in roadrunner_ports() if d != device}
        guard_destructive(device, number, args)
        reboot_bootsel(device)
        volume = wait_for_bootsel_mount(prefix)
    else:
        require_writable(volume)
        print(f"a board is already in BOOTSEL at {volume}")
        print(
            "note: found by scanning, so this is the right volume only if no "
            "other board is in BOOTSEL"
        )
        if not args.yes:
            if input("Flash it? [y/N] ").strip().lower() not in ("y", "yes"):
                raise RoadrunnerError("cancelled")

    shutil.copy(uf2, volume / uf2.name)
    print(f"copied {uf2.name}; waiting for the board to come back")

    # A normal UF2 update preserves the identity sector, so a provisioned
    # board stays provisioned and an erased sector stays erased.
    port = wait_for_serial_port(
        known_before=others, prefix=prefix, timeout=20, expect_disconnect=True
    )
    print_info(info(port))
    return EXIT_OK


# --------------------------------------------------------------------------
# Menu
# --------------------------------------------------------------------------

MENU = (
    ("list attached boards", cmd_list),
    ("info", cmd_info),
    ("identity registers", cmd_identity),
    ("dump all registers", cmd_dump),
    ("provision (resets the board)", cmd_provision),
    ("clear identity (resets the board)", cmd_clear),
    ("reboot to BOOTSEL (resets the board)", cmd_bootsel),
)


def run_menu(args) -> int:
    while True:
        print("\nRoadrunner admin")
        for index, (label, _) in enumerate(MENU, 1):
            print(f"  {index}. {label}")
        print("  q. quit")

        try:
            choice = input("> ").strip().lower()
        except EOFError:
            return EXIT_OK
        if choice in ("q", "quit", "exit", ""):
            return EXIT_OK
        if not choice.isdigit() or not 1 <= int(choice) <= len(MENU):
            print("not a choice")
            continue

        _, handler = MENU[int(choice) - 1]
        try:
            handler(args)
        except RoadrunnerError as exc:
            print(f"error: {exc}")
        except KeyboardInterrupt:
            print("\ninterrupted")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    assert __doc__ is not None
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run with no subcommand for an interactive menu.",
    )
    parser.add_argument("--port", help="serial device to use, e.g. COM7 or /dev/ttyACM0")
    parser.add_argument("--serial", help="board serial, with or without the RR- prefix")
    parser.add_argument(
        "-y", "--yes", action="store_true", help="skip confirmation prompts"
    )

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="list attached Roadrunners").set_defaults(func=cmd_list)
    sub.add_parser("info", help="USB admin INFO").set_defaults(func=cmd_info)
    sub.add_parser(
        "identity", help="read the identity register window"
    ).set_defaults(func=cmd_identity)
    sub.add_parser("dump", help="read every known register").set_defaults(func=cmd_dump)

    read = sub.add_parser("read", help="read named registers")
    read.add_argument("registers", nargs="+", help="register numbers, e.g. 0x10 0x30")
    read.set_defaults(func=cmd_read)

    prov = sub.add_parser("provision", help="assign an identity (resets the board)")
    prov.add_argument("--uuid", help="use this UUID instead of a random one")
    prov.set_defaults(func=cmd_provision)

    sub.add_parser(
        "clear", help="erase the identity (resets the board)"
    ).set_defaults(func=cmd_clear)
    sub.add_parser(
        "bootsel", help="reboot into BOOTSEL (resets the board)"
    ).set_defaults(func=cmd_bootsel)

    flash = sub.add_parser("flash", help="reboot to BOOTSEL and copy a UF2")
    flash.add_argument("uf2", help="path to the .uf2 image")
    flash.set_defaults(func=cmd_flash)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if getattr(args, "func", None) is None:
            return run_menu(args)
        return args.func(args)
    except RoadrunnerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
