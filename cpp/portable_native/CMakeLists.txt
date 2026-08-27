pybind11_add_module(portable_native MODULE
    probe.cpp
    bindings.cpp
)

target_include_directories(portable_native PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
target_compile_definitions(portable_native PRIVATE VERSION_INFO=${PROJECT_VERSION})

if(MSVC)
    target_compile_options(portable_native PRIVATE /W4 /permissive-)
else()
    target_compile_options(portable_native PRIVATE -Wall -Wextra -Wpedantic)
endif()

# scikit-build-core installs into the wheel root, so `import portable_native`
# works after `pip install -e .`.
install(TARGETS portable_native LIBRARY DESTINATION .)

# The pure C++ object library, so the Catch2 suite can link the same code the
# bindings expose rather than a copy of it.
add_library(portable_probe STATIC probe.cpp)
target_include_directories(portable_probe PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
