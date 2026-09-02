#include "identity_record.h"
#include <assert.h>

int main(void) {
    assert(ROADRUNNER_IDENTITY_FLASH_OFFSET == 0x1FF000u);
    assert(ROADRUNNER_IDENTITY_SECTOR_SIZE == 0x1000u);
    assert(ROADRUNNER_APPLICATION_FLASH_SIZE == 0x1FF000u);
    return 0;
}
