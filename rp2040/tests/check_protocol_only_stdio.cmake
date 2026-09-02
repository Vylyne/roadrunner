if(NOT DEFINED ROADRUNNER_STDIO_USB_SOURCES)
    message(FATAL_ERROR "ROADRUNNER_STDIO_USB_SOURCES was not provided")
endif()

foreach(roadrunner_source ${ROADRUNNER_STDIO_USB_SOURCES})
    if(NOT EXISTS "${roadrunner_source}")
        message(FATAL_ERROR "Source file does not exist: ${roadrunner_source}")
    endif()

    file(READ "${roadrunner_source}" roadrunner_source_contents)
    string(FIND "${roadrunner_source_contents}" "printf(" printf_offset)

    if(NOT printf_offset EQUAL -1)
        message(FATAL_ERROR
            "${roadrunner_source} writes diagnostic text to the shared CDC "
            "protocol endpoint (every Roadrunner image enables USB CDC "
            "stdio, so printf() output can collide with admin protocol "
            "frames on the same interface)")
    endif()
endforeach()
