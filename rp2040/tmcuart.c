#include <string.h>

#include "pico/stdlib.h"
#include "hardware/pio.h"
#include "hardware/uart.h"

#include "tmcuart.h"

#define SERIAL_BAUD 40000

#define UART_INST uart1
#define UART_TX_PIN 4
#define UART_RX_PIN 5

void tmcuart_init()
{
    uart_init(UART_INST, SERIAL_BAUD);
    gpio_set_function(UART_TX_PIN, GPIO_FUNC_UART);
    gpio_set_function(UART_RX_PIN, GPIO_FUNC_UART);
}

uint8_t tmcuart_crc8(uint8_t *buf, size_t len)
{
    // Generate a CRC8-ATM value for a bytearray
    uint8_t crc = 0;

    for(size_t j = 0; j < len; j++) {
        uint8_t b = buf[j];
        for(size_t i = 0; i < 8; i++) {
            if ((crc >> 7) ^ (b & 0x01)) {
                crc = (crc << 1) ^ 0x07;
            } else {
                crc = (crc << 1);
            }
            crc &= 0xff;
            b >>= 1;
        }
    }

    return crc;
}

uint8_t tmcuart_getc()
{
    uint8_t c = 0;
    tmcuart_read(&c, 1);
    return c;
}

void tmcuart_read(uint8_t *buf, size_t len)
{
    uart_read_blocking(UART_INST, buf, len);
}

void tmcuart_write(uint8_t *buf, size_t len)
{
    uart_write_blocking(UART_INST, buf, len);
}

bool tmcuart_sync()
{
    if(uart_is_readable(UART_INST))
        return tmcuart_getc() == 0xf5;
    return false;
}

void prepare_register_data(uint8_t reg, uint8_t *buf, size_t *length);
void sensor_bus_register_write(uint8_t reg, const uint8_t *data, size_t length);

static void tmcuart_send_response(uint8_t reg)
{
    uint8_t data[255];
    size_t length = 0;

    memset((void *)&data, 0, sizeof(data));
    data[0] = 0x05;
    data[1] = 0xff;
    data[2] = reg;

    prepare_register_data(reg, &data[3], &length);

    if((length + 4) > sizeof(data))
        return;

    data[length+3] = tmcuart_crc8((uint8_t *)&data, length+3);

    // klipper tmcuart bitbang is sensitive to timing, avoid replying too fast
    sleep_us(500);

    tmcuart_write((uint8_t *)&data, length+4);
}

void tmcuart_loop()
{
    /* Read: sync, addr, reg, crc.
     * Write: sync, addr, reg | 0x80, four data bytes, crc - Klipper's
     * tmc_uart sets bit 7 of the register byte to mean write, and the CRC
     * covers everything before it. The two frames differ in length, so the
     * write bit decides how much to read before the CRC can be checked. */
    uint8_t cmd[8] = { 0xf5, 0, 0, 0, 0, 0, 0, 0 };
    uint8_t addr, reg;

    if (!tmcuart_sync())
        return;

    tmcuart_read(&cmd[1], 2);

    addr = cmd[1];
    reg = cmd[2];
    // Both UART images enable USB CDC stdio for the admin protocol, so a bad
    // frame cannot be printf'd without colliding with protocol frames on the
    // same CDC interface. Bad frames are silently dropped.
    (void)addr;

    if (reg & 0x80) {
        tmcuart_read(&cmd[3], 5);
        if (cmd[7] != tmcuart_crc8((uint8_t *)&cmd, 7))
            return;

        // Writes get no reply: the datagram has no acknowledgement, which is
        // what Klipper expects. A refused write is indistinguishable from an
        // accepted one on the wire.
        sensor_bus_register_write(reg & 0x7f, &cmd[3], 4);
        return;
    }

    tmcuart_read(&cmd[3], 1);
    if (cmd[3] != tmcuart_crc8((uint8_t *)&cmd, 3))
        return;

    tmcuart_send_response(reg);
}