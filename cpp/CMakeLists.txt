# portable -- C++ build.
#
# Conventions deliberately mirror rtrimble13/po (`portopt`): CMake >= 3.20,
# C++17, position-independent code, FetchContent with shallow clones, and
# PORTABLE_BUILD_* option flags gating each component. ADR 0008 explains why:
# integrating portopt in v0.3 should be a merge, not a rewrite.
#
# v0.1 ships scaffolding only. There is no production C++ here by design --
# see docs/architecture.md §10 for the fallback contract that any future C++
# must satisfy.

cmake_minimum_required(VERSION 3.20)
project(portable_native
    VERSION 0.1.0
    DESCRIPTION "portable native extension scaffolding"
    LANGUAGES CXX
)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

option(PORTABLE_BUILD_NATIVE "Build the pybind11 extension module" ON)
option(PORTABLE_BUILD_TESTS  "Build the Catch2 test suite"        OFF)

# pybind11 resolution, in order:
#   1. the pip-installed pybind11 (what scikit-build-core gives us, no network)
#   2. FetchContent at po's pinned tag (what `make cpp` falls back to)
# Preferring the wheel keeps `pip install .` working on a machine with no
# network at configure time; the FetchContent arm keeps us aligned with po.
set(PORTABLE_PYBIND11_TAG "v2.12.0" CACHE STRING "pybind11 tag for the FetchContent fallback")

if(PORTABLE_BUILD_NATIVE)
    find_package(Python COMPONENTS Interpreter Development.Module REQUIRED)
    find_package(pybind11 CONFIG QUIET)
    if(NOT pybind11_FOUND)
        message(STATUS "portable: pybind11 not found as a package; fetching ${PORTABLE_PYBIND11_TAG}")
        include(FetchContent)
        FetchContent_Declare(pybind11
            GIT_REPOSITORY https://github.com/pybind/pybind11.git
            GIT_TAG        ${PORTABLE_PYBIND11_TAG}
            GIT_SHALLOW    TRUE
        )
        FetchContent_MakeAvailable(pybind11)
    else()
        message(STATUS "portable: using pybind11 ${pybind11_VERSION} from ${pybind11_DIR}")
    endif()

    add_subdirectory(portable_native)
endif()

if(PORTABLE_BUILD_TESTS)
    include(FetchContent)
    FetchContent_Declare(Catch2
        GIT_REPOSITORY https://github.com/catchorg/Catch2.git
        GIT_TAG        v3.5.4
        GIT_SHALLOW    TRUE
    )
    FetchContent_MakeAvailable(Catch2)
    enable_testing()
    add_subdirectory(tests)
endif()
