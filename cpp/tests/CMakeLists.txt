add_executable(portable_cpp_tests test_probe.cpp)
target_link_libraries(portable_cpp_tests PRIVATE portable_probe Catch2::Catch2WithMain)

include(Catch)
catch_discover_tests(portable_cpp_tests)
