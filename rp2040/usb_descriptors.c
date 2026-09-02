#include "identity_store.h"
#include "usb_descriptor_strings.h"

#include "pico/unique_id.h"
#include "tusb.h"

enum {
    RR_USB_STRING_LANGUAGE = 0,
    RR_USB_STRING_MANUFACTURER,
    RR_USB_STRING_PRODUCT,
    RR_USB_STRING_SERIAL,
    RR_USB_STRING_CDC,
    RR_USB_INTERFACE_COUNT = 2,
};

#define RR_USB_CONFIGURATION_LENGTH (TUD_CONFIG_DESC_LEN + TUD_CDC_DESC_LEN)

static const tusb_desc_device_t rr_usb_device_descriptor = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,
    .bDeviceClass = TUSB_CLASS_MISC,
    .bDeviceSubClass = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol = MISC_PROTOCOL_IAD,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor = 0x2e8a,
    .idProduct = 0x000a,
    .bcdDevice = 0x0100,
    .iManufacturer = RR_USB_STRING_MANUFACTURER,
    .iProduct = RR_USB_STRING_PRODUCT,
    .iSerialNumber = RR_USB_STRING_SERIAL,
    .bNumConfigurations = 1,
};

static const uint8_t rr_usb_configuration_descriptor[RR_USB_CONFIGURATION_LENGTH] = {
    TUD_CONFIG_DESCRIPTOR(1, RR_USB_INTERFACE_COUNT, 0,
                          RR_USB_CONFIGURATION_LENGTH, 0, 100),
    TUD_CDC_DESCRIPTOR(0, RR_USB_STRING_CDC, 0x81, 8, 0x02, 0x82, 64),
};

static struct rr_usb_descriptor_strings rr_usb_strings;

void rr_usb_descriptors_init(const struct rr_identity_store *store) {
    struct rr_identity identity;
    pico_unique_board_id_t board_id;
    rr_identity_status_t status = rr_identity_load(store, &identity);

    pico_get_unique_board_id(&board_id);
    rr_usb_descriptor_strings_build(&rr_usb_strings, status, &identity,
                                    board_id.id);
    tusb_init();
}

const uint8_t *tud_descriptor_device_cb(void) {
    return (const uint8_t *)&rr_usb_device_descriptor;
}

const uint8_t *tud_descriptor_configuration_cb(uint8_t index) {
    (void)index;
    return rr_usb_configuration_descriptor;
}

const uint16_t *tud_descriptor_string_cb(uint8_t index, uint16_t langid) {
    static const char *const strings[] = {
        NULL,
        RR_USB_MANUFACTURER,
        RR_USB_PRODUCT,
        rr_usb_strings.serial,
        "Roadrunner CDC",
    };
    static uint16_t descriptor[RR_USB_SERIAL_MAX_LENGTH + 1u];
    const char *string;
    uint8_t length = 0;

    (void)langid;
    if (index == RR_USB_STRING_LANGUAGE) {
        descriptor[1] = 0x0409;
        length = 1;
    } else {
        if (index >= sizeof(strings) / sizeof(strings[0])) {
            return NULL;
        }
        string = strings[index];
        while (string[length] != '\0'
               && length < RR_USB_SERIAL_MAX_LENGTH) {
            descriptor[1u + length] = (uint8_t)string[length];
            ++length;
        }
    }
    descriptor[0] = (uint16_t)((TUSB_DESC_STRING << 8u) | (length * 2u + 2u));
    return descriptor;
}
