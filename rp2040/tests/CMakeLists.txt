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

add_test(
    NAME protocol_only_cdc
    COMMAND ${CMAKE_COMMAND}
        -DROADRUNNER_MAIN=${CMAKE_CURRENT_LIST_DIR}/../main.c
        -P ${CMAKE_CURRENT_LIST_DIR}/check_protocol_only_stdio.cmake
)
