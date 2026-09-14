/* Host tests for the sensor-bus provisioning stage.
 *
 * The CRC is checked against golden values rather than against the host's
 * implementation: CRC-8/ATM's catalogue check value for "123456789", and the
 * UUID 00 01 .. 0f, whose value tests/test_high_resolution_filament_sensor.py
 * pins on the Klippy side too. */

#include "sensor_bus_provision.h"
#include "identity_record.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

#define CHUNK0 RR_SENSOR_BUS_PROVISION_CHUNK_FIRST
#define COMMIT RR_SENSOR_BUS_PROVISION_COMMIT

static const uint8_t counting_uuid[16] = {
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
    0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f,
};
static const uint8_t counting_uuid_crc = 0x41;

static void stage_chunk(const uint8_t uuid[16], unsigned int chunk)
{
    assert(!rr_sensor_bus_provision_write((uint8_t)(CHUNK0 + chunk),
                                          uuid + chunk * 4u, 4u, true));
}

static void stage_all(const uint8_t uuid[16])
{
    for (unsigned int chunk = 0; chunk < 4u; ++chunk) {
        stage_chunk(uuid, chunk);
    }
}

static bool commit_with(uint8_t crc, bool locked)
{
    const uint8_t data[4] = {0, 0, 0, crc};

    return rr_sensor_bus_provision_write(COMMIT, data, 4u, locked);
}

static bool commit_uuid(const uint8_t uuid[16])
{
    return commit_with(rr_sensor_bus_provision_crc8(uuid, 16u), true);
}

static void assert_nothing_parked(void)
{
    uint8_t uuid[16];

    assert(!rr_sensor_bus_provision_take_commit(uuid));
}

static void test_crc_matches_the_golden_values(void)
{
    assert(rr_sensor_bus_provision_crc8((const uint8_t *)"123456789", 9u) == 0xf4);
    assert(rr_sensor_bus_provision_crc8(counting_uuid, 16u) == counting_uuid_crc);
}

/* The serial the host expects back after committing this UUID. Klippy's
 * identity_serial() pins the same string, so the two cannot drift apart. */
static void test_the_committed_uuid_names_the_expected_serial(void)
{
    char serial[RR_IDENTITY_SERIAL_LENGTH + 1];

    rr_identity_serial(counting_uuid, serial);
    assert(strcmp(serial, "RR-00041061050R3GG28A1C60T3GF") == 0);
}

static void test_a_complete_stage_commits_once(void)
{
    uint8_t uuid[16];

    rr_sensor_bus_provision_reset();
    stage_all(counting_uuid);
    assert(commit_with(counting_uuid_crc, true));

    assert(rr_sensor_bus_provision_take_commit(uuid));
    assert(memcmp(uuid, counting_uuid, sizeof(uuid)) == 0);
    assert_nothing_parked();
}

static void test_chunks_after_zero_may_arrive_in_any_order(void)
{
    uint8_t uuid[16];

    rr_sensor_bus_provision_reset();
    stage_chunk(counting_uuid, 0u);
    stage_chunk(counting_uuid, 3u);
    stage_chunk(counting_uuid, 1u);
    stage_chunk(counting_uuid, 2u);
    assert(commit_uuid(counting_uuid));
    assert(rr_sensor_bus_provision_take_commit(uuid));
    assert(memcmp(uuid, counting_uuid, sizeof(uuid)) == 0);
}

static void test_a_provisioned_board_refuses_every_write(void)
{
    rr_sensor_bus_provision_reset();
    for (unsigned int chunk = 0; chunk < 4u; ++chunk) {
        assert(!rr_sensor_bus_provision_write((uint8_t)(CHUNK0 + chunk),
                                              counting_uuid + chunk * 4u,
                                              4u, false));
    }
    assert(!commit_with(counting_uuid_crc, false));
    assert_nothing_parked();

    /* Nothing was staged either: the same commit, now locked, still fails. */
    assert(!commit_with(counting_uuid_crc, true));
    assert_nothing_parked();
}

static void test_commit_with_a_missing_chunk_writes_nothing(void)
{
    rr_sensor_bus_provision_reset();
    stage_chunk(counting_uuid, 0u);
    stage_chunk(counting_uuid, 1u);
    stage_chunk(counting_uuid, 3u);
    assert(!commit_uuid(counting_uuid));
    assert_nothing_parked();

    /* The failed attempt consumed the stage, so supplying the missing chunk
     * afterwards is not enough. */
    stage_chunk(counting_uuid, 2u);
    assert(!commit_uuid(counting_uuid));
    assert_nothing_parked();
}

static void test_wrong_crc_writes_nothing_and_clears_the_stage(void)
{
    rr_sensor_bus_provision_reset();
    stage_all(counting_uuid);
    assert(!commit_with((uint8_t)(counting_uuid_crc ^ 0x01u), true));
    assert_nothing_parked();

    assert(!commit_uuid(counting_uuid));
    assert_nothing_parked();
}

