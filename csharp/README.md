# C#

Experimental scalar adapter. Roslyn 4.8.0 compiles C# to IL inside the .NET 8.0.0
Mono WASI interpreter. The same Wasm guest embeds the emitted IL into a fixed
Mono reactor; the shared Wasm `wasm-tools` guest assembles the authored WIT
Component. Application builds require Python and Wasmtime, with no native .NET
compiler, NativeAOT, SDK project build or native application relinking.

## Why this approach

Mono WASI can interpret IL and register bundled assemblies. This keeps ordinary
C# source compilation inside Wasm and allows managed language features to remain
ordinary IL. Native tools bootstrap the fixed compiler guest and runtime only.
The [upstream driver](https://github.com/dotnet/runtime/blob/v8.0.0/src/mono/wasi/runtime/driver.c)
provides assembly registration and managed invocation. This is an interpreter
route, similar to JavaScript's embedded QuickJS runtime.

[componentize-dotnet](https://github.com/bytecodealliance/componentize-dotnet)
provides upstream WIT bindings and AOT Components, but its native application
compiler conflicts with Wisp's source-build rule. Porting NativeAOT-LLVM into
Wasm would be a much larger project. Browser Mono supplies an interpreter too,
but requires JavaScript host services absent from a standalone WASI host.

The .NET 8 [WASI workload is an unsupported prototype](https://github.com/dotnet/runtime/blob/v8.0.0/src/mono/wasi/README.md),
and [.NET 9 rejects the workload](https://github.com/dotnet/runtime/issues/114236).
The pinned stack is deliberate. It is not a promise of compatibility with newer
.NET SDKs or the full .NET ecosystem.

The WASI crypto assembly throws even for hash algorithm names. Roslyn needs
hashing during emission, so the compiler's managed closure uses the matching
browser runtime's crypto assembly and its managed SHA implementations. The
output runtime keeps the WASI BCL. This does not provide general browser services
or cryptography to applications.

Every Component includes Mono and its untrimmed managed BCL closure. The scalar
example is about 22 MiB. Runtime initialization, assembly loading and interpreted
execution cost more than a small native-to-Wasm scalar library. No startup or
throughput target is claimed. Reflection metadata remains available; trimming
against only a bootstrap launcher would remove APIs needed by future user IL.

## Build and use

```sh
export WISP_WASMTIME=/absolute/path/to/wasmtime
export WISP_CWASM_RUNTIME="$WISP_WASMTIME"
# Optional existing native SDK, used only by bootstrap:
export WISP_CSHARP_WASI_SDK=/absolute/path/to/wasi-sdk-34.0-x86_64-linux
python3 build.py build csharp --jobs 1
python3 build.py test csharp --jobs 1
python3 build.py package csharp --jobs 1

python3 csharp/compile.py csharp/examples/add.cs \
  --wit csharp/examples/add.wit --world calculator -o build/add.component.wasm
"$WISP_WASMTIME" run --invoke 'add(20,22)' build/add.component.wasm
# 42
```

`bootstrap.py` downloads versioned, SHA-256-pinned artifacts into `.toolchains`.
Without `WISP_CSHARP_WASI_SDK`, it also downloads the pinned native WASI SDK.
Use `--offline` for cached inputs, `--check` to verify an existing distribution,
or `--rebuild` to rebuild. A native .NET SDK/workload installation is unnecessary;
bootstrap uses the pinned SDK directly, without MSBuild restore or NuGet restore.
`dist/manifest.json` records input, guest and managed assembly checksums.

The managed compiler and framework assemblies are interpreter data distributed
in `assemblies.zip`. Python unpacks verified bytes into the build's disk-backed
scratch directory. Roslyn emits a real managed PE assembly in Wasm and writes it
to a reserved 1 MiB data slot in the fixed reactor. This packaging step runs in
Wasm too. It does not parse C# or replace source expressions on the host.

The adapter validates the entry signature before publication, initializes a
validation Component without calling the application method, then publishes the
public Component, adjacent `.wit`, and `.build.json` receipt. The validation
export does not appear in the public Component. Managed method failures trap.
Existing outputs are preserved.

## Supported subset

- One UTF-8 `.cs` file, with a public `Calculator` class and exactly one public,
  static, non-generic `int Add(int, int)` method. Other classes and private generic
  helpers may be in that file. Overloads, by-reference parameters and generic
  entry methods are rejected.
- Authored WIT with only root `export add: func(left: s32, right: s32) -> s32`.
  Parameter names may vary. No WIT imports, interface exports or automatic
  contract inference.
- Arithmetic, negative values, unchecked signed 32-bit overflow, collections and
  LINQ have executable checks. This is not general NuGet library certification.
- Source and emitted IL each have a 1 MiB limit; Components have a 64 MiB limit.
- Single-threaded Roslyn, no compiler server, MSBuild evaluation, package restore,
  analyzers, source generators, external assembly references or native extensions.
  Application runtime APIs remain limited by the WASI BCL and available host
  capabilities. Threads, subprocesses, arbitrary P/Invoke, networking and general
  cryptography are unsupported. Output globalization is invariant.

## Verification and resources

`verify.py` clears PATH before source builds. It checks the real Component call
`add(20,22) = 42`, negative values and overflow, collections/LINQ, managed exception
traps, syntax and signature diagnostics, unsupported WIT, hidden validation
exports and preservation of existing output files. `verification.json` records
check results, hashes, sizes and elapsed time. Packaging verifies the installed
distribution's full inventory and bootstrap input hashes.

On the shared development host, the initial 5.7 GiB free disk left insufficient
space above the required 5 GiB reserve. After capacity increased to 15 GiB,
bootstrap and validation resumed in a verified 4 GiB cgroup with swap disabled
for that scope. Builds use one worker. Bootstrap budgets 3 GiB disk growth plus
5 GiB free reserve. Scratch, downloads and caches belong on disk, never `/tmp`
or another tmpfs. Check RAM, swap, filesystems, inodes and competing jobs before
running these commands. Optional `WISP_CSHARP_CACHE=/disk/path` selects a Wasmtime
cache; without it, guest compilation caching is disabled. Remove only task-owned
intermediates when finished.
