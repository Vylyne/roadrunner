# High Resolution Filament Sensor
#
# Copyright (C) 2023 Francois Chagnon <fc@francoischagnon.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os
import struct
import logging
import typing
import serialhdl
import serial
from . import bus, filament_switch_sensor, tmc_uart, high_resolution_filament_sensor_calibration as calibration

DEFAULT_I2C_TARGET_ADDR = 0x40
DEFAULT_I2C_SPEED = 100000

CHECK_RUNOUT_TIMEOUT = .100 # read sensor value at this interval

# Seconds between attempts to read the identity window while the read is
# failing. Identity is static between reboots, so the successful read happens
# once; this only paces the retries.
IDENTITY_RETRY_TIMEOUT = 10.

VIRTUAL_MOTION_PREFIX = 'virtual_motion_sensor'
VIRTUAL_SWITCH_PREFIX = 'virtual_switch_sensor'

class MagnetState:
    """" State of the rotary magnet encoder inside the sensor. """
    NOT_DETECTED = 1
    TOO_WEAK = 2
    TOO_STRONG = 3
    DETECTED = 4

    VALUES : dict[int, str] = {
        NOT_DETECTED: "not detected",
        TOO_WEAK: "too weak",
        TOO_STRONG: "too strong",
        DETECTED: "detected",
    }

    def __init__(self, value : int):
        self.value = value

    def __str__(self):
        return MagnetState.VALUES.get(self.value, "unknown")

    def __repr__(self):
        return "%s(value=%s)" % (self.__class__.__name__, repr(self.value))

class IdentityState:
    """ Result of the board's identity load, register 0x30.

    Any value other than OK means the board refuses to serve sensor data - see
    docs/roadrunner-identity-gate-design.md. ALREADY_PROVISIONED is part of the
    enum but is only ever returned by a provisioning attempt, so it is decoded
    here and not acted on. """
    NONE = 0
    OK = 1
    CONFLICT = 2
    ALREADY_PROVISIONED = 3
    IO_ERROR = 4

    VALUES : dict[int, str] = {
        NONE: "none",
        OK: "ok",
        CONFLICT: "conflict",
        ALREADY_PROVISIONED: "already provisioned",
        IO_ERROR: "io error",
    }

    def __init__(self, value : typing.Optional[int]):
        self.value = value

    def __str__(self):
        return IdentityState.VALUES.get(self.value, "unknown")

    def __repr__(self):
        return "%s(value=%s)" % (self.__class__.__name__, repr(self.value))

class IdentityRegister:
    """ Enum of registers carrying the board's identity.

    These are readable whether or not the board is provisioned: they are both
    the unprovisioned allow-list and the steady-state identity source, and one
    definition serves both.

    0x34 READ_FLASH_UID is deliberately absent. The protocol document says
    hosts must not persist it, and anything reported by get_status() is
    persisted - Moonraker caches printer objects and writes them to its logs.
    The admin script reads it for diagnostics instead. """
    STATE = 0x30
    SERIAL = 0x31
    FIRMWARE_VERSION = 0x32
    VARIANT = 0x33

    # The firmware does not carry a length in its reply, so these are the
    # caller's half of an agreement with it. Source of truth is the register
    # table in docs/roadrunner-usb-admin-protocol.md.
    SIZES : dict[int, int] = {
        STATE: 1,
        SERIAL: 34,
        FIRMWARE_VERSION: 32,
        VARIANT: 2,
    }

TRANSPORT_NAMES : dict[int, str] = {1: "i2c", 2: "uart", 3: "usb"}
LED_ORDER_NAMES : dict[int, str] = {1: "rgb", 2: "grb"}

def decode_identity_string(data : typing.Optional[bytearray]) -> typing.Optional[str]:
    """ Decode a NUL-padded ASCII identity register.

    Replaces undecodable bytes rather than raising: this ends up in
    get_status(), and a garbled read must not take the status query down with
    it. """
    if not data:
        return None
    return bytes(data).split(b"\x00", 1)[0].decode("ascii", errors="replace")

class SensorIdentity:
    """ Which board this is, read from the identity register window.

    Static between reboots, so it is read once and cached. `state` is the
    anchor: it is the authoritative explanation for a board that answers but
    refuses to report sensor data. The remaining fields are best-effort,
    because not every transport can carry them - see RegisterReaderUART. """

    def __init__(self, state : typing.Optional[int], serial : typing.Optional[str] = None,
                 firmware_version : typing.Optional[str] = None,
                 transport : typing.Optional[int] = None,
                 led_order : typing.Optional[int] = None):
        self.state = IdentityState(state)
        self.serial = serial
        self.firmware_version = firmware_version
        self.transport = transport
        self.led_order = led_order

    @property
    def provisioned(self) -> bool:
        return self.state.value == IdentityState.OK

    def __repr__(self):
        return "%s(state=%s, serial=%s, firmware_version=%s)" % (
            self.__class__.__name__, str(self.state), repr(self.serial),
            repr(self.firmware_version))

    def get_status(self):
        """ JSON-safe, and the same keys whether or not a field was readable. """
        return {
            "provisioned": self.provisioned,
            "state": str(self.state),
            "serial": self.serial,
            "firmware_version": self.firmware_version,
            "transport": TRANSPORT_NAMES.get(self.transport),
            "led_order": LED_ORDER_NAMES.get(self.led_order),
        }

    @staticmethod
    def unknown_status():
        """ The same shape with nothing filled in, for before the first read.

        Built from an instance rather than written out again, so the two can
        not drift apart: a Moonraker subscriber keys off the shape of the
        object and a key that appears late is a key it never sees. """
        return SensorIdentity(None).get_status()

class SensorRegister:
    """ Enum of registers that can be read from the sensor. """
    ALL = 0x10
    MAGNET_STATE = 0x21
    FILAMENT_PRESENCE = 0x22
    FULL_TURNS = 0x23
    ANGLE = 0x24

    def __init__(self, magnet_state, filament_presence, full_turns, angle):
        self.magnet_state = magnet_state
        self.filament_presence = filament_presence
        self.full_turns = full_turns
        self.angle = angle
        self.connected = not (magnet_state is None or
                                filament_presence is None or
                                full_turns is None or
                                angle is None)

