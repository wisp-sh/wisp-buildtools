# Toolchain package sizes

These are measurements of previously built packages. The Rust and `llvm-rust`
rows describe the retired Rust 1.83 split image, not the new Rust 1.99.1/WASIp3
recipe. Regenerate the report from new bundles to measure that compiler.


Each bundle contains its own files. Required Wisp packages are installed once alongside consumers. Installed bytes exclude generated share/wisp/toolchains receipts, filesystem allocation and RAM. Download bytes are complete bundles. Support covers file formats other than Wasm and native binaries, including JavaScript and JVM compiler code, JDK data, library sources and headers. External Wisp execution hosts are excluded from payload totals.

| Tool | Download MiB | Installed MiB | Wasm MiB | Native MiB | Support MiB | Required packages | Complete download MiB |
| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| llvm | 26.53 | 92.74 | 92.60 | 0.00 | 0.14 | none | 26.53 |
| clang | 1.24 | 10.56 | 3.09 | 0.00 | 7.47 | llvm | 27.77 |
| go | 47.52 | 208.91 | 208.83 | 0.00 | 0.08 | none | 47.52 |
| llvm-rust | 8.23 | 26.56 | 26.50 | 0.00 | 0.06 | none | 8.23 |
| rust | 90.86 | 263.96 | 263.78 | 0.00 | 0.17 | llvm-rust | 99.09 |
| zig | 6.46 | 41.62 | 3.10 | 0.00 | 38.52 | clang | 34.23 |
| assemblyscript | 2.60 | 10.34 | 9.45 | 0.00 | 0.89 | none | 2.60 |
| nim | 5.48 | 20.33 | 15.07 | 0.00 | 5.26 | llvm, clang | 33.25 |
| wasm-tools | 3.60 | 15.33 | 15.31 | 0.00 | 0.02 | none | 3.60 |
| javascript | 5.83 | 20.24 | 20.21 | 0.00 | 0.03 | wasm-tools | 9.42 |
| python | 6.02 | 15.28 | 15.19 | 0.00 | 0.09 | wasm-tools | 9.61 |
| grain | 3.98 | 22.39 | 0.83 | 0.00 | 21.56 | wasm-tools | 7.58 |
| llvm-swift | 36.49 | 120.06 | 116.88 | 0.00 | 3.18 | none | 36.49 |
| swift | 29.49 | 124.45 | 53.23 | 0.00 | 71.22 | llvm-swift, wasm-tools | 69.57 |
| jvm | 70.20 | 139.22 | 0.72 | 0.00 | 138.50 | none | 70.20 |
| teavm | 27.71 | 29.38 | 0.00 | 0.00 | 29.38 | jvm, wasm-tools | 101.51 |
| java | 3.06 | 3.30 | 0.00 | 0.00 | 3.30 | jvm, teavm | 104.57 |
| kotlin | 57.44 | 62.66 | 0.00 | 0.00 | 62.66 | jvm, teavm | 158.95 |

## Runtime requirements

