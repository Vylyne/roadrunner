# Bench harness

A two-board rig for validating the identity, transport and provisioning work on
real hardware without touching a printer. `AGENTS.md` requires this: connected
printer hardware is production hardware, and a toolhead or installed sensor is
never a test target.

Status: built and running, 2026-09-13. Klipper host is a Raspberry Pi rather
than WSL2. I2C bring-up passed on the first attempt; UART not yet exercised.

## Shape

- **DUT** — an RP2040-Zero running Roadrunner firmware. Full Roadrunner hardware
  is ideal; a bare board is enough for everything in the identity and
  provisioning plan.
- **Master** — an RP2040-Zero running **stock Klipper firmware**, driving the
  sensor bus.

### Why stock Klipper rather than custom master firmware

The chunked-read design exists to survive Klipper-specific behaviour: the
ten-byte `tmcuart` buffer, `shutdown("tmcuart data too large")` as the failure
mode rather than a returned error, and the retry semantics of
`MCU_TMC_uart_bitbang`. Custom master firmware would not reproduce any of it, so
it could pass while the real thing takes a printer down.

Running Klippy also puts the extra, `get_status` and the identity cache under
test in the same run, which is where most of the host-side work lives. Nothing
is simulated — the master is a real MCU over USB and the code path is the
production one.

Klipper's own simulator (`simulavr`, host-only MCU) is not useful here: it
cannot drive real I2C or UART to the DUT.

The adversarial cases — a missing staging chunk, a wrong commit CRC, the spliced
UUID — are firmware register logic and belong in `rp2040/tests` as host tests.
They need no bus at all. Between the two there is nothing left that wants custom
master firmware.

## Parts

| Qty | Part | Purpose |
| --- | --- | --- |
| 2 | RP2040-Zero | DUT and master |
| 4 | 330 Ω | series protection on each signal line |
| 3 | 4.7 kΩ | two I2C pull-ups (2.2 kΩ works; see the margin note), one thermistor pull-up |
| 1 | 100 kΩ | fake thermistor on the master |
| — | wire | including an explicit ground strap |

## Wiring

