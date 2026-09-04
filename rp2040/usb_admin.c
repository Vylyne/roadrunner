#include "usb_admin.h"

#include <string.h>

#if defined(PICO_ON_DEVICE) && PICO_ON_DEVICE
#include "pico/bootrom.h"
#include "tusb.h"
#endif

enum {
    RR_USB_ADMIN_SYNC = 0x52,
    RR_USB_ADMIN_VERSION = 0x01,
    RR_USB_ADMIN_RESPONSE = 0x80,
    RR_USB_ADMIN_MAX_PAYLOAD = 64,
    RR_USB_ADMIN_MAX_RESPONSE_PAYLOAD = 96,
    RR_USB_ADMIN_MAX_VERSION_LENGTH = 32,
};

enum rr_usb_admin_parser_state {
    RR_USB_ADMIN_WAIT_SYNC,
    RR_USB_ADMIN_WAIT_SECOND_SYNC,
    RR_USB_ADMIN_WAIT_VERSION,
    RR_USB_ADMIN_WAIT_HEADER,
    RR_USB_ADMIN_WAIT_BODY,
    RR_USB_ADMIN_DISCARD_OVERSIZE,
};

static const char rr_usb_admin_model[] = "roadrunner-v1";
static struct rr_usb_admin_config rr_usb_admin_config;
static enum rr_usb_admin_parser_state rr_usb_admin_state;
static uint8_t rr_usb_admin_frame[5u + RR_USB_ADMIN_MAX_PAYLOAD];
static size_t rr_usb_admin_frame_length;
static size_t rr_usb_admin_expected_length;
static uint16_t rr_usb_admin_discard_remaining;
#if defined(PICO_ON_DEVICE) && PICO_ON_DEVICE
static volatile bool rr_usb_admin_tx_complete;

void tud_cdc_tx_complete_cb(uint8_t interface_number) {
    if (interface_number == 0u) {
        rr_usb_admin_tx_complete = true;
    }
}
#endif

static uint8_t rr_usb_admin_crc8(const uint8_t *data, size_t length) {
    uint8_t crc = 0;

    for (size_t index = 0; index < length; ++index) {
        crc ^= data[index];
        for (unsigned int bit = 0; bit < 8u; ++bit) {
            crc = (uint8_t)((crc << 1u)
                            ^ ((crc & 0x80u) != 0u ? 0x07u : 0u));
        }
    }
    return crc;
}

static size_t rr_usb_admin_string_length(const char *string, size_t maximum) {
    size_t length = 0;

    if (string == NULL) {
        return 0;
    }
    while (length < maximum && string[length] != '\0') {
        ++length;
    }
    return length;
}

static void rr_usb_admin_write(const uint8_t *data, size_t length) {
    if (rr_usb_admin_config.write != NULL) {
        rr_usb_admin_config.write(rr_usb_admin_config.context, data, length);
        return;
    }
#if defined(PICO_ON_DEVICE) && PICO_ON_DEVICE
    while (length != 0u) {
        uint32_t written = tud_cdc_write(data, (uint32_t)length);

        if (written == 0u) {
            tud_cdc_write_flush();
            tud_task();
            continue;
        }
        data += written;
        length -= written;
    }
    tud_cdc_write_flush();
#else
    (void)data;
    (void)length;
#endif
}

static void rr_usb_admin_flush(void) {
    if (rr_usb_admin_config.flush != NULL) {
        rr_usb_admin_config.flush(rr_usb_admin_config.context);
        return;
    }
#if defined(PICO_ON_DEVICE) && PICO_ON_DEVICE
    tud_cdc_write_flush();
#endif
}

static bool rr_usb_admin_transmit_complete(void) {
    if (rr_usb_admin_config.transmit_complete != NULL) {
        return rr_usb_admin_config.transmit_complete(rr_usb_admin_config.context);
    }
#if defined(PICO_ON_DEVICE) && PICO_ON_DEVICE
    return rr_usb_admin_tx_complete
        && tud_cdc_write_available() == CFG_TUD_CDC_TX_BUFSIZE;
#else
    return true;
#endif
}

static void rr_usb_admin_reboot_bootsel(void) {
    if (rr_usb_admin_config.reboot_bootsel != NULL) {
        rr_usb_admin_config.reboot_bootsel(rr_usb_admin_config.context);
        return;
    }
#if defined(PICO_ON_DEVICE) && PICO_ON_DEVICE
    reset_usb_boot(0, 0);
#endif
}

static void rr_usb_admin_reboot_application(void) {
    if (rr_usb_admin_config.reboot_application != NULL) {
        rr_usb_admin_config.reboot_application(rr_usb_admin_config.context);
    }
}

