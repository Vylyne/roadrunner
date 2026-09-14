#ifndef ROADRUNNER_BOOT_MARKER_H
#define ROADRUNNER_BOOT_MARKER_H

#include <stdint.h>

/* Register 0x25: milliseconds since boot, so the host can tell "the board
 * reset" from "the bus went quiet" - a reset is the only way this value goes
 * down.
 *
 * It saturates instead of wrapping. A uint32_t of milliseconds wraps at ~49.7
 * days, and a wrap is indistinguishable from a reset. The cap is one below
 * 0xffffffff so a running board never collides with the 0xff fill a locked
 * board answers with. */
#define RR_BOOT_MARKER_MAX_MS 0xfffffffeu

static inline uint32_t rr_boot_marker_ms(uint64_t now_us)
{
    uint64_t ms = now_us / 1000u;
    return ms > RR_BOOT_MARKER_MAX_MS ? RR_BOOT_MARKER_MAX_MS : (uint32_t)ms;
}

#endif