class SensorEvent:
    """ A point-in-time reading from the sensor, which holds calculated
    values as well as additional printer state. """

    def __init__(self, eventtime : float, position : float, distance : float, epos : float):
        # eventtime at which this sensor reading was taken
        self.eventtime = eventtime

        # sensor position
        self.position = position

        # distance since previous reading from the sensor
        self.distance = distance

        # estimated extruder position at this eventtime
        self.epos = epos

    def __repr__(self):
        return "%s(eventtime=%s, position=%s, distance=%s, epos=%s)" % (
            self.__class__.__name__, self.eventtime, self.position, self.distance, self.epos)

class SensorUART(tmc_uart.MCU_TMC_uart_bitbang):
    """ Class for reading from the sensor via Klipper's native TMC uart driver. """

    def _remove_serial_bits(self, data : str) -> bytearray:
        """ Remove serial start and stop bits to a message in a bytearray. """
        mval = pos = 0
        for d in bytearray(data):
            mval |= d << pos
            pos += 8

        pos = 0
        res = bytearray()
        for i in range((len(data) * 8) // 10):
           shift = (i * 10) + 1
           res.append((mval >> shift) & 0xff)
        return res

    def _decode_read(self, reg : int, data : str) -> typing.Optional[bytearray]:
        """ Extract a uart read response message and returns the decoded message.
        Returns None when message cannot be verified. """
        decoded = self._remove_serial_bits(data)
        if len(decoded) < 5:
            return None
        if decoded[-1] != self._calc_crc8(decoded[:-1]):
            return None
        if decoded[0] != 0x05 and decoded[1] != 0xff:
            logging.warning("Received wrong message prefix: %s" % (decoded.hex(), ))
            return None
        if decoded[2] != reg:
            logging.warning("Received response for reg %02x (expected %02x)" % (decoded[2], reg))
            return None
        return decoded[3:-1]

    def reg_read(self, _instance_id, addr : int, reg : int, reg_length : int = 4) -> typing.Optional[bytearray]:
        """ Read a single register and returns a bytearray. """
        msg = self._encode_read(0xf5, addr, reg)
        read_length = (((4 + reg_length) * 10) + 7) // 8
        params = self.tmcuart_send_cmd.send([self.oid, msg, read_length])
        return self._decode_read(reg, params['read'])

class RegisterReaderGeneric:
    # The identity registers this transport can carry. Every transport can
    # read the whole window unless it says otherwise, and one cannot.
    identity_registers = (IdentityRegister.STATE, IdentityRegister.SERIAL,
                          IdentityRegister.FIRMWARE_VERSION, IdentityRegister.VARIANT)

    def __init__(self): pass
    def read(self) -> SensorRegister: raise NotImplementedError('must be implemented in subclass')
    def read_reg(self, reg : int, length : int) -> typing.Optional[bytearray]:
        raise NotImplementedError('must be implemented in subclass')

    def decode_all(self, data : typing.Optional[bytearray]) -> typing.Optional[SensorRegister]:
        """ Decode a READ_ALL payload, shared by the transports that read it
        in one go. """
        if not data or len(data) != 10:
            if data:
                error = "expected 10 bytes but got %d: '%s'" % (len(data), data.hex(), )
            else:
                error = "communication error"
            logging.warning(f"Reading from sensor failed: {error}")
            return

        magnet_state, filament_presence, full_turns, angle = struct.unpack('<BBll', data)
        if magnet_state == 0xff:
            # A board with no valid identity answers every sensor register
            # with 0xff rather than going quiet. Nothing here is real, so
            # report nothing; the sensor consults the identity window for the
            # reason and says so.
            return

        return SensorRegister(magnet_state, filament_presence, full_turns, angle)

    def read_identity(self) -> typing.Optional[SensorIdentity]:
        """ Read the identity window, or None when the board did not answer.

        STATE is the anchor: without it there is nothing to report and the
        caller should try again later. The rest is best-effort - a transport
        that cannot carry a field leaves it None rather than throwing away the
        state it did manage to read. """
        state = self.read_reg(IdentityRegister.STATE,
                              IdentityRegister.SIZES[IdentityRegister.STATE])
        if not state:
            return None

        fields = {}
        for reg in self.identity_registers:
            if reg != IdentityRegister.STATE:
                fields[reg] = self.read_reg(reg, IdentityRegister.SIZES[reg])

        variant = fields.get(IdentityRegister.VARIANT)
        if variant is not None and len(variant) != IdentityRegister.SIZES[IdentityRegister.VARIANT]:
            variant = None

        return SensorIdentity(
            state[0],
            serial=decode_identity_string(fields.get(IdentityRegister.SERIAL)),
            firmware_version=decode_identity_string(fields.get(IdentityRegister.FIRMWARE_VERSION)),
            transport=variant[0] if variant else None,
            led_order=variant[1] if variant else None,
        )

class RegisterReaderUART(RegisterReaderGeneric):
    # Klipper's MCU-side tmcuart buffer is ten bytes (`uint8_t data[10]` in
    # klipper/src/tmcuart.c), and asking for more is not a failed read - it is
    # `shutdown("tmcuart data too large")`, which takes the printer down.
    # reg_read() below asks the MCU for (((4 + reg_length) * 10) + 7) // 8
    # bytes, which reaches exactly 10 at a 4-byte register, so four bytes is a
    # hard ceiling on this transport.
    #
    # That leaves STATE (1) and VARIANT (2) readable. SERIAL (34) and
    # FIRMWARE_VERSION (32) are not, and are reported as None rather than
    # attempted: reading them here would require the firmware to offer the
    # window in four-byte chunks, which it does not.
    MAX_REGISTER_LENGTH = 4
    identity_registers = (IdentityRegister.STATE, IdentityRegister.VARIANT)

    def __init__(self, uart):
        self.uart = uart

    def read_reg(self, reg, length):
        if length > self.MAX_REGISTER_LENGTH:
            logging.warning(
                "Not reading register %02x over uart: it is %d bytes and "
                "Klipper's tmcuart buffer holds %d, which would shut down the "
                "MCU rather than fail the read."
                % (reg, length, self.MAX_REGISTER_LENGTH))
            return
        data = self.uart_read_reg(reg, length)
        if data is not None and len(data) != length:
            logging.warning("Register %02x returned %d bytes, expected %d"
                            % (reg, len(data), length))
            return
        return data

    def read(self):
        magnet_state = self.uart_read_reg1(SensorRegister.MAGNET_STATE)
        filament_presence = self.uart_read_reg1(SensorRegister.FILAMENT_PRESENCE)
        full_turns = self.uart_read_reg4(SensorRegister.FULL_TURNS)
        angle = self.uart_read_reg4(SensorRegister.ANGLE)

        return SensorRegister(magnet_state, filament_presence, full_turns, angle)

    def uart_read_reg(self, reg, length, retries=5):
        for _ in range(retries):
            response = self.uart.reg_read(None, 0, reg, length)
            if response:
                break
        if response is None:
            logging.warning("Error reading from uart, no response received or CRC was invalid.")
        else:
            return response

    def uart_read_reg4(self, reg):
        if data := self.uart_read_reg(reg, 4):
            val, = struct.unpack('<l', data)
            return val

    def uart_read_reg1(self, reg):
        if data := self.uart_read_reg(reg, 1):
            val, = struct.unpack('<B', data)
            return val

class RegisterReaderI2C(RegisterReaderGeneric):
    def __init__(self, i2c, sensor_name):
        self.i2c = i2c
        self.printer = i2c.get_mcu().get_printer()
        self.sensor_name = sensor_name

    def read(self):
        return self.decode_all(self.i2c_read_reg(SensorRegister.ALL, 10))

    def read_reg(self, reg, length):
        data = self.i2c_read_reg(reg, length)
        if data is not None and len(data) != length:
            logging.warning("Register %02x returned %d bytes, expected %d"
                            % (reg, len(data), length))
            return
        return data

    def i2c_read_reg(self, reg, length):
        try:
            params = self.i2c.i2c_read([reg], length)
            return bytearray(params['response'])
        except (serialhdl.error, self.printer.command_error) as e:
            mcu_name = self.i2c.get_mcu().get_name()
            bus_name = self.i2c.bus or "default"
            logging.warning(
                f"{self.sensor_name}: Unable to read via I2C "
                f"(MCU '{mcu_name}', bus '{bus_name}'): {e}"
            )
            return

class RegisterReaderSerial(RegisterReaderGeneric):
    def __init__(self, serial : serial.Serial):
        self.serial = serial
        self.buffer = bytes()

    def read(self):
        return self.decode_all(self.read_reg(SensorRegister.ALL, 10))

    def read_reg(self, reg, length):
        """ Request one register and return its payload.

        The framing does not carry a length: the reply is 0x05 0xff <reg>
        followed by however many bytes the firmware decided the register is,
        so caller and firmware have to agree in advance. That agreement is
        IdentityRegister.SIZES and the register table it points at. """
        try:
            if self.serial.write(bytearray([0xf5, reg])) != 2:
                logging.warning(f"Writing to serial failed")
                return

            while True:
                data = self.serial.read()
                if len(data) == 0:
                    break
                self.buffer += data

            marker = None
            for i in range(len(self.buffer)-1):
                if self.buffer[i] == 0x05 and self.buffer[i+1] == 0xff:
                    marker = i
                    break

            if marker is None:
                logging.warning(f"Failed to find register marker in buffer")
                self.buffer = bytes()
                return

            # Counted from the marker, not from the start of the buffer:
            # anything before the marker is not part of this response.
            if len(self.buffer) < marker + 3 + length:
                logging.warning(f"Not enough bytes read for response")
                return

            if (r := self.buffer[marker+2]) != reg:
                logging.warning(f"Wrong register response ({r!r})")
                self.buffer = self.buffer[marker+1:]
                return

            data = self.buffer[marker+3:marker+3+length]
            self.buffer = self.buffer[marker+3+length:]
            return bytearray(data)
        except serial.SerialException:
            logging.error("Unable to communicate with sensor")
            return

class SensorRotationHelper:
    """ Helper class used to convert raw point-in-time readings from the sensor
    into an angular change since the previous reading. """

    def __init__(self, resolution : int, ignore_bits : int):
        self.resolution = resolution
        self.ignore_bits = ignore_bits

        # Maximum and minimum values that can be obtained from the sensor
        self.angle_max_value = (1 << resolution) - 1
        self.angle_min_value = (1 << ignore_bits)

        # Value used to mask lower bits of the raw angle value read from the printer.
        self.mask = (1 << ignore_bits) - 1

        # The cumulative angle value including all full turns,
        # kept in the sensor's original resolution.
        self._absolute_angular_position = 0

    @property
    def angular_resolution(self):
        """ Returns the resolution given the number of bits the sensor returns. """
        return (self.angle_min_value / float(self.angle_max_value) * 360.)

    def absolute_angular_position(self):
        """ Returns the cumulative number of degrees turned since the sensor was booted up. """
        return ((self._absolute_angular_position & ~self.mask) / float(self.angle_max_value) * 360.)

    def update_raw(self, turns, angle):
        """ Calculate the absolute position from the number of full turns and the relative angle. """
        self._absolute_angular_position = (turns * self.angle_max_value) + angle

class MotionDirection:
    """" Wrapper for the motion direction. """
    def __init__(self, distance : float):
        self.distance = distance

    def __str__(self):
        if not self.distance:
            return "idle"
        elif self.distance > 0:
            return "extruding"
        else:
            return "reversing"

    def __repr__(self):
        return f"{self.__class__.__name__}(distance={self.distance})"

class CommandedMove:
    """ Represents a movement that was commanded to the printer around a given eventtime.
    Also holds the sensor position and the estimated extruder position around this eventtime.

    When the printer is ordered to move its extruder motor by a certain amount, we know
    the commanded (final) position ahead of time before the motor actually starts moving.
    As we read data from the sensor, the measured distance between each sensor reading is
    accumulated until the move is completed.
    """

    def __init__(self, eventtime, pos, last_epos, epos):
        # eventtime when this move was started
        self.eventtime : float = eventtime

        # Sensor position when the move was started
        self.pos : float = pos

        # Last known toolhead position before the move was started.
        # This can be a reading up to `CHECK_RUNOUT_TIME` seconds in the past.
        self.last_epos : float = last_epos

        # The commanded position, i.e. the final position once this
        # move is done, possibly in the future.
        self.epos : float = epos

        # Expected distance travelled once this move is done.
        self.distance : float = epos - last_epos

        # False while the move is happening, True once the printer is stopped or another move starts.
        self.ended : bool = False

        # All `SensorEvent` objects that happened during this move
        self.sensor_events : list[SensorEvent] = []
        self.first_event : typing.Optional[SensorEvent] = None
        self.last_event : typing.Optional[SensorEvent] = None
        self.first_motion_event : typing.Optional[SensorEvent] = None
        self.last_motion_event : typing.Optional[SensorEvent] = None

    def __repr__(self):
        return f"CommandedMove(t={self.eventtime}, pos={self.pos}, last_epos={self.last_epos}, " + \
            f"epos={self.epos}, distance={self.distance}, expected_distance={self.expected_distance}, " + \
            f"measured_distance={self.measured_distance}, ended={self.ended}, " + \
            f"first={self.first_event.epos if self.first_event else None}, last={self.last_event.epos if self.last_event else None}" + \
            ")"

    def add_sensor_event(self, event, capture=False):
        if capture:
            self.sensor_events.insert(0, event)
        if self.first_event is None:
            self.first_event = event
        if event.distance != 0.:
            if self.first_motion_event is None:
                self.first_motion_event = event
            self.last_motion_event = event
        self.last_event = event

    @property
    def last_motion_eventtime(self) -> typing.Optional[float]:
        """ The most recent `eventtime` at which the sensor had a non-zero `distance` reading. """
        if self.last_motion_event:
            return self.last_motion_event.eventtime

    @property
    def first_motion_eventtime(self) -> typing.Optional[float]:
        """ The most recent `eventtime` at which the sensor had a non-zero `distance` reading. """
        if self.first_motion_event:
            return self.first_motion_event.eventtime

    def has_stopped_moving(self, duration=0.1) -> bool:
        if not (e := self.last_motion_eventtime):
            return False
        if self.last_event.distance != 0.:
            return False
        diff = self.last_event.eventtime - e
        return diff >= duration

    @property
    def duration(self) -> typing.Optional[float]:
        """ The actual duration of the move. """
        if e := self.last_motion_eventtime:
            return e - self.first_motion_eventtime

    @property
    def expected_distance(self) -> typing.Optional[float]:
        """ Returns distance that the extruder is expected to have travelled
        between the start of the move and the most recent sensor reading. """
        if self.last_event:
            return self.last_event.epos - self.last_epos

    @property
    def measured_distance(self) -> float:
        """ Returns the difference in sensor position between the first and last event. """
        if self.last_event:
            return self.last_event.position - self.first_event.position
        else:
            return 0.

    @property
    def speed(self) -> typing.Optional[float]:
        """ Returns the average speed during the move (measured distance divided by duration). """
        if d := self.duration:
            return self.measured_distance / d

    @property
    def extrusion_rate(self) -> float:
        """ Returns the extrusion rate, (measured distance over the expected distance). """
        if d := self.expected_distance:
            return self.measured_distance / d
        return 0.0

    @property
    def detected(self) -> bool:
        return self.measured_distance != 0.

    @property
    def direction(self) -> str:
        """ Returns which way the extruder is moving given a positive or negative distance value. """
        return MotionDirection(self.measured_distance)

class ExtruderMove:
    """ A copy of a move in the extruder queue. """

    def __init__(self, eventtime, start_pos, end_pos, accel_t, cruise_t, decel_t, start_v, cruise_v, accel, can_pressure_advance):
        self.eventtime = eventtime
        self.start_pos = start_pos
        self.end_pos = end_pos
        self.distance = abs(end_pos - start_pos)
        self.retract = end_pos - start_pos < 0.
        self.accel_t = accel_t
        self.cruise_t = cruise_t
        self.decel_t = decel_t
        self.move_t = accel_t + cruise_t + decel_t
        self.start_v = start_v
        self.cruise_v = cruise_v
        self.accel = accel
        self.can_pressure_advance = can_pressure_advance

    def __repr__(self):
        return f"ExtruderMove(eventtime={self.eventtime}, start_pos={self.start_pos}, end_pos={self.end_pos}, distance={self.distance}, " \
                f"accel_t={self.accel_t}, cruise_t={self.cruise_t}, decel_t={self.decel_t}, move_t={self.move_t}, " \
                f"start_v={self.start_v}, cruise_v={self.cruise_v}, accel={self.accel}, can_pressure_advance={self.can_pressure_advance!r})"

class ExtruderMoveQueue:
    """ Holds future extruder moves. """

    def __init__(self):
        self.queue : list[ExtruderMove] = []

    def add(self, eventtime, move):
        start_pos = move.start_pos[3]
        end_pos = move.end_pos[3]
        axis_r = move.axes_r[3]
        accel = move.accel * axis_r
        start_v = move.start_v * axis_r
        cruise_v = move.cruise_v * axis_r
        can_pressure_advance = bool(axis_r > 0. and (move.axes_d[0] or move.axes_d[1]))

        move = ExtruderMove(eventtime, start_pos, end_pos, move.accel_t, move.cruise_t, move.decel_t, start_v, cruise_v, accel, can_pressure_advance)
        self.queue.append(move)

    def advance_time(self, now):
        while len(self.queue) > 0:
            if self.queue[0].eventtime > now:
                break
            # event is in the past, remove it
            self.queue.pop(0)

    def find_move_at_time(self, eventtime):
        for move in self.queue:
            if eventtime > move.eventtime:
                return move

class TriggerOnChange:
    """ Calls a function when the value is changed from False to True, only once. """

    def __init__(self, value : typing.Optional[bool], fn):
        self._value : bool = value
        self._changed_time = None
        self._fn = fn

    def set(self, value : bool, eventtime: float):
        if value is not self._value:
            self._fn(self._value, value, eventtime)
            self._changed_time = eventtime
        self._value = value

    def __bool__(self):
        return bool(self._value)
    __nonzero__=__bool__

class VirtualButtonWrapper:
    def __init__(self, printer):
        self.printer = printer
        self.sensors = {}

        # wrapper requirements
        self.pin_list = []

    def register_sensor(self, sensor):
        self.sensors[sensor.name] = sensor

    def register_callback(self, sensor, cb):
        raise NotImplementedError('must be implemented in subclass')

    def setup_buttons(self, pin_params_list, callback):
        for pin_params in pin_params_list:
            sensor_name = pin_params['pin']
            if sensor_name not in self.sensors:
                raise self.printer.config_error(
                        "%s not a high_resolution_filament_sensor object")

            self.pin_list.append(pin_params)

            sensor = self.sensors[sensor_name]
            self.register_callback(sensor, callback)

class VirtualMotionWrapper(VirtualButtonWrapper):
    def register_callback(self, sensor, cb):
        sensor.register_motion_callback(cb)

class VirtualSwitchWrapper(VirtualButtonWrapper):
    def register_callback(self, sensor, cb):
        sensor.register_switch_callback(cb)

class RunoutHelper(filament_switch_sensor.RunoutHelper):
    def __init__(self, config, sensor):
        super().__init__(config)
        self._sensor = sensor

    def cmd_QUERY_FILAMENT_SENSOR(self, gcmd):
        return self._sensor.cmd_QUERY_FILAMENT_SENSOR(gcmd)

class HighResolutionFilamentSensor:
    """ A filament sensor from which we can get extremely accurate position readings. """

    def __init__(self, config):
        self.name = config.get_name().split()[-1]
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.buttons = self.printer.load_object(config, 'buttons')
        self.reactor = self.printer.get_reactor()
        self.runout_helper = RunoutHelper(config, self)
        self.main_mcu = None

        # Configuration
        self.serial_port = config.get("serial", None)
        self.regs : typing.Optional[RegisterReaderGeneric] = None
        if self.serial_port:
            self.baud = config.getint("baud", default=115200)
        elif uart := self._lookup_uart_bitbang(config):
            self.regs = RegisterReaderUART(uart)
        else:
            i2c = bus.MCU_I2C_from_config(config, DEFAULT_I2C_TARGET_ADDR, DEFAULT_I2C_SPEED)
            self.regs = RegisterReaderI2C(i2c, self.name)

        self.extruder_name = config.get('extruder')
        self.invert_direction = config.getboolean('invert_direction', False)
        self.rotation_distance = config.getfloat('rotation_distance', minval=0)
        self.underextrusion_max_rate = config.getfloat('underextrusion_max_rate', minval=0.0, maxval=1.0)
        self.underextrusion_period = config.getfloat('underextrusion_period', minval=0.0)
        self.move_evaluation_distance = config.getfloat('move_evaluation_distance', 3, minval=0.0)
        self.hysteresis_bits = config.getint('hysteresis_bits', 3, minval=0, maxval=12) # ignore lower 3 bits by default

        # Printer state
        self._commanded_moves : list[CommandedMove] = []
        self._extruder_move_queue = ExtruderMoveQueue()
        self._current_extruder_move = None
        self._capture_history : bool = False
        self.position = 0.0
        self._is_printing = False
        self._is_homing = False
        self._unhealthy = TriggerOnChange(False, self._unhealthy_changed)
        self._runout = TriggerOnChange(False, self._runout_changed)
        self._underextrusion_start_time = None
        self._underextruding = TriggerOnChange(False, self._underextruding_changed)
        self._status_evaluation_move = None

        # virtual pins for native klipper module compatibility
        self._motion_callbacks = []
        self._motion_callback_state = True
        self._switch_callbacks = []
        self.setup_buttons(VIRTUAL_MOTION_PREFIX, VirtualMotionWrapper)
        self.setup_buttons(VIRTUAL_SWITCH_PREFIX, VirtualSwitchWrapper)

        # Board identity, read from the sensor once it answers
        self._identity : typing.Optional[SensorIdentity] = None
        self._identity_next_attempt = 0.
        self._device_path : typing.Optional[str] = None
        self._reads_ok = 0
        self._reads_failed = 0
        self._consecutive_failures = 0

        # Internal sensor state
        self._magnet_state = MagnetState(0xff)
        self._sensor_connected = TriggerOnChange(None, self._sensor_connected_changed)
        self._filament_present = TriggerOnChange(None, self._filament_present_changed)
        self._rotation_helper = SensorRotationHelper(12, self.hysteresis_bits) # 12 bits of precision, with configured hysteresis

        self._sensor_update_timer = self.reactor.register_timer(self._sensor_update_event)
        self.printer.register_event_handler('klippy:connect', self._handle_connect)
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        self.printer.register_event_handler('idle_timeout:printing', self._handle_printing)
        self.printer.register_event_handler('idle_timeout:ready', self._handle_not_printing)
        self.printer.register_event_handler('idle_timeout:idle', self._handle_not_printing)
        self.printer.register_event_handler('homing:homing_move_begin', self._handle_homing_begin)
        self.printer.register_event_handler('homing:homing_move_end', self._handle_homing_end)

        calibration.HighResolutionFilamentSensorCalibration(self)

    def cmd_QUERY_FILAMENT_SENSOR(self, gcmd):
        sensor_connected = "connected" if self._sensor_connected else "not connected"
        filament_present = "detected" if self._filament_present else "not detected"
        runout_detected = "detected" if self._runout else "not detected"
        underextruding_detected = "detected" if self._underextruding else "not detected"
        msg = f"Filament Sensor {self.name}:\n"
        msg += f"- sensor {sensor_connected}\n"
        if self._identity is None:
            msg += "- identity not read yet\n"
        else:
            msg += f"- identity {self._identity.state}"
            if self._identity.serial:
                msg += f", serial {self._identity.serial}"
            if self._identity.firmware_version:
                msg += f", firmware {self._identity.firmware_version}"
            msg += "\n"
        msg += f"- filament {filament_present}\n"
        msg += f"- runout {runout_detected}\n"
        msg += f"- underextrusion {underextruding_detected}\n"
        msg += f"- resolution: {self._rotation_helper.resolution} bits (lower {self._rotation_helper.ignore_bits} bits are ignored)\n"
        msg += f"- smallest detectable angular change: {self.detectable_angle_change()} degree\n"
        msg += f"- smallest detectable movement: {self.detectable_distance_change()} mm\n"
        gcmd.respond_info(msg)

    def _handle_connect(self):
        """ Connect the serial port, if necessary. """
        try:
            if self.serial_port:
                ser = serial.Serial(self.serial_port, self.baud, timeout=0.05, write_timeout=0.05)
                self.regs = RegisterReaderSerial(ser)
                # Resolved once, here, because the configured path is usually a
                # /dev/serial/by-id symlink and what a host tool matches against
                # is the device it points at.
                self._device_path = os.path.realpath(self.serial_port)
        except serial.SerialException:
            raise self.printer.config_error(f"{self.name}: Could not connect to {self.serial_port}")

    def setup_buttons(self, prefix, klass):
        """ Register virtual buttons for use with filament_motion_sensor and filament_switch_sensor. """
        wrapper = self.buttons.mcu_buttons.get(prefix)
        if wrapper is None:
            self.buttons.mcu_buttons[prefix] = wrapper = klass(self.printer)

            ppins = self.printer.lookup_object('pins')
            ppins.register_chip(prefix, self)
        wrapper.register_sensor(self)

    def register_motion_callback(self, cb):
        self._motion_callbacks.append(cb)

    def register_switch_callback(self, cb):
        self._switch_callbacks.append(cb)

    def detectable_angle_change(self):
        return self._rotation_helper.angular_resolution

    def detectable_distance_change(self):
        return self._rotation_helper.angular_resolution / 360. * self.rotation_distance

    def _lookup_uart_bitbang(self, config) -> typing.Optional[SensorUART]:
        if not (rx_pin := config.get('uart_rx_pin', None)):
            return
        ppins = config.get_printer().lookup_object("pins")
        rx_pin_params = ppins.lookup_pin(rx_pin, can_pullup=True)
        tx_pin_params = ppins.lookup_pin(config.get('uart_tx_pin'), can_pullup=True)

        if rx_pin_params['chip'] is not tx_pin_params['chip']:
            raise ppins.error("%s uart rx and tx pins must be on the same mcu")

        return SensorUART(rx_pin_params, tx_pin_params, None)

    def _measured_underextrusion_rate(self, move):
        """ Return a measure of how much we are actually extruding
        compared to how much was expected based on the move queue.
        The rate is returned as a value from 0.0 to 1.0, with 0.0 meaning
        the measured rate matches the expected rate and 1.0 meaning
        no extrusion was detected at all but some was expected. """
        return min(1., max(-1., 1. - move.extrusion_rate)) if move else 0.0

    def _combine_moves(self, newer : CommandedMove, older : CommandedMove) -> CommandedMove:
        move = CommandedMove(older.eventtime, older.pos, older.last_epos, newer.epos)
        move.ended = newer.ended
        move.first_event = older.first_event or newer.first_event
        move.first_motion_event = older.first_motion_event or newer.first_motion_event
        move.last_motion_event = newer.last_motion_event or older.last_motion_event
        move.last_event = newer.last_event or older.last_event
        move.sensor_events = newer.sensor_events + older.sensor_events
        return move

    def _combine_all_commanded_moves(self, fn=None):
        move = None

        for _move in self._commanded_moves:
            if not move:
                move = _move
            else:
                move = self._combine_moves(move, _move)
            if fn and fn(move):
                break

        return move

    def _combine_moves_for_distance(self, distance):
        _stop_fn = lambda move: move.expected_distance is not None and move.expected_distance >= distance
        return self._combine_all_commanded_moves(_stop_fn)

    def get_combined_moves(self):
        return self._combine_all_commanded_moves()

    def get_status(self, eventtime):
        move = self._status_evaluation_move
        speed = move.speed if move and not move.ended else None

        identity = self._identity.get_status() if self._identity \
            else SensorIdentity.unknown_status()

        return {
            "enabled": bool(self.runout_helper.sensor_enabled),
            "sensor_connected": bool(self._sensor_connected),
            # Which board this is, and where it is attached. A host tool
            # matches `identity.serial` against a USB device's serial
            # descriptor, and `connection.device_path` against the port it
            # enumerated at.
            "identity": identity,
            "connection": {
                "port": self.serial_port,
                "device_path": self._device_path,
                "reads_ok": self._reads_ok,
                "reads_failed": self._reads_failed,
                "consecutive_failures": self._consecutive_failures,
            },
            "magnet_state": str(self._magnet_state),
            "filament_detected": bool(self._filament_present),
            # "debug": {
            #     "move": repr(move),
            # },
            "motion": {
                "detected": move.detected if move else False,
                "direction": str(move.direction) if move else "idle",
                "commanded_distance": move.distance if move and not move.ended else 0.0,
                "expected_distance": move.expected_distance if move else 0.0,
                "measured_distance": move.measured_distance if move else 0.0,
                "measured_speed": speed or 0.0,
                "measured_volumetric_flow": self.extruder.filament_area * speed if speed else 0.0,
            },
            "underextrusion_rate": self._measured_underextrusion_rate(move) if move and self._is_printing else 0.0,
            "underextrusion_detected": bool(self._underextruding),
            "runout": bool(self._runout),
            "position": self.position,
        }

    def _get_extruder_pos(self, eventtime):
        """ Find the estimated extruder position at the given eventtime. """
        if self.main_mcu:
            print_time = self.main_mcu.estimated_print_time(eventtime)
            return self.extruder.find_past_position(print_time)

    def _capture_extruder_move(self, move_time, move, ea_index):
        clock_time = self.main_mcu.print_time_to_clock(move_time)
        eventtime = self.main_mcu._clocksync.estimate_clock_systime(clock_time)
        self._extruder_move_queue.add(eventtime, move)
        return self.orig_extruder_process_move(move_time, move, ea_index)

    def _inspect_commanded_move(self, eventtime):
        """ Check if the commanded position of the extruder has changed and
        keep track of commanded moves. """

        if self.toolhead.get_extruder() is not self.extruder:
            return

        extruder_move = self._extruder_move_queue.find_move_at_time(eventtime)
        if extruder_move and self._current_extruder_move != extruder_move:
            move = CommandedMove(eventtime, self.position, extruder_move.start_pos, extruder_move.end_pos)
            self._commanded_moves.insert(0, move)
            self._current_extruder_move = extruder_move

        self._extruder_move_queue.advance_time(eventtime)

        while len(self._commanded_moves) > 100:
            self._commanded_moves.pop()

    def _sensor_connected_changed(self, old_value, new_value, eventtime):
        logging.info(f"{self.name}: 'sensor_connected' changed from {old_value} to {new_value}")

        if old_value is None:
            return
        if new_value:
            # The board that came back is not necessarily the board that went
            # away - it may have been swapped, reflashed or provisioned while
            # it was gone. Drop the cached identity and ask again.
            self._identity = None
            self._identity_next_attempt = 0.
        if new_value:
            self._respond_info("Reconnected")
        else:
            self._respond_error("No longer connected or data cannot be read")

    def _filament_present_changed(self, old_value, new_value, eventtime):
        logging.info(f"{self.name}: 'filament_present' changed from {old_value} to {new_value}")

        for cb in self._switch_callbacks:
            cb(eventtime, new_value)

        if old_value is None:
            return
        if new_value:
            self._respond_info("Filament present")
        else:
            self._respond_error("Filament not present")

    def _update_identity(self, eventtime):
        """ Read the board's identity, once, and cache it.

        Identity does not change between reboots, so this has no business in
        the 100ms sensor poll: over I2C the serial register alone is 34
        RD_REQ events with the bus clock-stretching, and over UART it is
        bit-banged. Read it when the board first answers, keep it, and retry
        on a slow timer for as long as the read fails.

        Failure here is never fatal. A board that will not say who it is still
        reports filament, and taking Klippy down over a blank status field
        would be a far worse outcome than the blank field. """
        if self._identity is not None or eventtime < self._identity_next_attempt:
            return

        self._identity_next_attempt = eventtime + IDENTITY_RETRY_TIMEOUT
        try:
            identity = self.regs.read_identity()
        except Exception:
            logging.exception(f"{self.name}: reading the identity registers failed")
            return

        if identity is None:
            return

        self._identity = identity
        logging.info(f"{self.name}: identity {identity!r}")

        if not identity.provisioned:
            # The one thing this whole window exists to make sayable: the
            # board is answering, and it is refusing on purpose.
            self._respond_error(
                f"board is not provisioned (identity {identity.state}), so it "
                f"refuses to report sensor data. Provision it with "
                f"scripts/roadrunner_admin.py over USB.")

    def _update_state_from_sensor(self):
        """ Read data from sensor and sets the internal state to match. """

        eventtime = self.reactor.monotonic()
        self._inspect_commanded_move(eventtime)

        self._update_identity(eventtime)

        regs = self.regs.read()
        if regs and regs.connected:
            self._reads_ok += 1
            self._consecutive_failures = 0
        else:
            self._reads_failed += 1
            self._consecutive_failures += 1
        self._sensor_connected.set(bool(regs and regs.connected), eventtime)
        if not self._sensor_connected:
            return

        self._magnet_state = MagnetState(regs.magnet_state)
        self._filament_present.set(regs.filament_presence == 1, eventtime)

        self._rotation_helper.update_raw(regs.full_turns, regs.angle)

        inv = (-1 if self.invert_direction else 1)
        new_position = self.rotation_distance * (self._rotation_helper.absolute_angular_position() * inv) / 360.
        distance = new_position - self.position
        self.position = new_position

        if distance > 0.:
            # If we moved by any nonzero amount since previous measurement, toggle state and trigger callbacks
            self._motion_callback_state = not self._motion_callback_state
            for cb in self._motion_callbacks:
                cb(eventtime, self._motion_callback_state)

        if len(self._commanded_moves) > 0:
            move = self._commanded_moves[0]
            if not move.ended:
                event = SensorEvent(eventtime, self.position, distance, self._get_extruder_pos(eventtime))
                move.add_sensor_event(event, self._capture_history)

    def _handle_ready(self):
        """ Callback when printer becomes ready. """

        logging.info("[%s] ready" % (self.__class__.__name__, ))

        self.toolhead = self.printer.lookup_object('toolhead')
        self.extruder = self.printer.lookup_object(self.extruder_name)
        self.main_mcu = self.printer.lookup_object('mcu')

        self.orig_extruder_process_move = self.extruder.process_move
        self.extruder.process_move = self._capture_extruder_move

        self.reactor.update_timer(self._sensor_update_timer, self.reactor.NOW)

    def _handle_homing_begin(self, hmove):
        self._is_homing = True
        logging.info("[%s] homing begin (no sensor update)" % (self.__class__.__name__, ))

    def _handle_homing_end(self, hmove):
        self._is_homing = False
        logging.info("[%s] homing end (sensor update resumed)" % (self.__class__.__name__, ))

    def _handle_printing(self, print_time):
        """ Callback when printing starts. """

        logging.info("[%s] printing" % (self.__class__.__name__, ))
        eventtime = self.main_mcu.print_time_to_clock(print_time)
        self._runout.set(False, eventtime)
        self._underextrusion_start_time = None
        self._underextruding.set(False, eventtime)
        self._is_printing = True
        self.clear_move_queue()
        self._status_evaluation_move = None

    def _handle_not_printing(self, print_time):
        """ Callback when printing is finished. """

        logging.info("[%s] not printing" % (self.__class__.__name__, ))
        self._is_printing = False
        self.all_moves_ended()

    def _respond_error(self, msg):
        """ Print and error to the gcode console. """
        msg = f"{self.name}: {msg}"
        logging.warning(msg)
        lines = msg.strip().split('\n')
        if len(lines) > 1:
            self.gcode.respond_info("\n".join(lines), log=False)
        self.gcode.respond_raw('!! %s' % (lines[0].strip(),))

    def _respond_info(self, msg, log=False):
        self.gcode.respond_info(f"{self.name}: {msg}", log)

    def _exec_gcode(self, prefix, template):
        """ Execute the given gcode with error handling. """
        try:
            self.gcode.run_script(prefix + template.render() + "\nM400")
        except Exception:
            logging.exception("Script running error")
        self.runout_helper.min_event_systime = self.reactor.monotonic() + self.runout_helper.event_delay

    def _runout_event_handler(self, eventtime):
        """ Call the runout code and optionally pause the print. """
        pause_prefix = ""
        if self.runout_helper.runout_pause:
            # Pausing from inside an event requires that the pause portion
            # of pause_resume execute immediately.
            pause_resume = self.printer.lookup_object('pause_resume')
            pause_resume.send_pause_command()
            pause_prefix = "PAUSE\n"
            self.printer.get_reactor().pause(eventtime + self.runout_helper.pause_delay)
        self._exec_gcode(pause_prefix, self.runout_helper.runout_gcode)

    def _is_sensor_healthy(self):
        """ The sensor is 'unhealthy' when it stops responding, or when the magnet
        is too far from the magnetic rotary encoder."""
        return self._sensor_connected and \
            self._magnet_state.value == MagnetState.DETECTED

    def _sensor_unhealthy_reason(self) -> str:
        """ Returns a reason for the sensor being unhealthy for displaying in error messages. """
        if not self._sensor_connected:
            return "no data from sensor"
        if self._magnet_state.value != MagnetState.DETECTED:
            return "magnet %s" % (str(self._magnet_state), )
        return "unknown reason"

    def _is_runout_condition(self, eventtime):
        """ Checks whether there is a runout condition, either immediately when
        the filament is not detected, or after the configured period of time when
        underextruding. """

        if not self._filament_present:
            return True

        rate = self._measured_underextrusion_rate(self._status_evaluation_move)
        if rate > self.underextrusion_max_rate:
            if self._underextrusion_start_time is None:
                self._underextrusion_start_time = self.reactor.monotonic()
                # self._respond_error("Detected %.2f%% underextrusion starting at %.2f" %
                #                     (rate * 100, self._underextrusion_start_time))
                return False
            elif self._underextrusion_start_time + self.underextrusion_period < self.reactor.monotonic():
                self._underextruding.set(True, eventtime)
                return True
        elif self._underextrusion_start_time is not None:
            self._underextruding.set(False, eventtime)
            self._underextrusion_start_time = None

        return False

    def _unhealthy_changed(self, old_value, new_value, eventtime):
        logging.info(f"{self.name}: 'unhealthy' changed from {old_value} to {new_value}")
        if new_value is False:
            return

        if new_value is True:
            self._respond_error("Unhealthy (%s)" %
                                (self._sensor_unhealthy_reason(), ))
        else:
            self._respond_info("Became healthy again")

    def _runout_changed(self, old_value, new_value, eventtime):
        logging.info(f"{self.name}: 'runout' changed from {old_value} to {new_value}")

    def _underextruding_changed(self, old_value, new_value, eventtime):
        logging.info(f"{self.name}: 'underextruding' changed from {old_value} to {new_value}")

        if new_value:
            self._respond_error("Detected underextrusion for over %.2fs" %
                                (self.underextrusion_period, ))
        elif self._underextrusion_start_time:
            self._respond_info("Underextrusion cleared after %.2fs" %
                                    (self.reactor.monotonic() - self._underextrusion_start_time))

    def _check_print_issues(self, eventtime):
        """ Call runout code when print issues are detected. """

        if not self._is_printing:
           return

        if not self._is_sensor_healthy():
            if not self._unhealthy:
                self._unhealthy.set(True, eventtime)
                self._runout_event_handler(eventtime)
            return
        else:
            self._unhealthy.set(False, eventtime)

        if self._is_runout_condition(eventtime):
            if not self._runout:
                self._runout.set(True, eventtime)
                self._runout_event_handler(eventtime)
            return
        else:
            # runout restored
            self._runout.set(False, eventtime)

    def _sensor_update_event(self, eventtime):
        """ Periodic timer to fetch sensor data and update internal state. """

        if not self._is_homing:
            self._update_state_from_sensor()
            self._status_evaluation_move = self._combine_moves_for_distance(self.move_evaluation_distance)

            if eventtime >= self.runout_helper.min_event_systime and self.runout_helper.sensor_enabled:
                self._check_print_issues(eventtime)

        return eventtime + CHECK_RUNOUT_TIMEOUT

    def all_moves_ended(self):
        for move in self._commanded_moves:
            move.ended = True

    def clear_move_queue(self):
        self._commanded_moves = []

    def has_stopped_moving(self):
        if len(self._commanded_moves) > 0:
            return self._commanded_moves[0].has_stopped_moving()

    def capture_history(self, capture):
        self._capture_history = capture

def load_config_prefix(config):
    return HighResolutionFilamentSensor(config)
