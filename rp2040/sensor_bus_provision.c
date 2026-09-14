#include "sensor_bus_provision.h"

#include <string.h>

static struct {
    uint8_t stage[RR_SENSOR_BUS_PROVISION_UUID_SIZE];
    uint8_t present; /* bit N set once chunk N has been written */
    uint8_t committed[RR_SENSOR_BUS_PROVISION_UUID_SIZE];
    bool commit_pending;
} rr_sensor_bus_provision;

#define RR_SENSOR_BUS_PROVISION_ALL_PRESENT 0x0fu

uint8_t rr_sensor_bus_provision_crc8(const uint8_t *data, size_t length)
{
    uint8_t crc = 0;

    for (size_t index = 0; index < length; ++index) {
        crc ^= data[index];
        for (unsigned int bit = 0; bit < 8u; ++bit) {
            crc = (crc & 0x80u) ? (uint8_t)((crc << 1) ^ 0x07u)
                                : (uint8_t)(crc << 1);
        }
    }
    return crc;
}

static void rr_sensor_bus_provision_clear_stage(void)
{
    memset(rr_sensor_bus_provision.stage, 0,
           sizeof(rr_sensor_bus_provision.stage));
    rr_sensor_bus_provision.present = 0;
}

void rr_sensor_bus_provision_reset(void)
{
    rr_sensor_bus_provision_clear_stage();
    memset(rr_sensor_bus_provision.committed, 0,
           sizeof(rr_sensor_bus_provision.committed));
    rr_sensor_bus_provision.commit_pending = false;
}

bool rr_sensor_bus_provision_write(uint8_t reg, const uint8_t *data,
                                   size_t length, bool locked)
{
    bool valid;

    if (reg < RR_SENSOR_BUS_PROVISION_CHUNK_FIRST
        || reg > RR_SENSOR_BUS_PROVISION_COMMIT) {
        return false;
    }
    /* A provisioned board keeps its identity: the only way back to a blank
     * board is CLEAR_IDENTITY over the admin USB port. A torn or oversized
     * bus write is ignored rather than staged as garbage. */
    if (!locked || data == NULL || length != RR_SENSOR_BUS_PROVISION_WRITE_SIZE) {
        return false;
    }

    if (reg <= RR_SENSOR_BUS_PROVISION_CHUNK_LAST) {
        unsigned int chunk = reg - RR_SENSOR_BUS_PROVISION_CHUNK_FIRST;

        /* Starting over at chunk 0 discards whatever an earlier, abandoned
         * attempt left behind, before it can be spliced into this one. */
        if (chunk == 0u) {
            rr_sensor_bus_provision_clear_stage();
        }
        memcpy(rr_sensor_bus_provision.stage
                   + chunk * RR_SENSOR_BUS_PROVISION_WRITE_SIZE,
               data, RR_SENSOR_BUS_PROVISION_WRITE_SIZE);
        rr_sensor_bus_provision.present |= (uint8_t)(1u << chunk);
        return false;
    }

    /* COMMIT. The CRC is the host's, computed over the UUID it means to send,
     * so a stage spliced from two attempts fails here even with every chunk
     * present. Pass or fail, the stage is consumed. */
    valid = rr_sensor_bus_provision.present == RR_SENSOR_BUS_PROVISION_ALL_PRESENT
        && data[0] == 0u && data[1] == 0u && data[2] == 0u
        && data[3] == rr_sensor_bus_provision_crc8(
               rr_sensor_bus_provision.stage,
               sizeof(rr_sensor_bus_provision.stage));
    if (valid) {
        memcpy(rr_sensor_bus_provision.committed, rr_sensor_bus_provision.stage,
               sizeof(rr_sensor_bus_provision.committed));
        rr_sensor_bus_provision.commit_pending = true;
    }
    rr_sensor_bus_provision_clear_stage();
    return valid;
}

bool rr_sensor_bus_provision_take_commit(
    uint8_t uuid[RR_SENSOR_BUS_PROVISION_UUID_SIZE])
{
    if (!rr_sensor_bus_provision.commit_pending) {
        return false;
    }
    memcpy(uuid, rr_sensor_bus_provision.committed,
           sizeof(rr_sensor_bus_provision.committed));
    memset(rr_sensor_bus_provision.committed, 0,
           sizeof(rr_sensor_bus_provision.committed));
    rr_sensor_bus_provision.commit_pending = false;
    return true;
}