- **llvm**: Wisp Wasmtime host 49.0.1: external. Wisp supplies the execution engine and loads the four verified shared core modules. This package supplies LLVM and its Wasm C/C++ runtime libraries; source bootstrap SDKs and native hosts are excluded.
- **clang**: Wisp Wasmtime host 49.0.1: external. Wisp-owned execution host with WASI Preview 3 and external Component core-module imports. Select it with WISP_WASMTIME. The engine is not part of this compiler package. wisp:llvm 0.1.0: external. Install this Wisp toolchain package once in the same prefix. Its files are not embedded in this bundle.
- **go**: Python 3.12+: external. Native file I/O and orchestration host. This is the supported common workspace baseline; application source compilation executes in Wasm. Wasmtime 49.0.1: external. External Wasmtime 49.0.1 CLI is tested. Select it with the required --wasmtime argument. No native Go installation or network is needed to compile local Go sources. Go module installer : external. Optional external installer for fetching ordinary Go modules in Wisp workflows. The direct local-package build adapter does not invoke native Go or fetch modules.
- **llvm-rust**: Wisp Rust Wasmtime host 38.0.4: external. Wisp-owned host with legacy WASI threads and postloaded shared modules. The split library is bound to the matching Rust primary module by its integrity manifest; it does not use the LLVM 24 ABI.
- **rust**: Python 3.12+: external. Native file I/O and orchestration host. This is the supported common workspace baseline; application source compilation executes in Wasm. Wasmtime 38.0.4: external. Wisp-owned external host with legacy WASI Preview 1 threads, selected by WISP_RUST_WASMTIME or --wasmtime. No engine is included in the compiler package. Cargo : external. Native cargo metadata --locked --offline is required on every Cargo project build. Cargo version is not pinned by the adapter. Registry dependencies must be vendored before building, using cargo vendor. Rust compilation, build scripts and procedural macros execute in Wasm; native rustc is not needed. wisp:llvm-rust 0.1.0: external. Install this Wisp toolchain package once in the same prefix. Its files are not embedded in this bundle.
- **zig**: Python 3.12+: external. Native file I/O and orchestration host. This is the supported common workspace baseline; application source compilation executes in Wasm. Wisp Wasmtime host 49.0.1: external. Wisp-owned execution host with WASI Preview 3 and external Component core-module imports. Select it with WISP_WASMTIME. The engine is not part of this compiler package. wisp:clang 0.1.0: external. Install this Wisp toolchain package once in the same prefix. Its files are not embedded in this bundle.
- **assemblyscript**: Python 3.12+: external. Native file I/O and orchestration host. This is the supported common workspace baseline; application source compilation executes in Wasm. Wasmtime Python bindings 49.0.0: external. Wisp-owned Wasmtime Python bindings and native engine, selected by WISP_WASMTIME_PYTHON. Required host callbacks use the Python API. Node and npm are bootstrap-only requirements.
- **nim**: Python 3.12+: external. Native file I/O and orchestration host. This is the supported common workspace baseline; application source compilation executes in Wasm. Wisp Wasmtime host 49.0.1: external. Wisp-owned execution host with WASI Preview 3 and external Component core-module imports. Select it with WISP_WASMTIME. The engine is not part of this compiler package. wisp:llvm 0.1.0: external. Install this Wisp toolchain package once in the same prefix. Its files are not embedded in this bundle. wisp:clang 0.1.0: external. Install this Wisp toolchain package once in the same prefix. Its files are not embedded in this bundle.
- **wasm-tools**: Python 3.12+: external. Native file I/O and orchestration host. This is the supported common workspace baseline; application source compilation executes in Wasm. Wasmtime 49.0.1: external. External CLI; this version is tested. Supplies WASI Preview 1 and Component Model execution. Set WISP_WASMTIME or install wasmtime on PATH.
- **javascript**: Python 3.12+: external. Native file I/O and orchestration host. This is the supported common workspace baseline; application source compilation executes in Wasm. Wasmtime 49.0.1: external. External CLI; this version is tested. Supplies WASI Preview 1 and Component Model execution. Set WISP_WASMTIME or install wasmtime on PATH. wisp:wasm-tools 0.1.0: external. Install this Wisp toolchain package once in the same prefix. Its files are not embedded in this bundle.
- **python**: Python 3.12+: external. Native file I/O and orchestration host. This is the supported common workspace baseline; application source compilation executes in Wasm. Wasmtime 49.0.1: external. External CLI; this version is tested. Supplies WASI Preview 1 and Component Model execution. Set WISP_WASMTIME or install wasmtime on PATH. wisp:wasm-tools 0.1.0: external. Install this Wisp toolchain package once in the same prefix. Its files are not embedded in this bundle.
- **grain**: Python 3.12+: external. File I/O, source staging and Wasm guest orchestration. Source compilation runs entirely inside the Wasm QuickJS guest. Wasmtime 49.0.1: external. Wisp-provided external execution host. Select it with WISP_WASMTIME; no engine or native Grain/Node compiler is bundled. wisp:wasm-tools 0.1.0: external. Install this Wisp toolchain package once in the same prefix. Its files are not embedded in this bundle.
- **llvm-swift**: Wisp Wasmtime Python host 49.0.0: external. External bindings and engine supplied by Wisp. These backend modules use an image-specific table ABI matched to the exact Swift compiler and linker primary hashes; LLVM24 and Rust LLVM19 are separate profiles.
- **swift**: Python 3.12+: external. File I/O and guest orchestration. Swift compilation and linking execute inside Wasm. Wasmtime Python bindings 49.0.0: external. Wisp-owned external bindings and engine, selected with WISP_WASMTIME_PYTHON. They instantiate the compiler primary and its matching shared backend before guest entry. No engine or native swiftc is bundled. Component assembly also uses the shared wasm-tools CLI host. wisp:llvm-swift 0.1.0: external. Install this Wisp toolchain package once in the same prefix. Its files are not embedded in this bundle. wisp:wasm-tools 0.1.0: external. Install this Wisp toolchain package once in the same prefix. Its files are not embedded in this bundle.
- **jvm**: Python 3.12+: external. File I/O, input snapshots and guest orchestration. Compiler and backend code execute in Wasm. Wasmtime 49.0.1: external. Wisp-owned external CLI with WASI Preview1, tail calls and exceptions. No engine or native JVM is bundled.
- **teavm**: Python 3.12+: external. File I/O, input snapshots and guest orchestration. Compiler and backend code execute in Wasm. wisp:jvm 0.1.0: external. Install this Wisp toolchain package once in the same prefix. Its files are not embedded in this bundle. wisp:wasm-tools 0.1.0: external. Install this Wisp toolchain package once in the same prefix. Its files are not embedded in this bundle.
- **java**: Python 3.12+: external. File I/O, input snapshots and guest orchestration. Compiler and backend code execute in Wasm. wisp:jvm 0.1.0: external. Install this Wisp toolchain package once in the same prefix. Its files are not embedded in this bundle. wisp:teavm 0.1.0: external. Install this Wisp toolchain package once in the same prefix. Its files are not embedded in this bundle.
- **kotlin**: Python 3.12+: external. File I/O, input snapshots and guest orchestration. Compiler and backend code execute in Wasm. wisp:jvm 0.1.0: external. Install this Wisp toolchain package once in the same prefix. Its files are not embedded in this bundle. wisp:teavm 0.1.0: external. Install this Wisp toolchain package once in the same prefix. Its files are not embedded in this bundle.