static void rr_usb_admin_send_response(uint8_t opcode, const uint8_t *payload,
                                       size_t payload_length) {
    uint8_t response[5u + RR_USB_ADMIN_MAX_RESPONSE_PAYLOAD + 1u];
    size_t frame_length = 5u + payload_length;

    if (payload_length > RR_USB_ADMIN_MAX_RESPONSE_PAYLOAD) {
        return;
    }
    response[0] = RR_USB_ADMIN_SYNC;
    response[1] = RR_USB_ADMIN_SYNC;
    response[2] = RR_USB_ADMIN_VERSION;
    response[3] = (uint8_t)(opcode | RR_USB_ADMIN_RESPONSE);
    response[4] = (uint8_t)payload_length;
    if (payload_length != 0u) {
        memcpy(response + 5u, payload, payload_length);
    }
    response[frame_length] = rr_usb_admin_crc8(response, frame_length);
    rr_usb_admin_write(response, frame_length + 1u);
}

static void rr_usb_admin_send_status(uint8_t opcode, uint8_t status) {
    rr_usb_admin_send_response(opcode, &status, 1u);
}

static uint8_t rr_usb_admin_identity_status(rr_identity_status_t status) {
    switch (status) {
    case RR_IDENTITY_OK:
        return RR_USB_ADMIN_OK;
    case RR_IDENTITY_ALREADY_PROVISIONED:
        return RR_USB_ADMIN_ALREADY_PROVISIONED;
    case RR_IDENTITY_CONFLICT:
        return RR_USB_ADMIN_IDENTITY_CONFLICT;
    case RR_IDENTITY_IO_ERROR:
    case RR_IDENTITY_NONE:
    default:
        return RR_USB_ADMIN_IO_ERROR;
    }
}

static void rr_usb_admin_acknowledge_before_application_reboot(
    uint8_t opcode, const uint8_t *payload, size_t payload_length) {
#if defined(PICO_ON_DEVICE) && PICO_ON_DEVICE
    rr_usb_admin_tx_complete = false;
#endif
    rr_usb_admin_send_response(opcode, payload, payload_length);
    rr_usb_admin_flush();
    while (!rr_usb_admin_transmit_complete()) {
#if defined(PICO_ON_DEVICE) && PICO_ON_DEVICE
        tud_task();
#endif
    }
    rr_usb_admin_reboot_application();
}

static void rr_usb_admin_make_serial(char serial[35]) {
    static const char hexadecimal[] = "0123456789ABCDEF";

    if (rr_usb_admin_config.identity_status == RR_IDENTITY_OK
        && rr_usb_admin_config.identity != NULL) {
        rr_identity_serial(rr_usb_admin_config.identity->uuid, serial);
        return;
    }

    memcpy(serial, "RR-UNPROVISIONED-", 17u);
    for (size_t index = 0; index < RR_USB_ADMIN_FLASH_UID_SIZE; ++index) {
        uint8_t byte = rr_usb_admin_config.flash_uid == NULL ? 0u
            : rr_usb_admin_config.flash_uid[index];
        serial[17u + index * 2u] = hexadecimal[byte >> 4u];
        serial[18u + index * 2u] = hexadecimal[byte & 0x0fu];
    }
    serial[17u + RR_USB_ADMIN_FLASH_UID_SIZE * 2u] = '\0';
}

static void rr_usb_admin_send_info(void) {
    uint8_t payload[RR_USB_ADMIN_MAX_RESPONSE_PAYLOAD];
    char serial[35];
    size_t version_length = rr_usb_admin_string_length(
        rr_usb_admin_config.firmware_version, RR_USB_ADMIN_MAX_VERSION_LENGTH);
    size_t serial_length;
    size_t length = 0;

    rr_usb_admin_make_serial(serial);
    serial_length = rr_usb_admin_string_length(serial, sizeof(serial) - 1u);

    payload[length++] = rr_usb_admin_config.identity_status == RR_IDENTITY_OK
        ? RR_USB_ADMIN_OK : RR_USB_ADMIN_UNPROVISIONED;
    payload[length++] = (uint8_t)rr_usb_admin_config.identity_status;
    payload[length++] = rr_usb_admin_config.transport;
    payload[length++] = rr_usb_admin_config.led_order;
    payload[length++] = sizeof(rr_usb_admin_model) - 1u;
    memcpy(payload + length, rr_usb_admin_model, sizeof(rr_usb_admin_model) - 1u);
    length += sizeof(rr_usb_admin_model) - 1u;
    payload[length++] = (uint8_t)version_length;
    if (version_length != 0u) {
        memcpy(payload + length, rr_usb_admin_config.firmware_version, version_length);
        length += version_length;
    }
    payload[length++] = (uint8_t)serial_length;
    memcpy(payload + length, serial, serial_length);
    length += serial_length;
    if (rr_usb_admin_config.flash_uid != NULL) {
        memcpy(payload + length, rr_usb_admin_config.flash_uid,
               RR_USB_ADMIN_FLASH_UID_SIZE);
    } else {
        memset(payload + length, 0, RR_USB_ADMIN_FLASH_UID_SIZE);
    }
    length += RR_USB_ADMIN_FLASH_UID_SIZE;
    rr_usb_admin_send_response(RR_USB_ADMIN_INFO, payload, length);
}

