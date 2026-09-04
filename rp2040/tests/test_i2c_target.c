/* Host tests for the I2C target's register-serving state machine.
 *
 * Why this exists. The handler has shipped two distinct bugs, neither of which
 * a host test suite could previously have caught, because `i2c_target.c` was
 * SDK-bound and reachable only from a bench board:
 *
 *   1. It pushed a whole payload in one burst on a single I2C_SLAVE_REQUEST.
 *      The RP2040's TX FIFO is 16 bytes and `i2c_write_byte_raw` is a bare
 *      register store whose only guard is an assert the Release build strips,
 *      so bytes 17+ of the 34-byte serial were silently discarded.
 *   2. Fixing that introduced a reset of the serving index in
 *      I2C_SLAVE_FINISH. But the SDK raises FINISH on a *repeated START* as
 *      well as a Stop - i.e. in the middle of the ordinary
 *      write-address-then-read transaction - so the cached payload was
 *      destroyed before a single byte of it could be served, and every read
 *      returned filler.
 *
 * Both are pure event-sequencing faults. No hardware is needed to reproduce
 * them, only an honest model of when the SDK delivers each event. That model
 * lives in `struct fake_bus` below, and these tests drive the real handler
 * through it.
 *
 * The file under test is #included rather than linked because the handler is
 * file-static. That keeps `i2c_target.c` free of test-only visibility changes,
 * at the cost of this file owning its dependencies - `prepare_register_data`
 * included, which normally lives in main.c and cannot be built for the host.
 */

/* The stub headers must come before the fake bus below, which needs
 * i2c_inst_t and i2c_slave_handler_t. `i2c_target.c` includes them too, but
 * that #include sits further down, after the definitions it depends on. */
#include <hardware/i2c.h>
#include <pico/i2c_slave.h>
#include <pico/stdlib.h>

#include <assert.h>
#include <stdint.h>
#include <string.h>

/* ------------------------------------------------------------------
 * The fake bus
 * ------------------------------------------------------------------ */

struct i2c_inst {
    int unused;
};

static struct i2c_inst fake_i2c_instance;
i2c_inst_t *const i2c0 = &fake_i2c_instance;

static struct {
    /* Bytes the master is about to write, and how far it has got. */
    uint8_t tx[8];
    size_t tx_length;
    size_t tx_position;

    /* Bytes the slave has written back to the master, in order. */
    uint8_t rx[64];
    size_t rx_length;

    /* Registered by i2c_target_init via the stubbed i2c_slave_init. */
    i2c_slave_handler_t handler;
    uint8_t address;
} bus;

uint8_t i2c_read_byte_raw(i2c_inst_t *i2c) {
    (void)i2c;
    assert(bus.tx_position < bus.tx_length);
    return bus.tx[bus.tx_position++];
}

void i2c_write_byte_raw(i2c_inst_t *i2c, uint8_t value) {
    (void)i2c;
    assert(bus.rx_length < sizeof(bus.rx));
    bus.rx[bus.rx_length++] = value;
}

void i2c_slave_init(i2c_inst_t *i2c, uint8_t address, i2c_slave_handler_t handler) {
    (void)i2c;
    bus.address = address;
    bus.handler = handler;
}

/* ------------------------------------------------------------------
 * The register file under test's control
 * ------------------------------------------------------------------ */

/* Stands in for main.c's real one. The tests own what a register contains, so
 * they can use a payload longer than the 16-byte TX FIFO without depending on
 * the identity or sensor register maps. */
static uint8_t fake_register;
static uint8_t fake_payload[64];
static size_t fake_payload_length;
static int prepare_calls;

void prepare_register_data(uint8_t reg, uint8_t *buf, size_t *length) {
    ++prepare_calls;
    *length = 0;
    if (reg != fake_register) {
        return;
    }
    memcpy(buf, fake_payload, fake_payload_length);
    *length = fake_payload_length;
}

#include "../i2c_target.c"

/* ------------------------------------------------------------------
 * Driving the handler the way the SDK does
 * ------------------------------------------------------------------ */

