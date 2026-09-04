cmake_minimum_required(VERSION 3.13)

project(roadrunner_identity_tests C)

enable_testing()

add_executable(roadrunner_identity_tests
    test_identity_record.c
    ../identity_record.c
    ../identity_store.c
    ../usb_admin.c
    ../usb_descriptor_strings.c
)
target_include_directories(roadrunner_identity_tests PRIVATE ..)

add_test(NAME identity_record COMMAND roadrunner_identity_tests)

add_executable(roadrunner_identity_register_tests
    test_identity_registers.c
    ../identity_registers.c
    ../identity_record.c
    ../usb_descriptor_strings.c
)
target_include_directories(roadrunner_identity_register_tests PRIVATE ..)

add_test(NAME identity_registers COMMAND roadrunner_identity_register_tests)

# The I2C target's register-serving state machine, driven off-target.
#
# `i2c_target.c` is not listed as a source: the test #includes it, because the
# handler is file-static and this keeps the firmware free of test-only
# visibility changes. `stubs/` must precede `..` on the include path so its
# minimal <pico/stdlib.h>, <hardware/i2c.h> and <pico/i2c_slave.h> stand in for
# the SDK headers, which are unavailable and unwanted on the host.
add_executable(roadrunner_i2c_target_tests
    test_i2c_target.c
)
target_include_directories(roadrunner_i2c_target_tests PRIVATE stubs ..)

add_test(NAME i2c_target COMMAND roadrunner_i2c_target_tests)

set(ROADRUNNER_STDIO_USB_SOURCES
    ${CMAKE_CURRENT_LIST_DIR}/../main.c
    ${CMAKE_CURRENT_LIST_DIR}/../as5600.c
    ${CMAKE_CURRENT_LIST_DIR}/../i2c_target.c
    ${CMAKE_CURRENT_LIST_DIR}/../identity_record.c
    ${CMAKE_CURRENT_LIST_DIR}/../identity_registers.c
    ${CMAKE_CURRENT_LIST_DIR}/../identity_store.c
    ${CMAKE_CURRENT_LIST_DIR}/../ir_sensor.c
    ${CMAKE_CURRENT_LIST_DIR}/../neopixel.c
    ${CMAKE_CURRENT_LIST_DIR}/../tmcuart.c
    ${CMAKE_CURRENT_LIST_DIR}/../usb_admin.c
    ${CMAKE_CURRENT_LIST_DIR}/../usb_descriptor_strings.c
    ${CMAKE_CURRENT_LIST_DIR}/../usb_descriptors.c
    ${CMAKE_CURRENT_LIST_DIR}/../usbserial.c
)

add_test(
    NAME protocol_only_cdc
    COMMAND ${CMAKE_COMMAND}
        "-DROADRUNNER_STDIO_USB_SOURCES=${ROADRUNNER_STDIO_USB_SOURCES}"
        -P ${CMAKE_CURRENT_LIST_DIR}/check_protocol_only_stdio.cmake
)
