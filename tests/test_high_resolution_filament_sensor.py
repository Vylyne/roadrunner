"""Regression coverage for the Klippy extra's transport error boundary."""

from __future__ import annotations

import importlib.util
import logging
import struct
import sys
import types
from pathlib import Path

import pytest


EXTRA = (
    Path(__file__).resolve().parents[1]
    / "klippy"
    / "extras"
    / "high_resolution_filament_sensor.py"
)


def _load_extra(monkeypatch):
    """Load the extra with only the Klippy interfaces this test needs."""
    package = "roadrunner_test_extras"
    extras = types.ModuleType(package)
    extras.__path__ = []
    monkeypatch.setitem(sys.modules, package, extras)

    for name, module in {
        "bus": types.ModuleType(f"{package}.bus"),
        "filament_switch_sensor": types.ModuleType(
            f"{package}.filament_switch_sensor"
        ),
        "tmc_uart": types.ModuleType(f"{package}.tmc_uart"),
        "high_resolution_filament_sensor_calibration": types.ModuleType(
            f"{package}.high_resolution_filament_sensor_calibration"
        ),
        "serialhdl": types.ModuleType("serialhdl"),
        "serial": types.ModuleType("serial"),
    }.items():
        monkeypatch.setitem(sys.modules, module.__name__, module)
        if name != "serialhdl" and name != "serial":
            monkeypatch.setitem(sys.modules, f"{package}.{name}", module)

    sys.modules[f"{package}.filament_switch_sensor"].RunoutHelper = object
    sys.modules[f"{package}.tmc_uart"].MCU_TMC_uart_bitbang = object
    sys.modules["serialhdl"].error = RuntimeError
    sys.modules["serial"].SerialException = RuntimeError
    sys.modules["serial"].Serial = object

    spec = importlib.util.spec_from_file_location(f"{package}.sensor", EXTRA)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module.__name__] = module
    spec.loader.exec_module(module)
    return module


def test_i2c_read_failure_names_sensor_mcu_and_bus(monkeypatch, caplog):
    """A transport failure must not be masked by the reader's logging path."""
    module = _load_extra(monkeypatch)

    class I2CFault(Exception):
        pass

    class Printer:
        command_error = I2CFault

    class MCU:
        def get_name(self):
            return "toolhead"

        def get_printer(self):
            return Printer()

    class FailingI2C:
        bus = "i2c1"

        def get_mcu(self):
            return MCU()

        def i2c_read(self, _register, _length):
            raise I2CFault("timeout")

    module.serialhdl.error = I2CFault
    reader = module.RegisterReaderI2C(FailingI2C(), "roadrunner")

    with caplog.at_level(logging.WARNING):
        assert reader.i2c_read_reg(module.SensorRegister.ALL, 10) is None

    assert (
        "roadrunner: Unable to read via I2C (MCU 'toolhead', bus 'i2c1'): timeout"
        in caplog.messages
    )


class RecordingReader:
    """A transport that answers from a fixed register map and counts reads."""

    def __init__(self, module, payloads):
        self.module = module
        self.payloads = payloads
        self.reads = []

    def read_reg(self, reg, length):
        self.reads.append((reg, length))
        data = self.payloads.get(reg)
        if data is None:
            return None
        return bytearray(data)


def _identity_payloads(module, state=1, serial=b"RR-0001", version=b"v1.2.3",
                       transport=3, led_order=2, digest=0xBBE38AA9,
                       image_start=0x10000000, image_length=600):
    reg = module.IdentityRegister
    payloads = {
        reg.STATE: bytes([state]),
        reg.SERIAL: serial.ljust(reg.SIZES[reg.SERIAL], b"\x00"),
        reg.FIRMWARE_VERSION: version.ljust(reg.SIZES[reg.FIRMWARE_VERSION], b"\x00"),
        reg.VARIANT: bytes([transport, led_order]),
    }
    if digest is not None:
        payloads[reg.IMAGE_DIGEST] = struct.pack("<L", digest)
        payloads[reg.IMAGE_RANGE] = struct.pack("<LL", image_start, image_length)
    return payloads


def test_identity_read_strips_nul_padding_and_decodes_variant(monkeypatch):
    """The registers are fixed-width and NUL-padded; the status is not."""
    module = _load_extra(monkeypatch)

    reader = RecordingReader(module, _identity_payloads(module))
    reader.identity_registers = module.RegisterReaderGeneric.identity_registers
    identity = module.RegisterReaderGeneric.read_identity(reader)

    assert identity.provisioned
    assert identity.get_status() == {
        "provisioned": True,
        "state": "ok",
        "serial": "RR-0001",
        "firmware_version": "v1.2.3",
        "transport": "usb",
        "led_order": "grb",
    }


