# C/C++ compiler entry point

`clang-wasm` is a WASIp3 Component that imports the separately installed
[LLVM shared library](../llvm/SHARED.md). Its entry point creates a
`CompilerInvocation`, compiles through `CompilerInstance::ExecuteAction`, then
calls the WebAssembly LLD library in the same process. Compilation launches no
subprocesses. The compiler and its shared LLVM modules use the same linear
memory and indirect-function table.

## Build and verify

Run from the repository root:

```sh
python3 build.py build clang --jobs 2
python3 build.py test clang
python3 build.py package clang
```

Set `WISP_WASMTIME` to the Wisp host with external core-module imports and
`WISP_LLVM` to `llvm/dist/shared`. See [README.md](README.md) for supported
arguments, application targets and the optional WASI sysroot.

The source is [clang_wasm_main.cpp](src/clang_wasm_main.cpp). Bootstrap applies
[the WASIp3 port](patches/0001-clang-wasip3-in-process.patch) and
[the compiler entry-point patch](patches/0002-driverless-clang-wasm.patch).
It copies the entry-point source into LLVM and refreshes it on incremental
builds. [PORTING.md](PORTING.md) describes the WASI adaptations.

## Compilation contract

The default application target is `wasm32-unknown-unknown`. The compiler emits
an object with static relocation, then links Core Wasm with
`--no-entry --export-all`. The output has no WASI imports. Standard and builtin
include searches are disabled, so pass `-I` for application headers. RTTI,
exceptions and thread-safe statics are disabled. User code defaults to `-O0`;
`-O1` selects application optimization independently of compiler bootstrap flags.

`--wasi-sysroot` selects the fixed `wasm32-wasip1` application target and its
reactor startup, libc and compiler builtins. C++ also uses the matching
no-exceptions libc++ and libc++abi. The SDK must be mounted in the guest.
The hosted compiler reserves a 16 MiB stack for LLVM compilation.

## Verification and measurement

[verify-clang-wasm.sh](verify-clang-wasm.sh) checks addition, templates, structs,
`constexpr`, C input, include lookup, invalid syntax, disabled exceptions,
target-override rejection and generated-module imports. It requires `rg`.
With `strace`, it also checks that only the supplied Wasmtime process starts.
Without `strace`, it reports that tracing was skipped.

`linked-size.py` attributes linked code and data from `wasm-ld` maps.
`benchmark.py` records startup, compilation, link time and peak RSS. Run these
against your own artifacts; keep their generated reports outside Git.
`python3 build.py sizes clang` reports packaged download and installed sizes,
including the external LLVM package dependency.
