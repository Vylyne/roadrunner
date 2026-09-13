"""Regression coverage for the Klippy extra's transport error boundary."""

from __future__ import annotations

import importlib.util
import logging
import struct
import sys
import types
from pathlib import Path


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
        reg = module.IdentityRegister
        assert reader.read_reg(reg.SERIAL, reg.SIZES[reg.SERIAL]) is None

    assert uart.reads == []
    assert any("tmcuart buffer" in message for message in caplog.messages)

    identity = reader.read_identity()
    assert [reg for reg, _ in uart.reads] == [
        module.IdentityRegister.STATE,
        module.IdentityRegister.VARIANT,
    ]
    assert identity.serial is None
    assert identity.firmware_version is None


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


def test_uart_reads_the_digest_but_never_the_range(monkeypatch):
    """Four bytes fit the tmcuart buffer; the eight-byte range register does not.

    Asking for the range over UART is not a failed read - it is
    shutdown("tmcuart data too large"), which takes the printer down.
    """
    module = _load_extra(monkeypatch)

    class Uart:
        def __init__(self):
            self.reads = []

        def reg_read(self, _instance_id, addr, reg, reg_length=4):
            self.reads.append((reg, reg_length))
            return struct.pack("<L", 0xBBE38AA9)

    uart = Uart()
    reader = module.RegisterReaderUART(uart)
    image = reader.read_firmware_image()

    assert [reg for reg, _ in uart.reads] == [module.IdentityRegister.IMAGE_DIGEST]
    assert image.digest == 0xBBE38AA9
    assert image.start is None
    assert image.length is None
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

