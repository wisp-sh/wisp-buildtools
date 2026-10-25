# C# feasibility

Status: experimental design only. No C# compiler package, build adapter or
verified Component exists yet. `python3 build.py build|test|package csharp` is
not implemented. Bootstrap was deferred because this host cannot preserve the
required disk reserve. Wisp integration must wait for a verified scalar MVP.

## Decision

Pursue Roslyn running on the .NET 8 Mono WASI interpreter, with a second Mono
interpreter embedded in each output Component. This satisfies the Wasm-hosted
source-build rule by design, but Roslyn compatibility and Component packaging
still need an executable proof.

The [.NET 8 WASI documentation](https://github.com/dotnet/runtime/blob/v8.0.0/src/mono/wasi/README.md)
describes an IL interpreter. The
[runtime driver](https://github.com/dotnet/runtime/blob/v8.0.21/src/mono/wasi/runtime/driver.c)
initializes that interpreter and provides assembly registration, lookup and
managed method invocation. These are useful building blocks, not evidence that
Roslyn already works in WASI. The .NET 8 `wasi-experimental` workload is a
prototype; [the .NET 9 workload rejects this target](https://github.com/dotnet/runtime/issues/114236).
Do not assume a newer SDK is a compatible replacement.

Native .NET and a native WASI SDK may bootstrap a fixed compiler guest and a
fixed output runtime. Application builds must then run Roslyn's managed APIs
inside Wasm to emit IL, embed that IL and its managed dependencies using a Wasm
packager or Wasm-hosted linker, and assemble/validate the Component with the
shared Wasm `wasm-tools`. Python only orchestrates those guests and file I/O.
Native `dotnet build`, `csc`, ILLink, NativeAOT and native relinking of an
application are outside that boundary. A normal workload publish of each user
project would therefore be insufficient.

[componentize-dotnet](https://github.com/bytecodealliance/componentize-dotnet)
already supplies WIT bindings and AOT Components, but its native application
compiler conflicts with this rule. It is the best compromise if native source
builds are ever allowed, not a fallback for this adapter. Porting NativeAOT-LLVM
itself into Wasm would require a much larger compiler effort. Browser Mono can
also interpret IL, but would need its JavaScript host services replaced for a
standalone WASI compiler. Mono WASI starts closer to the required host.

## Tradeoffs and intended scope

Expect startup work for runtime initialization, assembly loading and Roslyn
metadata loading. Each output carries Mono plus a managed framework subset and
application IL, so even `Add` will be much larger than a scalar C Component.
Execution uses an interpreter. Startup, peak memory and Component sizes are
unmeasured; no performance or size claim is established.

The first contract is an authored root export `add(s32, s32) -> s32` mapped to
an ordinary public static `Calculator.Add(int, int)`. The input files are
[examples/add.cs](examples/add.cs) and [examples/add.wit](examples/add.wit).
They are acceptance inputs, not verified runnable examples. Initialize the
runtime once per Component instance, load the embedded IL, validate the exact
method signature, invoke it and unbox the signed 32-bit result. A trap must
report managed failures rather than silently returning a scalar default.

Limit initial source builds to local C# files and explicitly supplied compatible
managed assemblies. Disable Roslyn concurrent compilation and compiler-server
use. No MSBuild project evaluation, NuGet restore, analyzers or source generators
are promised. Reflection needs retained metadata; trimming a fixed runtime
against only the bootstrap launcher can remove APIs needed by future user IL.
Keep the compiler metadata/reference assemblies separate from executable BCL
assemblies. General NuGet compatibility does not follow from Roslyn support.
Native extensions, P/Invoke, OS processes, threads, networking, general WIT
objects/strings/resources and async contracts require separate work.

## Validation gates before registration

1. Pin an SDK, workload packs, Roslyn, reference assemblies, runtime sources,
   licenses and any bootstrap SDK archives by version and SHA-256. Record the
   resulting guests and runtime payload in an integrity manifest. No compiler
   artifacts were downloaded or checksums invented during this assessment.
2. Bootstrap a compiler guest with native tools, then compile `examples/add.cs`
   and a deliberate syntax error with only Wasmtime available to the adapter.
   This must exercise Roslyn and produce a real managed PE assembly.
3. Embed the emitted IL in a standalone runtime without native application
   compilation or relinking. Prove that a library reactor can initialize and
   invoke managed methods without a command `_start` or external DLL preopens.
4. Assemble the authored WIT Component entirely through Wasm stages. Verify
   `wasmtime run --invoke 'add(20,22)' add.component.wasm` prints `42`, test
   negative values, repeated calls, mismatched signatures and managed failures.
5. Add `bootstrap.py`, `compile.py`, `verify.py`, checksums and entries in
   `toolchains.json`, `buildtools.json` and `package-profiles.json`. Test build,
   verification, packaging and an installed package with native compilers absent.
   Only then add the Wisp adapter in a separate Wisp worktree.

## Resource gate recorded on 2026-10-25

`free -h` reported 14 GiB RAM, 12 GiB available and 2.9 GiB of 4 GiB swap used.
`vmstat 1 3` showed no swap activity in the two live samples. The shared agent
cgroup had an 11,872,866,304-byte memory limit. No compiler build process was
observed. `df -i` reported 28% inode use. `findmnt -T` confirmed this checkout
and the home cache use disk-backed ext4; `/tmp` is tmpfs and must not hold builds.

The exact disk sample was 6,061,006,848 available bytes. Reserving 5 GiB leaves
692,297,728 bytes for all job growth. As a baseline capacity probe, the official
[SDK 8.0.100 archive](https://dotnetcli.blob.core.windows.net/dotnet/Sdk/8.0.100/dotnet-sdk-8.0.100-linux-x64.tar.gz)
has a 214,395,068-byte Content-Length and its gzip trailer reports 587,776,000
uncompressed tar bytes. A retained archive plus that uncompressed stream already
budgets 802,171,068 bytes, before workload packs, Roslyn, caches or build outputs.
This is not a measured complete installation size or a selected release pin.
Only HTTP headers and four trailer bytes were read; no SDK archive was saved.

No SDK/workload installation, runtime bootstrap, source compilation, Component
invocation or package test ran. Use a larger disk-backed workspace or another
host and budget the full bootstrap before resuming. No Wisp files were changed.
