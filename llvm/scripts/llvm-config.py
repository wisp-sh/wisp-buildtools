#!/usr/bin/env python3
"""Relocatable build-host metadata interface for the packaged LLVM/WASI subset."""

import json
from pathlib import Path
import shlex
import sys


def main():
    prefix = Path(__file__).resolve().parent.parent
    metadata = json.loads((prefix / "share/llvm/sdk.json").read_text())
    arguments = sys.argv[1:]
    if not arguments or "--help" in arguments:
        print("usage: llvm-config [--link-static] OPTION... [COMPONENT...]")
        print("LLVM/WASI SDK metadata utility; libraries target " + metadata["host_target"])
        return
    options = [argument for argument in arguments if argument.startswith("--")]
    components = [argument.lower() for argument in arguments if not argument.startswith("--")] or ["all"]
    if "--link-shared" in options:
        raise ValueError("this SDK contains static Wasm libraries only")
    graph = metadata["components"]
    visited, ordered = set(), []

    def visit(component):
        if component in visited:
            return
        if component not in graph:
            raise ValueError("component is not packaged: " + component)
        visited.add(component)
        for dependency in graph[component]["dependencies"]:
            visit(dependency)
        library = graph[component]["library"]
        if library:
            ordered.append(library)

    for component in components:
        visit(component)
    libraries = list(reversed(ordered))
    include = "-I" + shlex.quote(str(prefix / "include"))
    values = {
        "--version": metadata["version"], "--prefix": str(prefix),
        "--bindir": str(prefix / "bin"), "--libdir": str(prefix / "lib"),
        "--includedir": str(prefix / "include"), "--cmakedir": str(prefix / "lib/cmake/WispLLVM"),
        "--host-target": metadata["host_target"], "--targets-built": " ".join(metadata["targets"]),
        "--build-mode": metadata["build_mode"], "--build-system": "cmake",
        "--assertion-mode": "ON" if metadata["assertions"] else "OFF",
        "--has-rtti": "YES" if metadata["rtti"] else "NO", "--shared-mode": "static",
        "--components": " ".join(sorted(graph)),
        "--cflags": include + " " + metadata["cflags"],
        "--cppflags": include + " " + metadata["cppflags"],
        "--cxxflags": include + " " + metadata["cxxflags"],
        "--ldflags": "-L" + shlex.quote(str(prefix / "lib")) + " " + metadata["ldflags"],
        "--system-libs": metadata["system_libs"],
        "--libs": " ".join("-l" + name for name in libraries),
        "--libnames": " ".join("lib" + name + ".a" for name in libraries),
        "--libfiles": " ".join(shlex.quote(str(prefix / "lib" / ("lib" + name + ".a"))) for name in libraries),
        "--src-root": str(prefix / "include"), "--obj-root": str(prefix),
        "--ignore-libllvm": None, "--link-static": None,
    }
    for option in options:
        if option not in values:
            raise ValueError("unsupported llvm-config option: " + option)
        if values[option] is not None:
            print(values[option].rstrip())


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        print("llvm-config: " + str(error), file=sys.stderr)
        sys.exit(1)
