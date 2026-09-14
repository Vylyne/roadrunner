/* Host stand-in for the Pico SDK's <hardware/uart.h>.
 *
 * Declares only what `tmcuart.c` uses. tests/test_tmcuart.c defines these
 * functions over a scripted byte queue. */
#ifndef ROADRUNNER_TEST_STUB_HARDWARE_UART_H
#define ROADRUNNER_TEST_STUB_HARDWARE_UART_H

#include <pico/stdlib.h>

typedef struct uart_inst uart_inst_t;

extern uart_inst_t *const uart1;

#define GPIO_FUNC_UART 2

void uart_init(uart_inst_t *uart, uint baudrate);
bool uart_is_readable(uart_inst_t *uart);
void uart_read_blocking(uart_inst_t *uart, uint8_t *dst, size_t len);
void uart_write_blocking(uart_inst_t *uart, const uint8_t *src, size_t len);

#endif
