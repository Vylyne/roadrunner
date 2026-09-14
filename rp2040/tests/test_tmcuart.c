/* Host tests for the TMC-UART datagram loop.
 *
 * Read and write datagrams differ in length, and the write bit in the register
 * byte is the only thing that says which one is arriving. Getting that wrong
 * does not fail loudly: a write read as a read fails its CRC and is dropped,
 * which on the wire looks exactly like a refused write. The frames below are
 * golden bytes built the way Klipper's tmc_uart builds them (_encode_write:
 * sync, addr, reg | 0x80, value most-significant byte first, CRC), with the
 * CRC computed outside this firmware. */

#include <hardware/uart.h>

#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "tmcuart.h"

struct uart_inst {
    int unused;
};

static struct uart_inst fake_uart_instance;
uart_inst_t *const uart1 = &fake_uart_instance;

static struct {
    uint8_t rx[32];
    size_t rx_length;
    size_t rx_position;
    uint8_t tx[64];
    size_t tx_length;
} line;

void uart_init(uart_inst_t *uart, uint baudrate)
{
    (void)uart;
    (void)baudrate;
}

bool uart_is_readable(uart_inst_t *uart)
{
    (void)uart;
    return line.rx_position < line.rx_length;
}

void uart_read_blocking(uart_inst_t *uart, uint8_t *dst, size_t len)
{
    (void)uart;
    /* On the device this would block forever; here it is a test failure. */
    assert(line.rx_position + len <= line.rx_length);
    memcpy(dst, line.rx + line.rx_position, len);
    line.rx_position += len;
}

void uart_write_blocking(uart_inst_t *uart, const uint8_t *src, size_t len)
{
    (void)uart;
    assert(line.tx_length + len <= sizeof(line.tx));
    memcpy(line.tx + line.tx_length, src, len);
    line.tx_length += len;
}

static int prepare_calls;
static uint8_t prepared_register;

void prepare_register_data(uint8_t reg, uint8_t *buf, size_t *length)
{
    static const uint8_t value[4] = {0xde, 0xad, 0xbe, 0xef};

    ++prepare_calls;
    prepared_register = reg;
    memcpy(buf, value, sizeof(value));
    *length = sizeof(value);
}

static int write_calls;
static uint8_t written_register;
static uint8_t written_data[4];
static size_t written_length;

void sensor_bus_register_write(uint8_t reg, const uint8_t *data, size_t length)
{
    ++write_calls;
    written_register = reg;
    written_length = length;
    memcpy(written_data, data, length < 4u ? length : 4u);
}

static void line_reset(const uint8_t *bytes, size_t length)
{
    memset(&line, 0, sizeof(line));
    assert(length <= sizeof(line.rx));
    memcpy(line.rx, bytes, length);
    line.rx_length = length;
    prepare_calls = 0;
    write_calls = 0;
}

static void test_a_read_datagram_is_answered(void)
{
    static const uint8_t frame[] = {0xf5, 0x00, 0x25, 0x7a};

    line_reset(frame, sizeof(frame));
    tmcuart_loop();

    assert(line.rx_position == sizeof(frame));
    assert(prepare_calls == 1);
    assert(prepared_register == 0x25);
    assert(write_calls == 0);
    assert(line.tx_length == 8u);
    assert(line.tx[0] == 0x05 && line.tx[1] == 0xff && line.tx[2] == 0x25);
    assert(line.tx[3] == 0xde && line.tx[6] == 0xef);
    assert(line.tx[7] == tmcuart_crc8(line.tx, 7u));
}

static void test_a_write_datagram_reaches_the_register_hook(void)
{
    static const uint8_t frame[] = {
        0xf5, 0x00, 0xd0, 0x12, 0x34, 0x56, 0x78, 0xd2,
    };

    line_reset(frame, sizeof(frame));
    tmcuart_loop();

    assert(line.rx_position == sizeof(frame));
    assert(write_calls == 1);
    assert(written_register == 0x50);
    assert(written_length == 4u);
    /* Wire order, as staged: Klipper's most-significant byte first. */
    assert(written_data[0] == 0x12 && written_data[1] == 0x34);
    assert(written_data[2] == 0x56 && written_data[3] == 0x78);
    assert(prepare_calls == 0);
    assert(line.tx_length == 0u);
}

static void test_a_write_with_a_bad_crc_is_dropped(void)
{
    static const uint8_t frame[] = {
        0xf5, 0x00, 0xd0, 0x12, 0x34, 0x56, 0x78, 0xd3,
    };

    line_reset(frame, sizeof(frame));
    tmcuart_loop();

    assert(line.rx_position == sizeof(frame));
    assert(write_calls == 0);
    assert(prepare_calls == 0);
    assert(line.tx_length == 0u);
}

static void test_a_read_with_a_bad_crc_is_dropped(void)
{
    static const uint8_t frame[] = {0xf5, 0x00, 0x25, 0x7b};

    line_reset(frame, sizeof(frame));
    tmcuart_loop();

    assert(prepare_calls == 0);
    assert(line.tx_length == 0u);
}

static void test_the_frame_crc_is_the_trinamic_one(void)
{
    static const uint8_t frame[] = {0xf5, 0x00, 0xd0, 0x12, 0x34, 0x56, 0x78};

    assert(tmcuart_crc8((uint8_t *)frame, sizeof(frame)) == 0xd2);
}

int main(void)
{
    test_the_frame_crc_is_the_trinamic_one();
    test_a_read_datagram_is_answered();
    test_a_write_datagram_reaches_the_register_hook();
    test_a_write_with_a_bad_crc_is_dropped();
    test_a_read_with_a_bad_crc_is_dropped();
    puts("tmcuart tests passed");
    return 0;
}