static void bus_reset(void) {
    memset(&bus, 0, sizeof(bus));
    prepare_calls = 0;
    i2c_target_init();
    assert(bus.handler != NULL);
}

static void master_writes(uint8_t byte) {
    assert(bus.tx_length < sizeof(bus.tx));
    bus.tx[bus.tx_length++] = byte;
    bus.handler(i2c0, I2C_SLAVE_RECEIVE);
}

/* A repeated START or a Stop. Named for what the SDK actually signals rather
 * than for "end of transaction", because it is emphatically not that. */
static void master_signals_start_or_stop(void) {
    bus.handler(i2c0, I2C_SLAVE_FINISH);
}

static void master_reads(size_t count) {
    for (size_t index = 0; index < count; ++index) {
        size_t before = bus.rx_length;
        bus.handler(i2c0, I2C_SLAVE_REQUEST);
        /* One byte per request, always. More would overrun the 16-byte TX
         * FIFO on device; fewer would stall the master, which is clocking a
         * byte out for each of these. */
        assert(bus.rx_length == before + 1u);
    }
}

static void set_register(uint8_t reg, const uint8_t *payload, size_t length) {
    assert(length <= sizeof(fake_payload));
    fake_register = reg;
    memcpy(fake_payload, payload, length);
    fake_payload_length = length;
}

/* 34 bytes: the size of READ_SERIAL, the largest real register and more than
 * twice the TX FIFO depth. */
static uint8_t long_payload[34];

static void build_long_payload(uint8_t seed) {
    for (size_t index = 0; index < sizeof(long_payload); ++index) {
        long_payload[index] = (uint8_t)(seed + index);
    }
}

/* ------------------------------------------------------------------
 * Tests
 * ------------------------------------------------------------------ */

/* The regression test for bug 2. A combined transaction is: write the register
 * address, repeated START, then read. The SDK delivers FINISH at that repeated
 * START, before any byte has been served. If the handler treats FINISH as
 * "transaction over" and drops its cache, every byte the master then clocks
 * out is filler. */
static void test_payload_survives_the_repeated_start(void) {
    bus_reset();
    build_long_payload(0x10);
    set_register(0x31, long_payload, sizeof(long_payload));

    master_writes(0x31);
    master_signals_start_or_stop();
    master_reads(sizeof(long_payload));

    assert(bus.rx_length == sizeof(long_payload));
    assert(memcmp(bus.rx, long_payload, sizeof(long_payload)) == 0);
}

/* The regression test for bug 1. 34 bytes cannot be pushed in one event; the
 * master re-requests per byte once the FIFO drains. `master_reads` asserts the
 * one-byte-per-request rule on every iteration, so a burst would fail here. */
static void test_serves_a_payload_larger_than_the_tx_fifo(void) {
    bus_reset();
    build_long_payload(0xA0);
    set_register(0x31, long_payload, sizeof(long_payload));

    master_writes(0x31);
    master_signals_start_or_stop();
    master_reads(sizeof(long_payload));

    assert(bus.rx_length == 34u);
    assert(bus.rx[0] == 0xA0);
    assert(bus.rx[16] == (uint8_t)(0xA0 + 16));  /* past the FIFO boundary */
    assert(bus.rx[33] == (uint8_t)(0xA0 + 33));
}

/* The payload is built once when the address arrives, not per request.
 * Rebuilding it per byte would re-run the base32 serial construction ~34 times
 * inside an ISR while the bus clock-stretches. */
static void test_builds_the_payload_once_per_transaction(void) {
    bus_reset();
    build_long_payload(0x01);
    set_register(0x31, long_payload, sizeof(long_payload));

    master_writes(0x31);
    master_signals_start_or_stop();
    master_reads(sizeof(long_payload));

    assert(prepare_calls == 1);
}

/* Some masters split the write and the read into two transactions separated by
 * a Stop rather than a repeated START. Same event sequence from the handler's
 * point of view, and it must behave identically. */