def test_identity_read_without_state_is_no_identity_at_all(monkeypatch):
    """State is the anchor: with it missing there is nothing to report."""
    module = _load_extra(monkeypatch)

    payloads = _identity_payloads(module)
    del payloads[module.IdentityRegister.STATE]
    reader = RecordingReader(module, payloads)
    reader.identity_registers = module.RegisterReaderGeneric.identity_registers

    assert module.RegisterReaderGeneric.read_identity(reader) is None


def test_unreadable_fields_do_not_discard_the_state_that_was_read(monkeypatch):
    """A transport that cannot carry a field reports it empty, not absent."""
    module = _load_extra(monkeypatch)

    payloads = _identity_payloads(module, state=2)
    del payloads[module.IdentityRegister.SERIAL]
    del payloads[module.IdentityRegister.FIRMWARE_VERSION]
    reader = RecordingReader(module, payloads)
    reader.identity_registers = module.RegisterReaderGeneric.identity_registers

    status = module.RegisterReaderGeneric.read_identity(reader).get_status()
    assert status["state"] == "conflict"
    assert status["provisioned"] is False
    assert status["serial"] is None
    assert status["firmware_version"] is None
    assert status["transport"] == "usb"


def test_unknown_status_has_the_same_keys_as_a_real_one(monkeypatch):
    """Moonraker subscribers key off the shape - a late key is never seen."""
    module = _load_extra(monkeypatch)

    reader = RecordingReader(module, _identity_payloads(module))
    reader.identity_registers = module.RegisterReaderGeneric.identity_registers
    real = module.RegisterReaderGeneric.read_identity(reader).get_status()

    assert module.SensorIdentity.unknown_status().keys() == real.keys()
    assert module.SensorIdentity.unknown_status()["state"] == "unknown"


def test_uart_never_asks_for_a_register_that_would_shut_down_the_mcu(monkeypatch, caplog):
    """Klipper's tmcuart buffer is 10 bytes and an over-long read is a
    shutdown, not a failed read. The reader must not send one."""
    module = _load_extra(monkeypatch)

    class Uart:
        def __init__(self):
            self.reads = []

        def reg_read(self, _instance_id, addr, reg, reg_length=4):
            self.reads.append((reg, reg_length))
            return bytearray([1] * reg_length)

    uart = Uart()
    reader = module.RegisterReaderUART(uart)

    with caplog.at_level(logging.WARNING):
        # 0x34 READ_FLASH_UID is eight bytes and has no chunk run.
        assert reader.read_reg(0x34, 8) is None

    assert uart.reads == []
    assert any("tmcuart buffer" in message for message in caplog.messages)


# docs/roadrunner-uart-chunked-identity-design.md, "Register allocation". The
# firmware pins the same numbers in rp2040/tests/test_identity_registers.c.
CHUNK_BASES = {0x31: 0x37, 0x32: 0x40, 0x36: 0x48}


