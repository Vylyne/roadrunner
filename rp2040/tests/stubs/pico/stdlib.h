/* Host stand-in for the Pico SDK's <pico/stdlib.h>.
 *
 * Exists so `i2c_target.c` can be compiled and driven on the host. The real
 * header pulls in the whole SDK; this declares only what that one file uses.
 * See tests/test_i2c_target.c for why the handler is worth testing off-target
 * at all - its bug was in event *sequencing*, which needs no hardware to
 * reproduce and had already shipped twice before a bench run would have caught
 * it.
 */
#ifndef ROADRUNNER_TEST_STUB_PICO_STDLIB_H
#define ROADRUNNER_TEST_STUB_PICO_STDLIB_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef unsigned int uint;

/* GPIO setup is a no-op here: the handler under test never touches a pin, and
 * only `i2c_target_init` calls these. Recording them would test the SDK, not
 * this firmware. */
#define GPIO_FUNC_I2C 3

static inline void gpio_init(uint gpio) { (void)gpio; }
static inline void gpio_set_function(uint gpio, int fn) { (void)gpio; (void)fn; }
static inline void gpio_pull_up(uint gpio) { (void)gpio; }

#endif