static void test_commit_padding_must_be_zero(void)
{
    const uint8_t data[4] = {0x01, 0, 0, counting_uuid_crc};

    rr_sensor_bus_provision_reset();
    stage_all(counting_uuid);
    assert(!rr_sensor_bus_provision_write(COMMIT, data, 4u, true));
    assert_nothing_parked();
}

/* The torn stage from the design: one attempt stages chunks 0-2 and dies, a
 * second stages only chunk 3 of a different UUID and commits it. The mask
 * reads complete; the CRC, computed over the UUID the second host meant, does
 * not match the splice. */
static void test_a_spliced_stage_fails_the_crc(void)
{
    uint8_t other_uuid[16];

    for (unsigned int index = 0; index < sizeof(other_uuid); ++index) {
        other_uuid[index] = (uint8_t)(0xa0u + index);
    }

    rr_sensor_bus_provision_reset();
    stage_chunk(counting_uuid, 0u);
    stage_chunk(counting_uuid, 1u);
    stage_chunk(counting_uuid, 2u);
    stage_chunk(other_uuid, 3u);
    assert(!commit_uuid(other_uuid));
    assert(!commit_uuid(counting_uuid));
    assert_nothing_parked();
}

static void test_writing_chunk_zero_clears_the_stage(void)
{
    uint8_t spliced[16];
    uint8_t uuid[16];
    uint8_t other_uuid[16];

    for (unsigned int index = 0; index < sizeof(other_uuid); ++index) {
        other_uuid[index] = (uint8_t)(0xa0u + index);
    }

    /* Leftovers from an abandoned attempt, then chunk 0 alone: even a CRC
     * computed over exactly what the buffer would hold is refused, because
     * chunk 0 emptied the mask. */
    rr_sensor_bus_provision_reset();
    stage_chunk(other_uuid, 1u);
    stage_chunk(other_uuid, 2u);
    stage_chunk(other_uuid, 3u);
    stage_chunk(counting_uuid, 0u);
    memcpy(spliced, other_uuid, sizeof(spliced));
    memcpy(spliced, counting_uuid, 4u);
    assert(!commit_uuid(spliced));
    assert_nothing_parked();

    /* A full sequence starting at chunk 0 commits cleanly over leftovers. */
    stage_chunk(other_uuid, 1u);
    stage_chunk(other_uuid, 2u);
    stage_all(counting_uuid);
    assert(commit_uuid(counting_uuid));
    assert(rr_sensor_bus_provision_take_commit(uuid));
    assert(memcmp(uuid, counting_uuid, sizeof(uuid)) == 0);
}

static void test_writes_of_the_wrong_length_are_ignored(void)
{
    const uint8_t five[5] = {0x00, 0x01, 0x02, 0x03, 0x04};

    rr_sensor_bus_provision_reset();
    assert(!rr_sensor_bus_provision_write(CHUNK0, counting_uuid, 3u, true));
    stage_chunk(counting_uuid, 1u);
    stage_chunk(counting_uuid, 2u);
    stage_chunk(counting_uuid, 3u);
    assert(!commit_uuid(counting_uuid));

    stage_all(counting_uuid);
    assert(!rr_sensor_bus_provision_write(COMMIT, five, 5u, true));
    /* The oversized commit was not an attempt, so the stage survives it. */
    assert(commit_uuid(counting_uuid));
    rr_sensor_bus_provision_reset();
}

static void test_other_registers_are_not_claimed(void)
{
    uint8_t uuid[16];

    rr_sensor_bus_provision_reset();
    stage_all(counting_uuid);
    assert(!rr_sensor_bus_provision_write(0x4f, counting_uuid, 4u, true));
    assert(!rr_sensor_bus_provision_write(0x55, counting_uuid, 4u, true));
    assert(!rr_sensor_bus_provision_write(0x10, counting_uuid, 4u, true));
    assert(commit_uuid(counting_uuid));
    assert(rr_sensor_bus_provision_take_commit(uuid));
}

static void test_reset_drops_a_parked_commit(void)
{
    rr_sensor_bus_provision_reset();
    stage_all(counting_uuid);
    assert(commit_uuid(counting_uuid));
    rr_sensor_bus_provision_reset();
    assert_nothing_parked();
}

int main(void)
{
    test_crc_matches_the_golden_values();
    test_the_committed_uuid_names_the_expected_serial();
    test_a_complete_stage_commits_once();
    test_chunks_after_zero_may_arrive_in_any_order();
    test_a_provisioned_board_refuses_every_write();
    test_commit_with_a_missing_chunk_writes_nothing();
    test_wrong_crc_writes_nothing_and_clears_the_stage();
    test_commit_padding_must_be_zero();
    test_a_spliced_stage_fails_the_crc();
    test_writing_chunk_zero_clears_the_stage();
    test_writes_of_the_wrong_length_are_ignored();
    test_other_registers_are_not_claimed();
    test_reset_drops_a_parked_commit();
    puts("sensor bus provision tests passed");
    return 0;
}
