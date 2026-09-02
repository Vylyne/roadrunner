file(READ "${ROADRUNNER_MAIN}" roadrunner_main)
string(FIND "${roadrunner_main}" "printf(" printf_offset)

if(NOT printf_offset EQUAL -1)
    message(FATAL_ERROR
        "main.c writes diagnostic text to the shared CDC protocol endpoint")
endif()
