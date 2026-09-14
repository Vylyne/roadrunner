#ifndef ROADRUNNER_SENSOR_BUS_PROVISION_H
#define ROADRUNNER_SENSOR_BUS_PROVISION_H

/* Provisioning over the sensor bus (I2C and TMC-UART).
 *
 * A UUID is sixteen bytes and a TMC-UART write carries four, so the host
 * stages it in four register writes and then commits:
 *
 *   0x50-0x53  UUID chunks 0-3, four bytes each, indexed by register
 *   0x54       COMMIT: {0, 0, 0, crc} where crc is CRC-8/ATM (poly 0x07,
 *              init 0, MSB-first - the USB admin frame CRC) over the sixteen
 *              staged bytes
 *
 * Bytes are staged in wire order on both transports: the order an I2C master
 * writes them, and the order they appear in a TMC-UART write datagram (which
 * Klipper's tmc_uart packs most-significant byte first). The host therefore
 * sends the same sixteen bytes either way, and "the low byte" of COMMIT is its
 * last byte on the wire.
 *
 * This module only stages and validates. Applying the identity writes flash,
 * which must not happen in the I2C target ISR that calls
 * rr_sensor_bus_provision_write(), so a validated commit is parked for the
 * main loop to collect with rr_sensor_bus_provision_take_commit(). The caller
 * owns mutual exclusion between the two when they run in different contexts.
 * See docs/roadrunner-sensor-bus-provisioning-design.md.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define RR_SENSOR_BUS_PROVISION_CHUNK_FIRST 0x50u
#define RR_SENSOR_BUS_PROVISION_CHUNK_LAST  0x53u
#define RR_SENSOR_BUS_PROVISION_COMMIT      0x54u
#define RR_SENSOR_BUS_PROVISION_WRITE_SIZE  4u
#define RR_SENSOR_BUS_PROVISION_UUID_SIZE   16u

uint8_t rr_sensor_bus_provision_crc8(const uint8_t *data, size_t length);

/* Zero the stage and drop any parked commit. Called once at boot. */
void rr_sensor_bus_provision_reset(void);

/* Apply one register write. `locked` is whether the board is unprovisioned;
 * every write is refused otherwise. A write of any length other than four
 * bytes, or to any register outside 0x50-0x54, changes nothing.
 *
 * Returns true only when this write was a COMMIT that validated, i.e. a UUID
 * is now parked for rr_sensor_bus_provision_take_commit(). */
bool rr_sensor_bus_provision_write(uint8_t reg, const uint8_t *data,
                                   size_t length, bool locked);

/* Collect a parked commit, once. Returns false when there is none. */
bool rr_sensor_bus_provision_take_commit(
    uint8_t uuid[RR_SENSOR_BUS_PROVISION_UUID_SIZE]);

/* Every transport forwards bus writes here. Defined by main.c on the device
 * and by the tests off-target. */
void sensor_bus_register_write(uint8_t reg, const uint8_t *data, size_t length);

#endif
