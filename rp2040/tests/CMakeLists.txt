cmake_minimum_required(VERSION 3.13)

project(roadrunner_identity_tests C)

enable_testing()

add_executable(roadrunner_identity_tests
    test_identity_record.c
    ../identity_record.c
    ../identity_store.c
    ../usb_descriptor_strings.c
)
target_include_directories(roadrunner_identity_tests PRIVATE ..)

add_test(NAME identity_record COMMAND roadrunner_identity_tests)
