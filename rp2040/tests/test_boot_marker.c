#include "boot_marker.h"

#include <assert.h>
#include <stdint.h>
#include <stdio.h>

static void test_counts_milliseconds_since_boot(void)
{
    assert(rr_boot_marker_ms(0u) == 0u);
    assert(rr_boot_marker_ms(999u) == 0u);
    assert(rr_boot_marker_ms(1000u) == 1u);
    assert(rr_boot_marker_ms(1100000u) == 1100u);
}

static void test_saturates_instead_of_wrapping(void)
{
    uint64_t wrap_us = (UINT64_C(1) << 32) * 1000u;

    assert(rr_boot_marker_ms(UINT64_C(0xfffffffe) * 1000u) == 0xfffffffeu);
    assert(rr_boot_marker_ms(UINT64_C(0xffffffff) * 1000u) == 0xfffffffeu);
    assert(rr_boot_marker_ms(wrap_us) == 0xfffffffeu);
    assert(rr_boot_marker_ms(wrap_us + 5000u) == 0xfffffffeu);
    assert(rr_boot_marker_ms(UINT64_MAX) == 0xfffffffeu);
}

static void test_never_decreases_across_the_cap(void)
{
    uint64_t now_us = (UINT64_C(0xfffffff0) * 1000u);
    uint32_t previous = rr_boot_marker_ms(now_us);

    for (int i = 0; i < 64; ++i) {
        now_us += 500u * 1000u;
        uint32_t marker = rr_boot_marker_ms(now_us);
        assert(marker >= previous);
        assert(marker != 0xffffffffu);
        previous = marker;
    }
}

int main(void)
{
    test_counts_milliseconds_since_boot();
    test_saturates_instead_of_wrapping();
    test_never_decreases_across_the_cap();
    puts("boot marker tests passed");
    return 0;
}
