#include "usb_descriptor_strings.h"

#include <string.h>

void rr_usb_descriptor_strings_build(
    struct rr_usb_descriptor_strings *strings,
    rr_identity_status_t identity_status,
    const struct rr_identity *identity) {
    static const char unprovisioned[] = "RR-UNPROVISIONED";

    if (strings == NULL) {
        return;
    }
    if (identity_status == RR_IDENTITY_OK && identity != NULL) {
        rr_identity_serial(identity->uuid, strings->serial);
        return;
    }

    /* No flash UID suffix: boards from one batch share a UID, so a suffix
     * disguised the collision rather than preventing it. See the amendment in
     * docs/roadrunner-uart-chunked-identity-design.md. */
    memcpy(strings->serial, unprovisioned, sizeof(unprovisioned));
}
