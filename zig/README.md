# Zig compiler hosted in WebAssembly

This toolchain compiles Zig libraries to freestanding Core WebAssembly. Both
compiler stages run inside Wasm. The frontend is the real Zig bootstrap compiler
from the official 0.17.0 source release. It emits C, then the sibling `clang`
Clang/LLD component compiles and links that C.

```sh
python3 bootstrap.py
python3 compile.py examples/add.zig -o work/add.wasm
"$WISP_WASMTIME" run --invoke add work/add.wasm 20 22
python3 verify.py
```

The invocation prints `42`. Build the sibling `clang` toolchain first if its
pinned `clang-wasm.component` is absent. Python 3.12 or newer and Linux x86_64 are
required for bootstrap. After bootstrap, compilation needs Python, the
files in `dist`, the separately installed Clang package, and Wisp's Wasmtime host.
Select Clang with `WISP_CLANG` or `--clang PATH`; the sibling package is the default. Set `WISP_WASMTIME`, pass
`--wasmtime PATH`, or install Wasmtime on the host PATH. The compiler package
contains no native runtime. It does not call a native Zig compiler, Clang, or linker.

## Pipeline

1. `dist/zig.wasm` runs under Wasmtime with WASI Preview 1 and compiles the
   project for `wasm32-freestanding`, using Zig's C backend.
2. The separate Clang package's `clang-wasm.component` runs under Wasmtime with WASIp3 and compiles and
   links the C intermediate in the component.
3. The output is a Core Wasm module with exported C ABI functions. Verified
   examples have no runtime imports.

The frontend retains Zig's parser, semantic analysis, compile-time evaluation,
local imports, generics, and standard library. It is upstream's small bootstrap
compiler with the C backend enabled. The normal Zig build runner, package
fetching, native targets, and LLVM backend are outside this toolchain.
The target is a freestanding library, so OS APIs, libc, and an allocator are not
bundled into generated programs. Standard library code that needs these
facilities needs a separate runtime integration. C-backend runtime helpers and
C interoperability beyond the verified examples have not been audited.

Zig code uses `ReleaseSmall` optimization. This follows the upstream bootstrap
compiler's supported path and disables runtime safety checks. Clang uses `-O1`
by default. Pass `-O0` to disable Clang optimization. `--emit-c PATH` saves the
intermediate for inspection.

`compile.py` stages the directory containing the root source file, preserving
local imports and embedded files. Use `--project-root DIRECTORY` when imports
need a wider project directory. Dependency caches, build outputs, and hidden
toolchain directories are omitted. Project symlinks are rejected. Filenames
and output directories can contain spaces. The output is replaced only after
both compiler stages succeed.

```sh
python3 compile.py project/src/library.zig --project-root project \
  --emit-c work/library.c -o work/library.wasm
```

## Pinned inputs

`toolchain.lock.json` contains upstream download URLs and SHA-256 hashes.
Bootstrap verifies the archives, Zig bootstrap module, matching `zig.h`, and
sibling Clang component before installing them. `dist/manifest.json` records
size and SHA-256 for every installed runtime file.

From the repository root, `python3 build.py build zig` builds the C/C++ backend
first and passes its source-build receipt to Zig's bootstrap. That receipt pins
the LLVM revision, current build recipe, source inputs and actual backend hash.
The C/C++ build must also pass its WASI 0.3 interface check and compile an
addition function that returns `42`. Zig records the external backend hash and build provenance in `dist/manifest.json`.
It does not copy the compiler or its receipt into the Zig package. Direct `bootstrap.py` calls keep requiring the original
pinned backend hash unless `--clang-build-receipt PATH` is supplied explicitly.

- Zig 0.17.0 official source archive and its `stage1/zig1.wasm`.
- `clang`'s driverless component built from LLVM revision
  `d297af3b3d7a086d64f1477cfa682a16fcfa3260`.
Clang owns its resource headers. Zig keeps only upstream `zig.h` locally.
`WISP_CLANG_RESOURCE_DIR` selects the external Clang header directory;
`../clang/include` is the default.
Wisp provides the Wasmtime host. Version 50.0.0 is tested with both
WASI Preview 1 and WASI 0.3 command execution. Its bytes are excluded from
the compiler package.

Native SDK executables never enter the compilation pipeline. Zig bootstrap
installs the frontend and Zig-specific library data only. Clang and its
resource headers remain separate package dependencies.

The source scan was attempted before implementation. The scanner reported
`is-malicious: TYPESAFE_API_KEY is not set. The scan talks to TypeSafe Jev and
will not run without it.` No successful scan is claimed. The release archive
hash was checked against Zig's official download index.

## Verification

`python3 verify.py` checks runtime file hashes, integer addition, floating point
exports, structs, standard library calls, compile-time generics, slice iteration,
local imports, invalid Zig source, and preservation of an earlier artifact after
a failed build. It validates generated modules by running them with Wasmtime
and checks that their import section is empty. Builds run with `PATH` set to a
nonexistent directory, preventing a native compiler fallback.

The verifier writes `receipts/verification.json` with output hashes and timings.
It is an integration check of the actual hosted compiler pipeline.

Upstream references:

- <https://ziglang.org/news/goodbye-cpp/>
- <https://ziglang.org/download/index.json>
- <https://github.com/ziglang/zig/tree/0.17.0/stage1>

When selecting a separately source-built backend without changing the installed
manifest, pass `--clang-sha256 SHA256` to pin its exact bytes. Without this option
the runner requires the backend checksum from dist/manifest.json. Record the
backend's source-build receipt alongside application build evidence. Both
compiler guests use the Wasmtime cache and disable host JIT optimization.

`--memory-runtime` appends memory.c to the emitted C and supplies freestanding
memcpy, memmove, memset and memcmp implementations through the guest backend.
The byte loops use volatile accesses to avoid recursive lowering to intrinsics.
The staged C header returns zero for Wasm return-address diagnostics, since
Clang cannot materialize a native return address on this target. Installed
headers remain unchanged. `--emit-c FILE` also saves the staged header as
FILE with a .zig.h suffix, including when the later backend stage fails.