## Wisp execution hosts

Measured once outside compiler packages. These sizes exclude system libraries and caches.

| Host | MiB | Measurement |
| --- | ---: | --- |
| Wisp Wasmtime host | 28.30 | Executable only |
| Rust Wasmtime host | 20.52 | Executable only |
| Wasmtime Python host | 30.66 | Python binding files and engine |

## Shared payload

Installing every package once totals 1227.31 MiB. Exact file-content union is 1223.76 MiB. LLVM, Clang and wasm-tools dependencies are separate packages; consumers do not embed their files.

- 0.37 MiB shared by clang, swift: `clang/include/hexagon_protos.h`.
- 0.13 MiB shared by clang, swift: `clang/include/lsxintrin.h`.
- 0.07 MiB shared by clang, swift: `clang/include/ppc_wrappers/emmintrin.h`.
- 0.07 MiB shared by clang, swift: `clang/include/velintrin_gen.h`.
- 0.06 MiB shared by clang, swift: `clang/include/ppc_wrappers/xmmintrin.h`.
- 0.05 MiB shared by clang, swift: `clang/include/__clang_cuda_texture_intrinsics.h`.
- 0.04 MiB shared by clang, swift: `clang/include/ppc_wrappers/mmintrin.h`.
- 0.03 MiB shared by clang, swift: `clang/include/tgmath.h`.
- 0.03 MiB shared by clang, swift: `clang/include/__clang_hip_cmath.h`.
- 0.02 MiB shared by clang, swift: `clang/include/ia32intrin.h`.