static void test_stop_separated_read_works_the_same(void) {
    bus_reset();
    build_long_payload(0x40);
    set_register(0x31, long_payload, sizeof(long_payload));

    master_writes(0x31);
    master_signals_start_or_stop(); /* Stop ends the write transaction */
    master_reads(4u);

    assert(bus.rx[0] == 0x40);
    assert(bus.rx[3] == 0x43);
}

/* A second transaction naming a different register must serve that register,
 * not the stale cache from the first. */
static void test_a_second_transaction_replaces_the_cache(void) {
    static const uint8_t two_bytes[] = {0xAA, 0xBB};

    bus_reset();
    build_long_payload(0x70);
    set_register(0x31, long_payload, sizeof(long_payload));

    master_writes(0x31);
    master_signals_start_or_stop();
    master_reads(2u);
    master_signals_start_or_stop();

    bus.rx_length = 0;
    set_register(0x33, two_bytes, sizeof(two_bytes));

    master_writes(0x33);
    master_signals_start_or_stop();
    master_reads(2u);

    assert(bus.rx_length == 2u);
    assert(bus.rx[0] == 0xAA);
    assert(bus.rx[1] == 0xBB);
}

/* Reading past the payload yields a defined filler rather than walking off the
 * buffer or re-serving stale bytes. Note this 0x00 is the transport's
 * end-of-payload filler; it is unrelated to the 0xff a locked board fills a
 * register's own length with, and the two never occupy the same position. */
static void test_reads_past_the_payload_yield_filler(void) {
    static const uint8_t two_bytes[] = {0xAA, 0xBB};

    bus_reset();
    set_register(0x33, two_bytes, sizeof(two_bytes));

    master_writes(0x33);
    master_signals_start_or_stop();
    master_reads(5u);

    assert(bus.rx[0] == 0xAA);
    assert(bus.rx[1] == 0xBB);
    assert(bus.rx[2] == 0x00);
    assert(bus.rx[3] == 0x00);
    assert(bus.rx[4] == 0x00);
}

/* An unknown register leaves the length at zero, so every byte is filler. The
 * handler must not serve whatever happened to be in the buffer. */
static void test_unknown_register_serves_only_filler(void) {
    bus_reset();
    build_long_payload(0x55);
    set_register(0x31, long_payload, sizeof(long_payload));

    master_writes(0x99); /* not fake_register */
    master_signals_start_or_stop();
    master_reads(3u);

    assert(bus.rx[0] == 0x00);
    assert(bus.rx[1] == 0x00);
    assert(bus.rx[2] == 0x00);
}

/* A master that issues a read with no preceding register write must not fault
 * or leak the previous transaction's bytes. */
static void test_read_without_a_preceding_address_is_safe(void) {
    bus_reset();

    master_reads(3u);

    assert(bus.rx[0] == 0x00);
    assert(bus.rx[1] == 0x00);
    assert(bus.rx[2] == 0x00);
    assert(prepare_calls == 0);
}

/* Bytes written after the register address are discarded - the register
 * interface is read-only - and must not be mistaken for a new address. */
static void test_further_written_bytes_do_not_rebind_the_register(void) {
    static const uint8_t two_bytes[] = {0xAA, 0xBB};

    bus_reset();
    set_register(0x33, two_bytes, sizeof(two_bytes));

    master_writes(0x33);
    master_writes(0x99); /* a data byte, not a new address */
    master_signals_start_or_stop();
    master_reads(2u);

    assert(prepare_calls == 1);
    assert(bus.rx[0] == 0xAA);
    assert(bus.rx[1] == 0xBB);
}

int main(void) {
    test_payload_survives_the_repeated_start();
    test_serves_a_payload_larger_than_the_tx_fifo();
    test_builds_the_payload_once_per_transaction();
    test_stop_separated_read_works_the_same();
    test_a_second_transaction_replaces_the_cache();
    test_reads_past_the_payload_yield_filler();
    test_unknown_register_serves_only_filler();
    test_read_without_a_preceding_address_is_safe();
    test_further_written_bytes_do_not_rebind_the_register();
    return 0;
}
