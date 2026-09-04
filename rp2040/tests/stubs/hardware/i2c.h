/* Host stand-in for the Pico SDK's <hardware/i2c.h>.
 *
 * The two byte accessors are the seam the test drives the handler through.
 * On real hardware `i2c_write_byte_raw` is a bare `hw->data_cmd = value` with
 * no flow control and a 16-byte TX FIFO behind it, which is why the handler
 * must emit exactly one byte per I2C_SLAVE_REQUEST - the test asserts that,
 * because exceeding the FIFO fails silently on device (the SDK's only guard is
 * an assert, and the firmware builds Release with -DNDEBUG).
 */
#ifndef ROADRUNNER_TEST_STUB_HARDWARE_I2C_H
#define ROADRUNNER_TEST_STUB_HARDWARE_I2C_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct i2c_inst i2c_inst_t;

extern i2c_inst_t *const i2c0;

/* Defined by the test, which owns the fake bus. */
uint8_t i2c_read_byte_raw(i2c_inst_t *i2c);
void i2c_write_byte_raw(i2c_inst_t *i2c, uint8_t value);

static inline unsigned int i2c_init(i2c_inst_t *i2c, unsigned int baudrate) {
    (void)i2c;
    return baudrate;
}

#endif
