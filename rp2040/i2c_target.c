#include <stdio.h>

#include <hardware/i2c.h>
#include <pico/i2c_slave.h>
#include <pico/stdlib.h>

#include "i2c_target.h"

#define I2C_INST i2c0
#define I2C_SDA_PIN 4
#define I2C_SCL_PIN 5
#define I2C_BAUDRATE 100 * 1000 /* 100 kHz */
#define I2C_ADDRESS 0x40

static struct
{
    uint8_t mem[256];
    size_t mem_length;   /* bytes of mem[] valid for the current transaction */
    size_t mem_position; /* next byte of mem[] to serve on I2C_SLAVE_REQUEST */
    uint8_t mem_address;
    bool mem_address_written;
    uint8_t write_data[4]; /* the first bytes written after the address */
    size_t write_length;   /* every byte written after the address, counted */
} context;

void prepare_register_data(uint8_t reg, uint8_t *buf, size_t *length);
void sensor_bus_register_write(uint8_t reg, const uint8_t *data, size_t length);

static void i2c_slave_handler(i2c_inst_t *i2c, i2c_slave_event_t event) {
    switch (event) {
    case I2C_SLAVE_RECEIVE: // master has written some data
        if (!context.mem_address_written) {
            // writes always start with the memory address
            context.mem_address = i2c_read_byte_raw(i2c);
            context.mem_address_written = true;

            /* Cache the payload once, here, at the point the register
             * address becomes known - not per I2C_SLAVE_REQUEST below.
             * The TX FIFO (IC_TX_BUFFER_DEPTH) is 16 bytes, smaller than
             * some register payloads (up to 34 bytes), so RD_REQ re-fires
             * once per byte once the FIFO drains; re-running
             * prepare_register_data() on every one of those events would
             * rebuild the serial string ~34 times inside an ISR while the
             * bus clock-stretches. */
            context.mem_length = 0;
            context.mem_position = 0;
            prepare_register_data(context.mem_address, context.mem, &context.mem_length);
        } else {
            /* Register data. Only the provisioning staging registers accept
             * any, and none takes more than four bytes; the rest are counted
             * so an oversized write is recognised and ignored rather than
             * truncated into something that looks valid. */
            uint8_t byte = i2c_read_byte_raw(i2c);

            if (context.write_length < sizeof(context.write_data)) {
                context.write_data[context.write_length] = byte;
            }
            if (context.write_length < SIZE_MAX) {
                context.write_length++;
            }
        }
        break;
    case I2C_SLAVE_REQUEST: // master is requesting data
        /* Serve one byte per event from the cached payload, advancing
         * mem_position. This is what makes a payload larger than the
         * 16-byte TX FIFO work: the master re-issues RD_REQ for every byte
         * it clocks out, and each call here answers exactly one of those,
         * rather than trying to push the whole payload in a single burst
         * that the FIFO would silently truncate. */
        if (context.mem_position < context.mem_length) {
            i2c_write_byte_raw(i2c, context.mem[context.mem_position]);
            context.mem_position++;
        } else {
            /* Past the end of the payload (or an unknown register, where
             * mem_length is 0): send a defined filler rather than walking
             * off the buffer or re-serving stale bytes. */
            i2c_write_byte_raw(i2c, 0x00);
        }
        break;
    case I2C_SLAVE_FINISH: // master has signalled Stop, or a repeated Start
        /* This also fires mid-transaction on a repeated START (e.g. between
         * the register-address write and the read that follows it), not
         * only on Stop. Do not reset mem_length or mem_position here: the
         * cached payload built in I2C_SLAVE_RECEIVE must survive into the
         * read phase. Only mem_address_written is reset, so the next write
         * is treated as a fresh register address; mem_length/mem_position
         * are re-armed there too, at the point a new address arrives.
         *
         * A write carrying data ends here as well, and is handed on only now
         * that its length is known. An address-then-read transaction reaches
         * FINISH with no data bytes, so reads never get this far. Refusals
         * are silent: the peripheral has already ACKed every byte, and the
         * host confirms provisioning by reading the serial back, not by the
         * bus acknowledgement. */
        if (context.mem_address_written && context.write_length > 0) {
            sensor_bus_register_write(context.mem_address, context.write_data,
                                      context.write_length);
        }
        context.write_length = 0;
        context.mem_address_written = false;
        break;
    default:
        break;
    }
}

void i2c_target_init()
{
    gpio_init(I2C_SDA_PIN);
    gpio_set_function(I2C_SDA_PIN, GPIO_FUNC_I2C);
    gpio_pull_up(I2C_SDA_PIN);

    gpio_init(I2C_SCL_PIN);
    gpio_set_function(I2C_SCL_PIN, GPIO_FUNC_I2C);
    gpio_pull_up(I2C_SCL_PIN);

    i2c_init(I2C_INST, I2C_BAUDRATE);
    i2c_slave_init(I2C_INST, I2C_ADDRESS, &i2c_slave_handler);
}