static void rr_usb_admin_provision_identity(const uint8_t *uuid) {
    struct rr_identity identity;
    uint8_t payload[2u + RR_IDENTITY_SERIAL_LENGTH];
    char serial[RR_IDENTITY_SERIAL_LENGTH + 1];
    rr_identity_status_t status = rr_identity_provision(
        rr_usb_admin_config.identity_store, uuid);

    if (status != RR_IDENTITY_OK) {
        rr_usb_admin_send_status(RR_USB_ADMIN_PROVISION_UUID,
                                 rr_usb_admin_identity_status(status));
        return;
    }
    status = rr_identity_load(rr_usb_admin_config.identity_store, &identity);
    if (status != RR_IDENTITY_OK) {
        rr_usb_admin_send_status(RR_USB_ADMIN_PROVISION_UUID,
                                 rr_usb_admin_identity_status(status));
        return;
    }
    rr_identity_serial(identity.uuid, serial);
    payload[0] = RR_USB_ADMIN_OK;
    payload[1] = RR_IDENTITY_SERIAL_LENGTH;
    memcpy(payload + 2u, serial, RR_IDENTITY_SERIAL_LENGTH);
    rr_usb_admin_acknowledge_before_application_reboot(
        RR_USB_ADMIN_PROVISION_UUID, payload, sizeof(payload));
}

static void rr_usb_admin_clear_identity(void) {
    uint8_t payload = RR_USB_ADMIN_OK;
    rr_identity_status_t status = rr_identity_clear(
        rr_usb_admin_config.identity_store);

    if (status != RR_IDENTITY_OK) {
        rr_usb_admin_send_status(RR_USB_ADMIN_CLEAR_IDENTITY,
                                 rr_usb_admin_identity_status(status));
        return;
    }
    rr_usb_admin_acknowledge_before_application_reboot(
        RR_USB_ADMIN_CLEAR_IDENTITY, &payload, 1u);
}

static void rr_usb_admin_forward_frame(void) {
    if (rr_usb_admin_config.legacy_byte == NULL) {
        return;
    }
    for (size_t index = 0; index < rr_usb_admin_frame_length; ++index) {
        rr_usb_admin_config.legacy_byte(rr_usb_admin_config.context,
                                        rr_usb_admin_frame[index]);
    }
}

static void rr_usb_admin_reset_parser(void) {
    rr_usb_admin_state = RR_USB_ADMIN_WAIT_SYNC;
    rr_usb_admin_frame_length = 0;
    rr_usb_admin_expected_length = 0;
}

void rr_usb_admin_init(const struct rr_usb_admin_config *config) {
    memset(&rr_usb_admin_config, 0, sizeof(rr_usb_admin_config));
    if (config != NULL) {
        rr_usb_admin_config = *config;
    }
    rr_usb_admin_discard_remaining = 0;
    rr_usb_admin_reset_parser();
}

