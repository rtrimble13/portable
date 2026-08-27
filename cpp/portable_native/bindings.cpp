#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include "probe.hpp"

namespace py = pybind11;

PYBIND11_MODULE(portable_native, m) {
    m.doc() =
        "portable native extension (scaffolding).\n\n"
        "Proves the CMake + pybind11 toolchain end to end. Every function here "
        "has a pure-Python twin in portable_core.native._reference, and a "
        "differential test asserts they agree. See ADR 0008.";

    m.attr("__version__") = "0.1.0";

    py::class_<portable::BuildInfo>(m, "BuildInfo")
        .def_readonly("compiler", &portable::BuildInfo::compiler)
        .def_readonly("cxx_standard", &portable::BuildInfo::cxx_standard)
        .def_readonly("module_version", &portable::BuildInfo::module_version)
        .def("__repr__", [](const portable::BuildInfo& b) {
            return "<BuildInfo compiler='" + b.compiler + "' cxx=" + b.cxx_standard + ">";
        });

    m.def("build_info", &portable::build_info,
          "Compiler, C++ standard, and module version for this build.");

    m.def("checksum", &portable::checksum, py::arg("values"),
          "FNV-1a checksum over 64-bit integers. Deterministic on every platform.");
}
