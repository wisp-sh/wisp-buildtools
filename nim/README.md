# Nim compiler hosted in WebAssembly

This toolchain runs the complete Nim 2.2.12 compiler in WASI. Nim emits C, then a small WASIp3 component imports Clang, LLVM and LLD from the separate [shared LLVM package](../llvm/SHARED.md) to produce a WASIp1 Core WebAssembly reactor. User builds use no native Nim, C compiler or linker.

```sh
python3 bootstrap.py
python3 compile.py examples/add.nim -o work/add.wasm --export add
python3 verify.py
```

The scalar example exports a regular Nim procedure through its C ABI:

```nim
proc add*(a, b: int32): int32 {.exportc, cdecl.} =
  a + b
```

```sh
../clang/.toolchains/wasmtime-v49.0.1-x86_64-linux/wasmtime run \
  --invoke add work/add.wasm 20 22
# 42
```

`--export` can be repeated. Without it the linker exports all symbols. `--project-root DIRECTORY` mounts a project containing the source and its local imports. `--work-dir DIRECTORY` retains the generated C files, objects and backend invocation JSON. `--nim-arg=VALUE` passes an extra option to the Nim frontend. The launcher replaces the output only after compilation and linking succeed.

The result is a Core Wasm module, ready for a separate Component Model assembly step. Call its WASI reactor `_initialize` once before calling library exports. That initializes Nim globals and runs top level Nim statements. Hosts using the WASI reactor convention do this automatically. The verifier checks initialization through Wasmtime.

## Bootstrap inputs

The default paths use sibling `clang` and `llvm` builds in the Wisp buildtools workspace:

- Nim 2.2.12 official Linux x86-64 bootstrap archive. Its URL and SHA-256 are pinned in `toolchain.lock.json` and verified before extraction.
- WASI SDK 34.0, normally `../clang/.toolchains/wasi-sdk-34.0-x86_64-linux`. Set `WASI_SDK_PATH` or pass `--wasi-sdk` to select another copy.
- The packaged LLVM 24 SDK, normally `../llvm/artifacts/llvm-24-d297af3-wasip3-o1`. Set `WISP_LLVM_SDK` or pass `--llvm-sdk` to select it.
- Python 3.12+, curl, CMake and a native C++ build tool. Bootstrap compiles at most two C files at a time.
- The LLVM runtime provider, normally `../llvm/dist/shared`. Set `WISP_LLVM` to its installed directory.
- Clang's resource headers, normally `../clang/include`. Set `WISP_CLANG_RESOURCE_DIR` to their installed directory. Nim verifies their hashes against its bootstrap inventory.
- A Wisp-provided Wasmtime host with WASIp3 and external core-module imports for user builds. Set `WISP_WASMTIME` to its executable. The Nim package contains no host runtime.

Native Nim generates the compiler's C during bootstrap. WASI SDK compiles that C. The shared LLVM builder links the small Clang/LLD entry point against external shared modules. `dist/` contains Nim's frontend, the small backend consumer, Nim standard library, application C headers and libraries, notices, and a SHA-256 manifest. It can be relocated together with its Python entry points and `toolchain.lock.json`, alongside the LLVM package. `bootstrap.py` verifies an existing package before reusing it. `--force` rebuilds it. Bootstrap prepares the complete new package before replacing an existing `dist/`.

## WASI profile

Nim 2.2.12 has no native `--os:wasi` target. This port uses its `wasm32` CPU with the Linux/POSIX library branches and WASI libc. Memory management uses ORC with `-d:useMalloc`, and exceptions use Nim's `goto` implementation. This preserves Nim exceptions without a JavaScript exception runtime. The bootstrap and application adapters select `-d:useFork` so `osproc` uses the included failure stubs instead of requiring POSIX `spawn.h`. Child processes remain unsupported.

The compatibility changes are small and live in `src/`:

- The compiler's generated three argument `main` is rewritten to a two argument WASI `main`, with `environ` supplied by libc.
- The compiler's `/proc/self/exe` lookup resolves to its `/nim/bin/nim.wasm` mount. Other `readlink` calls retain libc's behavior.
- Missing POSIX process operations return `ENOSYS`. WASI cannot spawn an external compiler or run shell commands through NimScript, `staticExec`, or `osproc`.
- The thread CPU clock maps to the WASI monotonic clock. Close-on-exec constants exist for compiling Nim's POSIX I/O code.

There is no network, thread or subprocess implementation in this port. Pure Nim modules, generics, templates, macros and C emission run in the upstream compiler. C interop requires headers and implementations available to the WASI C backend. The compiler emits Linux-targeted C against WASI headers, so platform-specific standard library modules need testing individually.

`verify.py` checks `add(20, 22) == 42` in Wasmtime, preserves existing outputs after frontend and linker failures, and compiles with an empty `PATH` to verify that user builds need no native compiler. It also builds a program using standard library imports, a compile time macro, generics, sequences and global initialization. Wasmtime initializes the reactor before invoking its stateful exports. Verification needs no Node. Results are written to `work/verify/results.json`.

## Hosted C backend

`dist/bin/c-backend.wasm` provides Clang's cc1 argument interface and Wasm LLD in one WASIp3 process, using external LLVM shared modules rather than embedding LLVM:

```text
c-backend.wasm cc1 CC1_ARGUMENTS...
c-backend.wasm link LLD_ARGUMENTS...
c-backend.wasm build MANIFEST.json
```

The build manifest has `compile`, an array of cc1 argument arrays, and `link`, an LLD argument array. The backend compiles each translation unit separately and links all objects in the same Wasmtime invocation. The host Python script only prepares arguments and moves the finished artifact.

Nim is MIT licensed. LLVM and WASI SDK retain their upstream licenses. Runtime copies and notices are packaged under `dist/licenses`.

The application runner skips host configuration files and explicitly adds the
standard library core, pure, collections, concurrency, POSIX, impure and wrappers
search directories. Legacy imports such as `import macros` resolve alongside
`import std/macros` without relying on a native Nim installation or nim.cfg.
Compiler guests use the Wasmtime cache with host JIT optimization disabled.