void rr_usb_admin_receive(uint8_t byte) {
    uint8_t opcode;

    switch (rr_usb_admin_state) {
    case RR_USB_ADMIN_WAIT_SYNC:
        if (byte == RR_USB_ADMIN_SYNC) {
            rr_usb_admin_frame[0] = byte;
            rr_usb_admin_frame_length = 1u;
            rr_usb_admin_state = RR_USB_ADMIN_WAIT_SECOND_SYNC;
        } else if (rr_usb_admin_config.legacy_byte != NULL) {
            rr_usb_admin_config.legacy_byte(rr_usb_admin_config.context, byte);
        }
        return;
    case RR_USB_ADMIN_WAIT_SECOND_SYNC:
        rr_usb_admin_frame[rr_usb_admin_frame_length++] = byte;
        if (byte == RR_USB_ADMIN_SYNC) {
            rr_usb_admin_state = RR_USB_ADMIN_WAIT_VERSION;
        } else {
            rr_usb_admin_forward_frame();
            rr_usb_admin_reset_parser();
        }
        return;
    case RR_USB_ADMIN_WAIT_VERSION:
        rr_usb_admin_frame[rr_usb_admin_frame_length++] = byte;
        if (byte == RR_USB_ADMIN_VERSION) {
            rr_usb_admin_state = RR_USB_ADMIN_WAIT_HEADER;
        } else {
            rr_usb_admin_forward_frame();
            rr_usb_admin_reset_parser();
        }
        return;
    case RR_USB_ADMIN_WAIT_HEADER:
        rr_usb_admin_frame[rr_usb_admin_frame_length++] = byte;
        if (rr_usb_admin_frame_length != 5u) {
            return;
        }
        opcode = rr_usb_admin_frame[3];
        rr_usb_admin_expected_length = rr_usb_admin_frame[4];
        if (rr_usb_admin_expected_length > RR_USB_ADMIN_MAX_PAYLOAD) {
            rr_usb_admin_send_status(opcode, RR_USB_ADMIN_BAD_LENGTH);
            rr_usb_admin_discard_remaining = (uint16_t)rr_usb_admin_expected_length + 1u;
            rr_usb_admin_state = RR_USB_ADMIN_DISCARD_OVERSIZE;
        } else {
            rr_usb_admin_state = RR_USB_ADMIN_WAIT_BODY;
        }
        return;
    case RR_USB_ADMIN_WAIT_BODY:
        if (rr_usb_admin_frame_length < 5u + rr_usb_admin_expected_length) {
            rr_usb_admin_frame[rr_usb_admin_frame_length++] = byte;
            return;
        }
        opcode = rr_usb_admin_frame[3];
        if (byte != rr_usb_admin_crc8(rr_usb_admin_frame,
                                      rr_usb_admin_frame_length)) {
            rr_usb_admin_send_status(opcode, RR_USB_ADMIN_BAD_CRC);
        } else if (opcode == RR_USB_ADMIN_INFO) {
            if (rr_usb_admin_expected_length != 0u) {
                rr_usb_admin_send_status(opcode, RR_USB_ADMIN_BAD_LENGTH);
            } else {
                rr_usb_admin_send_info();
            }
        } else if (opcode == RR_USB_ADMIN_REBOOT_BOOTSEL) {
            if (rr_usb_admin_expected_length != 0u) {
                rr_usb_admin_send_status(opcode, RR_USB_ADMIN_BAD_LENGTH);
            } else {
#if defined(PICO_ON_DEVICE) && PICO_ON_DEVICE
                rr_usb_admin_tx_complete = false;
#endif
                rr_usb_admin_send_status(opcode, RR_USB_ADMIN_OK);
                rr_usb_admin_flush();
                while (!rr_usb_admin_transmit_complete()) {
#if defined(PICO_ON_DEVICE) && PICO_ON_DEVICE
                    tud_task();
#endif
                }
                rr_usb_admin_reboot_bootsel();
            }
        } else if (opcode == RR_USB_ADMIN_PROVISION_UUID) {
            if (rr_usb_admin_expected_length != RR_IDENTITY_UUID_SIZE) {
                rr_usb_admin_send_status(opcode, RR_USB_ADMIN_BAD_LENGTH);
            } else {
                rr_usb_admin_provision_identity(rr_usb_admin_frame + 5u);
            }
        } else if (opcode == RR_USB_ADMIN_CLEAR_IDENTITY) {
            if (rr_usb_admin_expected_length != 4u) {
                rr_usb_admin_send_status(opcode, RR_USB_ADMIN_BAD_LENGTH);
            } else if (memcmp(rr_usb_admin_frame + 5u, "RRCL", 4u) != 0) {
                rr_usb_admin_send_status(opcode,
                                         RR_USB_ADMIN_CONFIRMATION_REQUIRED);
            } else if (rr_usb_admin_config.identity_status
                       == RR_IDENTITY_NONE) {
                /* Refused only for RR_IDENTITY_NONE: the one state with
                 * genuinely nothing to erase. IO_ERROR is admitted because
                 * it is sometimes recoverable (a verify-stage failure with
                 * working reads still reaches the erase). CONFLICT is also
                 * admitted, but the store itself refuses to erase it -
                 * REBOOT_BOOTSEL is the real recovery path there. */
                rr_usb_admin_send_status(opcode, RR_USB_ADMIN_UNPROVISIONED);
            } else {
                rr_usb_admin_clear_identity();
            }
        } else {
            rr_usb_admin_send_status(opcode, RR_USB_ADMIN_BAD_LENGTH);
        }
        rr_usb_admin_reset_parser();
        return;
    case RR_USB_ADMIN_DISCARD_OVERSIZE:
        --rr_usb_admin_discard_remaining;
        if (rr_usb_admin_discard_remaining == 0u) {
            rr_usb_admin_reset_parser();
        }
        return;
    }
}

void rr_usb_admin_poll(void) {
#if defined(PICO_ON_DEVICE) && PICO_ON_DEVICE
    while (tud_cdc_available() != 0u) {
        rr_usb_admin_receive((uint8_t)tud_cdc_read_char());
    }
#endif
}