The DUT serves I2C and UART on the **same two pins** — `SDA=4, SCL=5`
([rp2040/i2c_target.c:10-11](../rp2040/i2c_target.c#L10-L11)) and `TX=4, RX=5`
([rp2040/tmcuart.c:12-13](../rp2040/tmcuart.c#L12-L13)) — so one harness covers
both transports with no rewiring.

| Master | Through | DUT | Role |
| --- | --- | --- | --- |
| GP4 + 4.7 kΩ pull-up to 3V3 | 330 Ω | GP4 | I2C SDA |
| GP5 + 4.7 kΩ pull-up to 3V3 | 330 Ω | GP5 | I2C SCL |
| GP9 | 330 Ω | GP4 | UART: master RX ← DUT TX |
| GP8 | 330 Ω | GP5 | UART: master TX → DUT RX |
| GND | — | GND | required, see below |
| GP26 | 4.7 kΩ to 3V3 **and** 100 kΩ to GND | — | fake thermistor |

Both boards take power from their own USB connection to the host. **Run an
explicit ground strap anyway** — grounding only through two USB cables and the
host is a long, noisy return path.

### Direction matters on the UART pair

DUT GP4 is the DUT's *transmit* pin, so the master's GP9 must be the **receive**
side (`uart_rx_pin`), and GP8 drives the DUT's receive pin (`uart_tx_pin`).
Getting these backwards is the easy mistake; the 330 Ω resistors are what make
it a non-event rather than two damaged pins.

The hardware function labels happen to agree — master GP9 is UART1 RX and GP8 is
UART1 TX — but that is coincidence, not a constraint. Klipper's `tmc_uart`
**bit-bangs**, so any GPIO pair would work.

### Electrical notes

- **Prefer 4.7 kΩ over 2.2 kΩ.** With the pull-up at the master and 330 Ω in
  series to the DUT, the master reads its own low level across the divider. At
  4.7 kΩ the master node sits around 0.25 V against a 0.99 V threshold; at
  2.2 kΩ it is around 0.49 V. Both work; 4.7 kΩ has roughly twice the margin,
  and edge rates are irrelevant at 100 kHz over a short harness.
- **The DUT already pulls up internally** (`gpio_pull_up` on both SDA and SCL,
  [i2c_target.c:86,90](../rp2040/i2c_target.c#L86)). At roughly 50–80 kΩ these do
  not replace the externals, but they do mean a missing external pull-up looks
  like a flaky bus rather than a dead one.
- **Never load both transport sections at once.** Both master pairs land on the
  same DUT pins. With a UART section live during an I2C run, GP8 idles high and
  fights SCL through 330 Ω — around 9 mA continuous and eroded low-level margin.
  Nothing burns; it produces intermittent failures that look like a bus fault.

## Switching transports

The harness avoids rewiring, not reflashing. Each transport needs a matched
pair:

| Transport | DUT firmware | Master config |
| --- | --- | --- |
| I2C | `roadrunner_v1_i2c_rgb` / `_grb` | `i2c_bus: i2c0b` |
| UART | `roadrunner_v1_uart_rgb` / `_grb` | `uart_rx_pin` / `uart_tx_pin` |
| USB serial | `roadrunner_v1_usbserial_rgb` / `_grb` | `serial:` — no harness needed |

Pick `_rgb` or `_grb` to match the DUT's LED; it does not affect any transport.

Switching transport means reflashing the DUT **and** swapping the config
section. Doing one without the other is the most likely source of a confusing
first session.

## Host setup

Klippy does not run on Windows. Either:

- **WSL2 plus `usbipd-win`**, attaching the master's USB device into the WSL
  instance. Keeps everything on the same machine as the repo. The attach step
  has to be repeated after each replug, which is worth scripting.
- **A Pi or spare Linux box** as the Klipper host. More production-like, no USB
  passthrough, but the repo is then remote from the test.

WSL2 is the better start: the extra is Python and gets edited constantly during
this work.

## Bench `printer.cfg`

### Base

```ini
[mcu]
serial: /dev/serial/by-id/usb-Klipper_rp2040_<master-id>-if00

[printer]
kinematics: none
max_velocity: 100
max_accel: 1000
```

`kinematics: none` is real and intended for exactly this — Klipper's
`klippy/kinematics/none.py` describes itself as "Dummy 'none' kinematics support
(for developer testing)". No steppers, no homing, no axis limits.

### The extruder that has to exist

The extra takes a required `extruder` option and `_handle_ready` monkey-patches
`extruder.process_move`
([high_resolution_filament_sensor.py:1261](../klippy/extras/high_resolution_filament_sensor.py#L1261)).
`DummyExtruder` cannot stand in: it defines no `process_move`, and
`add_printer_objects` does not register it as a printer object at all — the only
instance lives inside `toolhead.py`. Without a real `[extruder]` section the
lookup fails, the poll timer never starts, and it presents as "the sensor never
reads."

```ini
[extruder]
step_pin: gpio10
dir_pin: gpio11
enable_pin: !gpio12
microsteps: 16
rotation_distance: 22.6
nozzle_diameter: 0.4
filament_diameter: 1.75
heater_pin: gpio13
sensor_type: Generic 3950
sensor_pin: gpio26
control: pid
pid_Kp: 20.0
pid_Ki: 1.0
pid_Kd: 100.0
min_temp: 0
max_temp: 300
```

**Declared but not connected.** Step/dir/enable is open-loop — Klipper sets up
step generation and never checks that a driver is listening, so three unused
GPIOs satisfy it with nothing soldered. `heater_pin` is just a PWM output.

**Do not add a `[tmc2209 extruder]` section** or any other `[tmcXXXX]`. Those
talk to a real driver over UART and fail at startup when nothing answers.

**The thermistor is the only part that must be real**, because it is the only
input Klipper actually reads — an out-of-range ADC is a startup fault.

It takes **two** resistors: 4.7 kΩ from `gpio26` to 3V3, and 100 kΩ from `gpio26`
to GND. `pullup_resistor: 4700` is not a resistor Klipper provides — it is the
value Klipper *assumes* a printer board has fitted, used only in the conversion
math. A bare RP2040-Zero has no such pull-up. With the 100 kΩ alone the ADC pin
is barely driven, and on the first build of this rig it read a wandering
171–177 °C instead of 25 °C. That stays inside `min_temp`/`max_temp` so nothing
faults — which is the trap: it drifts, and a drift past either limit shuts the
bench down mid-test for a reason unrelated to the sensor. With both resistors
fitted the divider reads about 25 °C under `Generic 3950`.

### I2C

```ini
[high_resolution_filament_sensor dut]
extruder: extruder
i2c_bus: i2c0b
i2c_address: 64
i2c_speed: 100000
rotation_distance: 23.5
underextrusion_max_rate: 0.5
underextrusion_period: 5
```

`i2c0b` is `gpio4,gpio5` on the RP2040 (`DECL_CONSTANT_STR("BUS_PINS_i2c0b",
"gpio4,gpio5")` in Klipper's `src/rp2040/i2c.c`), which is the master pair in the
table above. Address 64 is `0x40`, the firmware's target address; 100 kHz is the
extra's default and one of only two speeds Klipper's RP2040 I2C supports.

### UART

```ini
[high_resolution_filament_sensor dut]
extruder: extruder
uart_rx_pin: gpio9
uart_tx_pin: gpio8
rotation_distance: 23.5
underextrusion_max_rate: 0.5
underextrusion_period: 5
```

`uart_rx_pin` is what selects this transport at all — without it the extra falls
through to I2C ([line 1000](../klippy/extras/high_resolution_filament_sensor.py#L1000)).

## What a bare DUT can and cannot test

Everything in
[roadrunner-identity-implementation-plan.md](roadrunner-identity-implementation-plan.md)
works on a bare board:

- Identity reads run from `_update_identity`, which is called *before* the
  `if not self._sensor_connected: return` early-out, so they do not depend on a
  healthy sensor.
- With no magnet, `_is_sensor_healthy()` is false — but `_check_print_issues`
  returns early when not printing, so nothing fires.
- The move queue stays empty and the under-extrusion path is inert. No filament
  motion is needed for identity or provisioning work.

Full Roadrunner hardware is needed only to exercise the motion and runout paths.

## First bring-up order

1. Flash stock Klipper to the master, confirm Klippy starts with the base config
   and the extruder alone — no sensor section. This separates "the fake
   extruder is wrong" from "the bus is wrong".
2. Flash the DUT with the I2C variant, add the I2C section, confirm the sensor
   answers.
3. Reflash the DUT to the UART variant, swap the section, confirm again.
4. Only then start on the plan's stages.
