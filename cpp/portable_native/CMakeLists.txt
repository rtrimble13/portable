pybind11_add_module(portable_native MODULE
    probe.cpp
    bindings.cpp
)

target_include_directories(portable_native PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
target_compile_definitions(portable_native PRIVATE VERSION_INFO=${PROJECT_VERSION})

# /Zc:__cplusplus is required, not cosmetic: without it MSVC reports
# __cplusplus as 199711L regardless of /std:c++17, for backward compatibility.
# build_info() reads __cplusplus, so the Catch2 test asserting a C++17-or-later
# standard failed on Windows and passed everywhere else -- a real difference in
# what the binary reports about itself, on the platform the owner works on.
if(MSVC)
    target_compile_options(portable_native PRIVATE /W4 /permissive- /Zc:__cplusplus)
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
# The Catch2 suite links this rather than the module, so it needs the same
# flag -- otherwise the test binary and the extension disagree about what
# __cplusplus is, which is how this was missed.
if(MSVC)
    target_compile_options(portable_probe PUBLIC /Zc:__cplusplus)
endif()
