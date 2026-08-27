#include <catch2/catch_test_macros.hpp>

#include "probe.hpp"

#include <cstdint>
#include <vector>

TEST_CASE("checksum is deterministic", "[probe]") {
    const std::vector<std::int64_t> values{1, 2, 3, 5, 8, 13};
    REQUIRE(portable::checksum(values) == portable::checksum(values));
}

TEST_CASE("checksum of the empty sequence is the FNV offset basis", "[probe]") {
    REQUIRE(portable::checksum({}) == 14695981039346656037ULL);
}

TEST_CASE("checksum is order-sensitive", "[probe]") {
    REQUIRE(portable::checksum({1, 2}) != portable::checksum({2, 1}));
}

TEST_CASE("checksum handles the full signed range without UB", "[probe]") {
    const std::vector<std::int64_t> extremes{
        INT64_MIN, -1, 0, 1, INT64_MAX,
    };
    REQUIRE(portable::checksum(extremes) == portable::checksum(extremes));
}

TEST_CASE("build_info reports a C++17-or-later standard", "[probe]") {
    const auto info = portable::build_info();
    REQUIRE(info.module_version == "0.1.0");
    REQUIRE(std::stol(info.cxx_standard) >= 201703L);
}