class ChunkedUart:
    """A tmcuart that answers the way the firmware does: four bytes a register,
    with the wide fields served as stateless chunks."""

    def __init__(self, module, payloads, fail=()):
        self.registers = {}
        for reg, data in payloads.items():
            if len(data) <= 4:
                self.registers[reg] = bytes(data)
            if reg in CHUNK_BASES:
                for index in range(len(data) // 4):
                    self.registers[CHUNK_BASES[reg] + index] = \
                        bytes(data[index * 4:index * 4 + 4])
        self.fail = set(fail)
        self.reads = []

    def reg_read(self, _instance_id, addr, reg, reg_length=4):
        self.reads.append((reg, reg_length))
        assert reg_length <= 4, "an over-long tmcuart read shuts the MCU down"
        if reg in self.fail or reg not in self.registers:
            return None
        return bytearray(self.registers[reg][:reg_length])


def _uart_reader(module, payloads, fail=(), budget=None):
    uart = ChunkedUart(module, payloads, fail)
    reader = module.RegisterReaderUART(uart)
    if budget is not None:
        reader.CHUNKS_PER_ATTEMPT = budget
    return reader, uart


def _chunk_reads(uart, base, count):
    return [reg for reg, _ in uart.reads if base <= reg < base + count]


def test_uart_reassembles_the_serial_and_version_from_chunks(monkeypatch):
    module = _load_extra(monkeypatch)
    serial = b"RR-7HZHY879879X19ZQZTJYQ7DDRB"
    reader, uart = _uart_reader(
        module, _identity_payloads(module, serial=serial), budget=100)

    identity = reader.read_identity()

    assert identity.serial == serial.decode()
    assert identity.firmware_version == "v1.2.3"
    assert identity.get_status()["transport"] == "usb"
    # The 29-character serial needs all eight chunks; "v1.2.3" plus its
    # terminator needs two.
    assert _chunk_reads(uart, 0x37, 8) == list(range(0x37, 0x3f))
    assert _chunk_reads(uart, 0x40, 8) == [0x40, 0x41]


def test_uart_stops_at_the_chunk_holding_the_terminator(monkeypatch):
    """RR-UNPROVISIONED is sixteen characters, so its NUL is in chunk 4."""
    module = _load_extra(monkeypatch)
    reader, uart = _uart_reader(
        module, _identity_payloads(module, state=0, serial=b"RR-UNPROVISIONED"),
        budget=100)

    assert reader.read_identity().serial == "RR-UNPROVISIONED"
    assert _chunk_reads(uart, 0x37, 8) == [0x37, 0x38, 0x39, 0x3a, 0x3b]


def test_uart_reads_the_image_range_in_full_even_with_zero_bytes(monkeypatch):
    """The range is binary: a zero start is a value, not a terminator."""
    module = _load_extra(monkeypatch)
    reader, uart = _uart_reader(
        module, _identity_payloads(module, image_start=0, image_length=600),
        budget=100)

    image = reader.read_firmware_image()

    assert image.digest == 0xBBE38AA9
    assert (image.start, image.length) == (0, 600)
    assert _chunk_reads(uart, 0x48, 2) == [0x48, 0x49]


def test_uart_loses_a_field_whose_chunk_fails_but_keeps_the_rest(monkeypatch):
    """Never a truncated serial - and never the state thrown away for it."""
    module = _load_extra(monkeypatch)
    serial = b"RR-7HZHY879879X19ZQZTJYQ7DDRB"
    reader, uart = _uart_reader(
        module, _identity_payloads(module, serial=serial), fail={0x3a},
        budget=100)

    identity = reader.read_identity()

    assert identity.serial is None
    assert identity.firmware_version == "v1.2.3"
    assert identity.provisioned
    # Nothing past the failed chunk is asked for.
    assert not [reg for reg in _chunk_reads(uart, 0x37, 8) if reg > 0x3a]


def test_uart_spreads_the_identity_over_polls_and_converges(monkeypatch):
    module = _load_extra(monkeypatch)
    serial = b"RR-7HZHY879879X19ZQZTJYQ7DDRB"
    reader, uart = _uart_reader(module, _identity_payloads(module, serial=serial))
    sensor = _bare_sensor(module, reader)
    assert module.RegisterReaderUART.CHUNKS_PER_ATTEMPT == 6

    for tick in range(20):
        sensor._update_identity(tick * 0.1)
        if sensor._identity is not None and sensor._firmware_image is not None:
            break

    assert sensor._identity.serial == serial.decode()
    assert sensor._identity.firmware_version == "v1.2.3"
    assert (sensor._firmware_image.start, sensor._firmware_image.length) == \
        (0x10000000, 600)
    # Ten identity chunks and two range chunks, each read exactly once.
    chunks = [reg for reg, _ in uart.reads if 0x37 <= reg <= 0x49]
    assert sorted(chunks) == sorted(set(chunks))
    assert len(chunks) == 12
    # Six identity chunks on the first poll; the last four on the second, and
    # the image read gets its own budget on that same poll.
    assert tick == 1
    assert reader._chunks == {}


def test_a_failed_chunk_is_not_retried_by_every_pending_attempt(monkeypatch):
    """Otherwise a board that always fails one chunk never finishes the rest."""
    module = _load_extra(monkeypatch)
    serial = b"RR-7HZHY879879X19ZQZTJYQ7DDRB"
    reader, uart = _uart_reader(
        module, _identity_payloads(module, serial=serial), fail={0x3b}, budget=3)
    sensor = _bare_sensor(module, reader)

    for tick in range(20):
        sensor._update_identity(tick * 0.1)
        if sensor._identity is not None:
            break

    assert sensor._identity.serial is None
    assert sensor._identity.firmware_version == "v1.2.3"
    assert uart.reads.count((0x3b, 4)) == 5  # one read, its own retries


def test_a_pending_read_waits_for_the_next_poll_not_the_retry_timer(monkeypatch):
    module = _load_extra(monkeypatch)
    reader, uart = _uart_reader(module, _identity_payloads(module), budget=1)
    sensor = _bare_sensor(module, reader)

    sensor._update_identity(0.)
    assert sensor._identity is None
    reads = len(uart.reads)

    sensor._update_identity(0.1)
    assert len(uart.reads) > reads


def test_an_unprovisioned_board_is_reported_once_across_pending_polls(monkeypatch):
    module = _load_extra(monkeypatch)
    reader, uart = _uart_reader(
        module, _identity_payloads(module, state=0), budget=1)
    sensor = _bare_sensor(module, reader)

    for tick in range(40):
        sensor._update_identity(tick * 0.1)

    assert sensor._firmware_image is not None
    assert len(sensor.errors) == 1
    assert "not provisioned" in sensor.errors[0]


def test_a_reconnect_drops_half_read_chunks(monkeypatch):
    """The board that came back may carry a different serial."""
    module = _load_extra(monkeypatch)
    reader, uart = _uart_reader(module, _identity_payloads(module), budget=1)
    sensor = _bare_sensor(module, reader)
    sensor._respond_info = lambda msg, log=False: None

    assert reader.read_identity() is module.READ_PENDING
    assert reader._chunks

    sensor._sensor_connected_changed(False, True, 0.)

    assert reader._chunks == {}


def test_serial_reader_frames_a_register_longer_than_read_all(monkeypatch):
    """The framing carries no length, so the caller's size has to drive it."""
    module = _load_extra(monkeypatch)

    reg = module.IdentityRegister
    payload = b"RR-0002".ljust(reg.SIZES[reg.SERIAL], b"\x00")

    class FakeSerial:
        def __init__(self):
            # Leading noise the reader has to skip past to find the marker.
            self.pending = bytearray(b"\x00\x01" + bytes([0x05, 0xff, reg.SERIAL]) + payload)
            self.written = bytearray()

        def write(self, data):
            self.written += data
            return len(data)

        def read(self):
            if not self.pending:
                return b""
            byte = self.pending[:1]
            self.pending = self.pending[1:]
            return bytes(byte)

    port = FakeSerial()
    reader = module.RegisterReaderSerial(port)

    assert module.decode_identity_string(
        reader.read_reg(reg.SERIAL, reg.SIZES[reg.SERIAL])) == "RR-0002"
    assert bytes(port.written) == bytes([0xf5, reg.SERIAL])
    assert reader.buffer == b""


def _generic_reader(module, payloads):
    """A RecordingReader wired to the generic reader's own register logic."""

    class Reader(RecordingReader):
        identity_registers = module.RegisterReaderGeneric.identity_registers
        image_registers = module.RegisterReaderGeneric.image_registers
        read_identity = module.RegisterReaderGeneric.read_identity
        read_firmware_image = module.RegisterReaderGeneric.read_firmware_image
        decode_all = module.RegisterReaderGeneric.decode_all

    return Reader(module, payloads)


def _bare_sensor(module, reader):
    """A sensor with only the attributes the identity path touches.

    Constructing the real thing needs a Klipper config, a reactor and a
    printer; none of that is what these assertions are about.
    """
    sensor = object.__new__(module.HighResolutionFilamentSensor)
    sensor.name = "roadrunner"
    sensor.regs = reader
    sensor._identity = None
    sensor._firmware_image = None
    sensor._identity_next_attempt = 0.
    sensor._boot_marker_supported = None
    sensor.errors = []
    sensor._respond_error = sensor.errors.append
    return sensor


def test_identity_is_read_once_not_on_every_sensor_poll(monkeypatch):
    """32 bytes over a clock-stretching I2C bus has no place in a 100ms loop."""
    module = _load_extra(monkeypatch)

    reader = _generic_reader(module, _identity_payloads(module))
    sensor = _bare_sensor(module, reader)

    for tick in range(20):
        sensor._update_identity(tick * 0.1)
    # Well past the retry timer: a cache that is not a cache re-reads here.
    sensor._update_identity(module.IDENTITY_RETRY_TIMEOUT * 10)

    assert [reg for reg, _ in reader.reads] == \
        list(module.RegisterReaderGeneric.identity_registers) \
        + list(module.RegisterReaderGeneric.image_registers)
    assert sensor._identity.serial == "RR-0001"
    assert sensor._firmware_image.digest == 0xBBE38AA9


def test_a_failed_identity_read_retries_on_a_slow_timer(monkeypatch):
    """Retry, but not at the poll rate, and never by raising."""
    module = _load_extra(monkeypatch)

    reader = _generic_reader(module, {})
    sensor = _bare_sensor(module, reader)

    for tick in range(20):
        sensor._update_identity(tick * 0.1)
    assert len(reader.reads) == 1

    sensor._update_identity(module.IDENTITY_RETRY_TIMEOUT + 1.)
    assert len(reader.reads) == 2
    assert sensor._identity is None


def test_an_unprovisioned_board_says_why_instead_of_looking_dead(monkeypatch):
    """The whole point of the window: answering, and refusing on purpose."""
    module = _load_extra(monkeypatch)

    reader = _generic_reader(module, _identity_payloads(module, state=0))
    sensor = _bare_sensor(module, reader)
    sensor._update_identity(0.)

    assert sensor._identity.provisioned is False
    assert sensor._identity.get_status()["state"] == "none"
    assert len(sensor.errors) == 1
    assert "not provisioned" in sensor.errors[0]


def test_a_locked_board_reports_no_sensor_data(monkeypatch):
    """0xff fills every sensor field, and 0xff is not filament presence."""
    module = _load_extra(monkeypatch)

    locked = bytearray([0xff] * 10)
    assert module.RegisterReaderGeneric().decode_all(locked) is None


def test_a_locked_board_explains_itself_instead_of_looking_dead(monkeypatch):
    """The reason this feature exists.

    A board with no valid identity answers every sensor register with 0xff, so
    on its own it is indistinguishable from a board that is broken or
    unplugged. The identity window is readable on that same locked board, so
    the printer object can carry the reason.
    """
    module = _load_extra(monkeypatch)

    class Reader(RecordingReader):
        identity_registers = module.RegisterReaderGeneric.identity_registers
        image_registers = module.RegisterReaderGeneric.image_registers
        read_identity = module.RegisterReaderGeneric.read_identity
        read_firmware_image = module.RegisterReaderGeneric.read_firmware_image
        decode_all = module.RegisterReaderGeneric.decode_all

        def read(self):
            return self.decode_all(bytearray([0xff] * 10))

    class Reactor:
        def monotonic(self):
            return 0.

    class RunoutHelper:
        sensor_enabled = True

    reader = Reader(module, _identity_payloads(module, state=0))
    sensor = _bare_sensor(module, reader)
    sensor.reactor = Reactor()
    sensor.runout_helper = RunoutHelper()
    sensor.serial_port = "/dev/serial/by-id/usb-Roadrunner"
    sensor._device_path = "/dev/ttyACM0"
    sensor._reads_ok = 0
    sensor._reads_failed = 0
    sensor._consecutive_failures = 0
    sensor._resets = 0
    sensor._inspect_commanded_move = lambda eventtime: None
    sensor._sensor_connected = module.TriggerOnChange(None, lambda *a: None)
    sensor._filament_present = module.TriggerOnChange(None, lambda *a: None)
    sensor._magnet_state = module.MagnetState(0xff)
    sensor._underextruding = module.TriggerOnChange(False, lambda *a: None)
    sensor._runout = module.TriggerOnChange(False, lambda *a: None)
    sensor._status_evaluation_move = None
    sensor._is_printing = False
    sensor.position = 0.

    sensor._update_state_from_sensor()

    status = sensor.get_status(0.)
    assert status["sensor_connected"] is False
    assert status["identity"]["state"] == "none"
    assert status["identity"]["provisioned"] is False
    assert status["connection"]["device_path"] == "/dev/ttyACM0"
    assert status["connection"]["consecutive_failures"] == 1


def test_the_digest_is_reported_with_its_algorithm_never_bare(monkeypatch):
    """A bare number is the failure this shape exists to prevent.

    Two sides comparing digests computed by different functions agree on the
    field and disagree on the value forever, and nobody can tell why.
    """
    module = _load_extra(monkeypatch)

    reader = _generic_reader(module, _identity_payloads(module))
    image = module.RegisterReaderGeneric.read_firmware_image(reader)

    assert image.get_status() == {
        "algorithm": "crc32-iso-hdlc",
        "digest": "0xbbe38aa9",
        "start": 0x10000000,
        "length": 600,
    }


def test_the_digest_matches_the_protocol_documents_golden_vector(monkeypatch):
    """The board's number and the host's number come from the same document.

    This is the same vector as tests/test_uf2_image_digest.py, checked from
    the other side: a 600-byte image whose CRC-32/ISO-HDLC the spec fixes at
    0xBBE38AA9. If the two files disagree, one of them has drifted from the
    document rather than from the other.
    """
    module = _load_extra(monkeypatch)

    reader = _generic_reader(module, _identity_payloads(module))
    image = module.RegisterReaderGeneric.read_firmware_image(reader)

    assert image.digest == 0xBBE38AA9
    assert (image.start, image.length) == (0x10000000, 600)


def test_unknown_firmware_image_has_the_same_keys_as_a_real_one(monkeypatch):
    """A key that appears late is a key a Moonraker subscriber never sees."""
    module = _load_extra(monkeypatch)

    reader = _generic_reader(module, _identity_payloads(module))
    real = module.RegisterReaderGeneric.read_firmware_image(reader).get_status()

    assert module.FirmwareImage.unknown_status().keys() == real.keys()
    assert module.FirmwareImage.unknown_status()["digest"] is None
    assert module.FirmwareImage.unknown_status()["algorithm"] is None


def test_a_board_without_a_digest_reports_nothing_rather_than_zero(monkeypatch):
    """0x00000000 is a legal digest, so a missing read must not look like one."""
    module = _load_extra(monkeypatch)

    payloads = _identity_payloads(module)
    del payloads[module.IdentityRegister.IMAGE_DIGEST]
    reader = _generic_reader(module, payloads)

    assert module.RegisterReaderGeneric.read_firmware_image(reader) is None


def test_an_unreadable_range_keeps_the_digest_it_did_read(monkeypatch):
    """The range is best-effort; the digest is the anchor."""
    module = _load_extra(monkeypatch)

    payloads = _identity_payloads(module)
    del payloads[module.IdentityRegister.IMAGE_RANGE]
    reader = _generic_reader(module, payloads)

    image = module.RegisterReaderGeneric.read_firmware_image(reader)
    assert image.digest == 0xBBE38AA9
    assert image.start is None
    assert image.length is None


def test_uart_reads_the_range_through_its_chunks_never_whole(monkeypatch):
    """Asking for the eight-byte range register over UART is not a failed
    read - it is shutdown("tmcuart data too large"), which takes the printer
    down. ChunkedUart asserts on any such read."""
    module = _load_extra(monkeypatch)
    reader, uart = _uart_reader(module, _identity_payloads(module))

    image = reader.read_firmware_image()

    assert [reg for reg, _ in uart.reads] == [
        module.IdentityRegister.IMAGE_DIGEST, 0x48, 0x49]
    assert image.digest == 0xBBE38AA9
    assert (image.start, image.length) == (0x10000000, 600)
    assert all(length <= module.RegisterReaderUART.MAX_REGISTER_LENGTH
               for _, length in uart.reads)


def test_a_failed_digest_read_retries_without_re_reading_the_identity(monkeypatch):
    """Identity is 32 bytes over a slow bus; do not pay for it twice."""
    module = _load_extra(monkeypatch)

    payloads = _identity_payloads(module)
    del payloads[module.IdentityRegister.IMAGE_DIGEST]
    reader = _generic_reader(module, payloads)
    sensor = _bare_sensor(module, reader)

    sensor._update_identity(0.)
    assert sensor._identity is not None
    assert sensor._firmware_image is None

    reader.reads.clear()
    reader.payloads[module.IdentityRegister.IMAGE_DIGEST] = struct.pack("<L", 7)
    sensor._update_identity(module.IDENTITY_RETRY_TIMEOUT + 1.)

    assert [reg for reg, _ in reader.reads] == list(
        module.RegisterReaderGeneric.image_registers)
    assert sensor._firmware_image.digest == 7


def test_a_reconnect_drops_the_cached_digest_with_the_identity(monkeypatch):
    """The board that came back may have been reflashed while it was gone."""
    module = _load_extra(monkeypatch)

    reader = _generic_reader(module, _identity_payloads(module))
    sensor = _bare_sensor(module, reader)
    sensor.infos = []
    sensor._respond_info = lambda msg, log=False: sensor.infos.append(msg)
    reader.discard_partial_reads = lambda: None
    sensor._update_identity(0.)
    assert sensor._firmware_image is not None

    sensor._sensor_connected_changed(False, True, 0.)

    assert sensor._identity is None
    assert sensor._firmware_image is None


def _opener(module):
    """A sensor stripped to just what _open_serial reads."""
    sensor = object.__new__(module.HighResolutionFilamentSensor)
    sensor.name = "roadrunner"
    sensor.serial_port = "/dev/serial/by-id/usb-Roadrunner"
    sensor.baud = 115200
    return sensor


def test_the_serial_port_is_claimed_exclusively(monkeypatch):
    """Klipper must not share the port with a host tool.

    Two processes on one CDC device do not get two streams: writes interleave
    and whichever reads first eats the other's reply, so both sides see
    intermittent garbage instead of an error. TIOCEXCL makes the second
    opener fail with EBUSY, which is a diagnosis rather than a mystery.
    """
    module = _load_extra(monkeypatch)
    calls = []

    class FakeSerial:
        def __init__(self, port, baud, **kwargs):
            calls.append((port, baud, kwargs))

    monkeypatch.setattr(module.serial, "Serial", FakeSerial, raising=False)
    _opener(module)._open_serial()

    assert len(calls) == 1
    port, baud, kwargs = calls[0]
    assert port == "/dev/serial/by-id/usb-Roadrunner"
    assert baud == 115200
    assert kwargs["exclusive"] is True


def test_a_pyserial_without_exclusive_still_connects(monkeypatch, caplog):
    """`exclusive` is posix-only and pyserial 3.3+; older or other platforms
    raise rather than ignore it. Losing the lock is worth reporting, not worth
    refusing to start a printer over."""
    module = _load_extra(monkeypatch)
    calls = []

    class FakeSerial:
        def __init__(self, port, baud, **kwargs):
            if "exclusive" in kwargs:
                raise TypeError("unexpected keyword argument 'exclusive'")
            calls.append(kwargs)

    monkeypatch.setattr(module.serial, "Serial", FakeSerial, raising=False)
    with caplog.at_level(logging.INFO):
        assert _opener(module)._open_serial() is not None

    assert len(calls) == 1
    assert "exclusive" not in calls[0]
    assert any("exclusive access" in message for message in caplog.messages)


# --- Boot marker (register 0x25) -------------------------------------------

DETECTED = 4
UNKNOWN = 0
LOCKED_MARKER = 0xffffffff


class ScriptedBoard:
    """A board replaying one frame per poll.

    A frame is (magnet_state, turns, angle, boot_ms). A frame of None is a
    poll where the board does not answer at all; a boot_ms of None is a poll
    where only the marker read fails.
    """

    def __init__(self, module, frames, marker_supported=True):
        self.module = module
        self.frames = list(frames)
        self.frame = None
        self.marker_supported = marker_supported
        self.marker_reads = 0

    def read(self):
        self.frame = self.frames.pop(0)
        if self.frame is None:
            return None
        magnet, turns, angle, _ = self.frame
        return self.module.SensorRegister(magnet, 1, turns, angle)

    def read_reg(self, reg, length):
        assert (reg, length) == (0x25, 4)
        self.marker_reads += 1
        if not self.marker_supported or self.frame[3] is None:
            return None
        return bytearray(struct.pack("<L", self.frame[3]))


def _polling_sensor(module, board):
    """A sensor that can run the real poll, with position in raw counts."""

    class Reactor:
        def monotonic(self):
            return 0.

    sensor = _bare_sensor(module, board)
    sensor._identity = object()
    sensor._update_identity = lambda eventtime: None
    sensor._inspect_commanded_move = lambda eventtime: None
    sensor.reactor = Reactor()
    sensor.infos = []
    sensor._respond_info = lambda msg, log=False: sensor.infos.append(msg)
    sensor.serial_port = None
    sensor._device_path = None
    sensor._reads_ok = 0
    sensor._reads_failed = 0
    sensor._consecutive_failures = 0
    sensor._resets = 0
    sensor._last_boot_ms = None
    sensor._boot_marker_failures = 0
    sensor._rebase_pending = False
    sensor._rebase_detected_polls = 0
    sensor._position_offset = 0.
    sensor._sensor_connected = module.TriggerOnChange(None, lambda *a: None)
    sensor._filament_present = module.TriggerOnChange(None, lambda *a: None)
    sensor._underextruding = module.TriggerOnChange(False, lambda *a: None)
    sensor._runout = module.TriggerOnChange(False, lambda *a: None)
    sensor._magnet_state = module.MagnetState(0xff)
    sensor._status_evaluation_move = None
    sensor._is_printing = False
    # 12 bits, nothing ignored, and a rotation distance that makes one raw
    # count one millimetre: position is turns * 4095 + angle.
    sensor._rotation_helper = module.SensorRotationHelper(12, 0)
    sensor.rotation_distance = 4095.
    sensor.invert_direction = False
    sensor.position = 0.
    sensor.motion_triggers = 0

    def motion(eventtime, state):
        sensor.motion_triggers += 1

    sensor._motion_callbacks = [motion]
    sensor._motion_callback_state = True
    sensor._commanded_moves = []
    return sensor


def _poll_all(sensor, board):
    positions = []
    while board.frames:
        sensor._update_state_from_sensor()
        positions.append(sensor.position)
    return positions


def test_an_increasing_marker_leaves_position_tracking_untouched(monkeypatch):
    module = _load_extra(monkeypatch)
    board = ScriptedBoard(module, [
        (DETECTED, 0, 100, 1000),
        (DETECTED, 0, 200, 1100),
        (DETECTED, 1, 5, 1200),
        (DETECTED, 1, 50, 1300),
    ])
    sensor = _polling_sensor(module, board)

    assert _poll_all(sensor, board) == pytest.approx([100., 200., 4100., 4145.])
    assert sensor._resets == 0
    assert sensor.infos == []


def test_a_lower_marker_is_a_reset_and_invents_no_distance(monkeypatch):
    """The brownout jump: without this, the first post-reset poll reports a
    distance the size of everything measured so far."""
    module = _load_extra(monkeypatch)
    board = ScriptedBoard(module, [
        (DETECTED, 3, 100, 60000),
        (DETECTED, 3, 200, 60100),
        # The board restarts: a zeroed state until its encoder loop runs.
        (UNKNOWN, 0, 0, 150),
        (UNKNOWN, 0, 0, 250),
        # The first real angle can take the turn count to -1.
        (DETECTED, -1, 3000, 1200),
        (DETECTED, -1, 3000, 1300),
        # Moving again, from wherever the old count left off.
        (DETECTED, -1, 3010, 1400),
        (DETECTED, -1, 3040, 1500),
    ])
    sensor = _polling_sensor(module, board)

    positions = _poll_all(sensor, board)

    before = 3 * 4095. + 200
    assert positions == pytest.approx(
        [3 * 4095. + 100] + [before] * 5 + [before + 10, before + 40])
    assert sensor._resets == 1
    assert sensor._reads_failed == 0
    assert len(sensor.infos) == 1


def test_a_reset_is_counted_in_connection_status_not_as_a_read_failure(monkeypatch):
    module = _load_extra(monkeypatch)
    board = ScriptedBoard(module, [
        (DETECTED, 0, 100, 5000),
        (DETECTED, 0, 100, 10),
    ])
    sensor = _polling_sensor(module, board)

    _poll_all(sensor, board)
    sensor._identity = None  # only the stand-in the poll needed
    sensor.runout_helper = types.SimpleNamespace(sensor_enabled=True)
    connection = sensor.get_status(0.)["connection"]

    assert connection["resets"] == 1
    assert connection["reads_failed"] == 0


def test_the_reset_rebase_emits_no_motion(monkeypatch):
    module = _load_extra(monkeypatch)
    board = ScriptedBoard(module, [
        (DETECTED, 2, 0, 9000),
        (DETECTED, 0, 7, 100),
        (DETECTED, 0, 7, 200),
        (DETECTED, 0, 7, 300),
    ])
    sensor = _polling_sensor(module, board)

    sensor._update_state_from_sensor()
    triggers = sensor.motion_triggers
    _poll_all(sensor, board)

    assert sensor.motion_triggers == triggers
    assert sensor.position == pytest.approx(2 * 4095.)


def test_a_saturated_marker_never_reads_as_a_reset(monkeypatch):
    module = _load_extra(monkeypatch)
    markers = [0xfffffffc, 0xfffffffd, 0xfffffffe, 0xfffffffe, 0xfffffffe]
    board = ScriptedBoard(module, [
        (DETECTED, 0, i, marker) for i, marker in enumerate(markers)
    ])
    sensor = _polling_sensor(module, board)

    assert _poll_all(sensor, board) == pytest.approx([0., 1., 2., 3., 4.])
    assert sensor._resets == 0


def test_the_locked_fill_is_not_a_marker(monkeypatch):
    module = _load_extra(monkeypatch)
    board = ScriptedBoard(module, [
        (DETECTED, 0, 10, 5000),
        (DETECTED, 0, 20, LOCKED_MARKER),
        (DETECTED, 0, 30, 5200),
    ])
    sensor = _polling_sensor(module, board)

    _poll_all(sensor, board)

    assert sensor._resets == 0
    assert sensor._last_boot_ms == 5200


def test_a_failed_marker_read_holds_position_without_failing_the_read(monkeypatch):
    """Once a board has answered 0x25, a reading without it cannot be told
    apart from one taken after a reset."""
    module = _load_extra(monkeypatch)
    board = ScriptedBoard(module, [
        (DETECTED, 0, 10, 5000),
        (DETECTED, 0, 20, None),
        (DETECTED, 0, 30, 5200),
    ])
    sensor = _polling_sensor(module, board)

    assert _poll_all(sensor, board) == pytest.approx([10., 10., 30.])
    assert sensor._reads_failed == 0
    assert sensor._resets == 0


def test_a_power_cycle_is_a_disconnect_then_a_reset(monkeypatch):
    """The marker survives the disconnect, or the reset it exists to catch
    would be forgotten on the way back."""
    module = _load_extra(monkeypatch)
    board = ScriptedBoard(module, [
        (DETECTED, 1, 0, 30000),
        None,
        None,
        (UNKNOWN, 0, 0, 120),
        (DETECTED, 0, 900, 1150),
        (DETECTED, 0, 900, 1250),
        (DETECTED, 0, 905, 1350),
    ])
    sensor = _polling_sensor(module, board)

    positions = _poll_all(sensor, board)

    assert positions == pytest.approx([4095.] * 6 + [4100.])
    assert sensor._resets == 1
    assert sensor._reads_failed == 2


def test_a_reset_drops_the_moves_it_interrupted(monkeypatch):
    """A move measured across a reset is missing whatever moved while the
    board was down, and would read as underextrusion."""
    module = _load_extra(monkeypatch)
    board = ScriptedBoard(module, [
        (DETECTED, 0, 10, 5000),
        (DETECTED, 0, 0, 10),
    ])
    sensor = _polling_sensor(module, board)

    sensor._update_state_from_sensor()
    sensor._commanded_moves = [types.SimpleNamespace(ended=True)]
    sensor._update_state_from_sensor()

    assert sensor._commanded_moves == []


def test_firmware_without_the_marker_is_left_alone(monkeypatch):
    """Released firmware has no 0x25. It keeps reporting position as it always
    has, and is asked for the marker only until it has plainly not got one."""
    module = _load_extra(monkeypatch)
    board = ScriptedBoard(
        module, [(DETECTED, 0, i, None) for i in range(25)],
        marker_supported=False)
    sensor = _polling_sensor(module, board)

    positions = _poll_all(sensor, board)

    assert positions == pytest.approx([float(i) for i in range(25)])
    assert board.marker_reads == module.BOOT_MARKER_GIVE_UP
    assert sensor._boot_marker_supported is False
    assert sensor._resets == 0


def test_a_board_that_has_not_answered_identity_is_not_asked(monkeypatch):
    module = _load_extra(monkeypatch)
    board = ScriptedBoard(module, [(DETECTED, 0, 1, 100)])
    sensor = _polling_sensor(module, board)
    sensor._identity = None

    _poll_all(sensor, board)

    assert board.marker_reads == 0
    assert sensor.position == pytest.approx(1.)

