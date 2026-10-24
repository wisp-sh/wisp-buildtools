# Clang hosted in WebAssembly

This package provides a small WASIp3 C/C++ compiler driver and Clang resource
headers. The driver imports LLVM, Clang CodeGen, LLD and the C++ runtime from the
separate [LLVM shared library package](../llvm/SHARED.md). It contains no LLVM
archive copies or Wasmtime host.

From the repository root:

```sh
python3 build.py build clang
python3 build.py test clang
python3 build.py package clang
```

Wisp provides the Wasmtime host through `WISP_WASMTIME`. `WISP_LLVM` points to
the installed shared LLVM directory containing `manifest.json`. The host loads
its checksum-verified core modules with `--preload`; the compiler and its
libraries share linear memory and an indirect-function table. The compiler
component itself is approximately 3.24 MB. The package size report accounts for
its resource headers and separate LLVM dependency.

`WISP_CLANG` selects `.toolchains/build-p3/bin/clang-wasm.component`.
`WISP_CLANG_RESOURCE_DIR` selects `include/`. Nim and Zig use these headers from
the Clang package rather than copying them into their own packages.

The compiler accepts one source file, `-o`, `-O0`, `-O1` and `-I`. A `.c` suffix
selects C; other source files select C++. Compilation targets
`wasm32-unknown-unknown`, disables standard include paths, RTTI and exceptions,
and links with Wasm LLD's `--no-entry --export-all`. Its output is a Core Wasm
module with no WASI imports. All parsing, optimization, code generation and
linking run in Wasm.

Bootstrap uses LLVM revision
`d297af3b3d7a086d64f1477cfa682a16fcfa3260` and checksum-pinned WASI SDK 34.0.
The native SDK compiler and table generators run only during bootstrap.
[`build-clang-wasi.sh`](build-clang-wasi.sh) builds the source libraries;
[`../llvm/scripts/shared.py`](../llvm/scripts/shared.py) links the shared
provider and both compiler consumers. Static archives stay in bootstrap
artifacts and are excluded from runtime packages.
The Wasm compiler build remaps source paths to `/wisp-buildtools` and home paths
to `/build-user`. Rebuild cached LLVM archives before producing public bundles
if they predate those flags.

[`verify-clang-wasm.sh`](verify-clang-wasm.sh) checks scalar addition, templates,
structs, `constexpr`, C input, includes, invalid syntax, disabled exceptions,
rejection of target overrides and generated module imports. Where `strace` is
available it verifies that only the supplied Wasmtime host starts.

[DRIVERLESS.md](DRIVERLESS.md) describes the compiler entry point and verification
commands. Runtime sizes come from the package inventory.

For libc-backed library reactors, `--wasi-sysroot DIR` opts into the fixed
`wasm32-wasip1` application target. It uses the sysroot's target headers,
`crt1-reactor.o`, libc and compiler builtins; C++ also uses the SDK's no-exceptions
libc++ and libc++abi. `DIR` must be a WASI SDK 34.0 `share/wasi-sysroot` directory
with the SDK directory mounted in the compiler guest. `-Dname=value` and
`-std=c++11` are accepted in both modes. Exceptions, RTTI, multiple input files
and arbitrary target overrides remain unsupported. All application compilation
and linking still run inside the Wasm compiler.
