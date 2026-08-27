// portable -- native scaffolding.
//
// The single exported computation, kept deliberately trivial: its job is to
// prove the toolchain end to end, not to be fast at anything.
//
// It is also the template. Any real C++ added later must satisfy the fallback
// contract in docs/architecture.md §10 and ADR 0008:
//
//   * a pure-Python reference implementation, written FIRST and normative;
//   * a differential test comparing the two over generated inputs;
//   * no native-only functionality, ever;
//   * no Decimal across this boundary -- money moves as integers or strings
//     with an explicit, tested conversion, never as double. CLAUDE.md
//     invariant 1 does not stop at the language boundary, and the total
//     absence of `double` and `float` from this header is the point.

#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace portable {

/// Build provenance, so a number can be traced to the code that produced it
/// (PORT-GIPS-J03).
struct BuildInfo {
    std::string compiler;      ///< e.g. "GNU 13.3.0"
    std::string cxx_standard;  ///< e.g. "201703"
    std::string module_version;
};

BuildInfo build_info();

/// A deterministic checksum over 64-bit integers.
///
/// FNV-1a, chosen because it is exactly specified, has no floating point in
/// it, and gives identical results on every platform -- which is what makes it
/// usable as the differential test's subject. Determinism is the property
/// being demonstrated here (CLAUDE.md invariant 6, PORT-GIPS-J06), not speed.
std::uint64_t checksum(const std::vector<std::int64_t>& values);

}  // namespace portable
