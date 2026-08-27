#include "probe.hpp"

#include <sstream>

namespace portable {

namespace {
constexpr std::uint64_t kFnvOffsetBasis = 14695981039346656037ULL;
constexpr std::uint64_t kFnvPrime = 1099511628211ULL;
}  // namespace

BuildInfo build_info() {
    std::ostringstream compiler;
#if defined(__clang__)
    compiler << "Clang " << __clang_major__ << "." << __clang_minor__;
#elif defined(__GNUC__)
    compiler << "GNU " << __GNUC__ << "." << __GNUC_MINOR__;
#elif defined(_MSC_VER)
    compiler << "MSVC " << _MSC_VER;
#else
    compiler << "unknown";
#endif
    return BuildInfo{compiler.str(), std::to_string(__cplusplus), "0.1.0"};
}

std::uint64_t checksum(const std::vector<std::int64_t>& values) {
    std::uint64_t hash = kFnvOffsetBasis;
    for (const std::int64_t value : values) {
        // Reinterpret through the unsigned domain so the result does not depend
        // on the platform's signed representation.
        const auto bits = static_cast<std::uint64_t>(value);
        for (int shift = 0; shift < 64; shift += 8) {
            hash ^= (bits >> shift) & 0xFFULL;
            hash *= kFnvPrime;
        }
    }
    return hash;
}

}  // namespace portable
