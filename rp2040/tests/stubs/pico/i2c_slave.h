/* Host stand-in for the Pico SDK's <pico/i2c_slave.h>.
 *
 * The event enum must keep the SDK's meanings, because the test's whole point
 * is the sequence in which the SDK delivers them. In particular
 * I2C_SLAVE_FINISH does NOT mean "the transaction ended": the SDK raises it on
 * a repeated START too, mid-transaction, between a register-address write and
 * the read that follows it (i2c_slave.c raises it whenever START_DET,
 * STOP_DET or TX_ABRT fires while a transfer is in progress). Assuming
 * otherwise is exactly the bug test_i2c_target.c guards.
 */
#ifndef ROADRUNNER_TEST_STUB_PICO_I2C_SLAVE_H
#define ROADRUNNER_TEST_STUB_PICO_I2C_SLAVE_H

#include <hardware/i2c.h>

#include <stdint.h>

typedef enum i2c_slave_event_t {
    I2C_SLAVE_RECEIVE, /* master has written data; read it from the Rx FIFO */
    I2C_SLAVE_REQUEST, /* master is reading; write one byte to the Tx FIFO */
    I2C_SLAVE_FINISH,  /* master sent a Stop OR a repeated Start */
} i2c_slave_event_t;

typedef void (*i2c_slave_handler_t)(i2c_inst_t *i2c, i2c_slave_event_t event);

/* Defined by the test so it can capture the handler `i2c_target_init`
 * registers, rather than reaching for the file-static directly. */
void i2c_slave_init(i2c_inst_t *i2c, uint8_t address, i2c_slave_handler_t handler);

#endif
