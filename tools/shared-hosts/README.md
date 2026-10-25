# Wisp host recipe used by CI

These source files are an exact copy of Wisp's `tools/shared-hosts` recipe.
`origin.json` records their owner and hashes. The prerequisite checker verifies
that inventory before CI uses them. This copy lets a buildtools-only checkout
provision Wisp's runtime without checking out Wisp or copying a developer's host.

The recipe builds pinned Wasmtime 50.0.0 with precompilation, GC-backed exceptions, and Component core-module imports from
`--preload`. The host belongs to Wisp and stays in the CI host directory. It is
excluded from compiler packages. LLVM, Clang, Zig, Nim and Rust jobs use it.
Rust 1.99.1 enables WASIp3 support with `-Sp3=y`. The 38.0.4 recipe remains for
historical Rust 1.83 artifacts and is outside the active Rust pipeline.
AssemblyScript uses the separate Python API. Other jobs use the official host.

Change the Wisp source recipes first, copy all six source files here, and update
their origin hashes together. The source archive and Cargo lockfile remain
pinned. `python3 tools/shared-hosts/build.py --check --prefix PREFIX` verifies an
already built host and runs the external-module smoke test again. Use
`build-rust.py` only for the legacy per-thread Rust host.
