# C#

Experimental C# library adapter. Roslyn 4.8.0 compiles C# to IL inside the .NET 8.0.0
Mono WASI interpreter. The same Wasm guest embeds the emitted IL into a fixed
Mono reactor; the shared Wasm `wasm-tools` guest assembles the authored WIT
Component. Application builds require Python and Wasmtime, with no native .NET
compiler, NativeAOT, MSBuild execution or native application relinking.

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
example is about 21 MiB. Runtime initialization, assembly loading and interpreted
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
scratch directory. Roslyn emits managed PE assemblies and generates typed bindings in Wasm. The
guest packages application and dependency IL into a 16 MiB slot in the fixed
reactor, then appends canonical ABI wrappers for the authored exports. It omits
unused slot bytes from the file. The fixed Mono runtime needs no application
relinking. This packaging step runs in Wasm too. It does not parse C# or replace source expressions on the host.

The adapter validates the entry signature before publication, initializes a
validation Component without calling the application method, then publishes the
public Component, adjacent `.wit`, and `.build.json` receipt. The validation
export does not appear in the public Component. Managed method failures trap.
Existing outputs are preserved.

## Projects and dependencies

```sh
python3 csharp/compile.py csharp/examples/library/Api/Api.csproj \
  --project-root csharp/examples/library \
  --wit csharp/examples/library/library.wit --world library \
  --entry Example.Api -o build/library.component.wasm
"$WISP_WASMTIME" run --invoke 'greet("世界")' build/library.component.wasm
# "Hello, 世界!"
```

Inputs may be multiple `.cs` files, a directory, or one SDK-style `.csproj`.
Directory inputs select their single immediate project, or collect C# files
recursively. `bin`, `obj`, `.git`, tool caches and output directories are excluded.
`--project-root` bounds source and project-reference paths. Use a common parent
for sibling projects. Symlinks, cyclic references and duplicate assembly names
are rejected. Each `ProjectReference` compiles to IL inside Wasm before its parent.

The project reader supports `net8.0` libraries, assembly names, static
`Compile Include/Remove/Exclude` globs, `ProjectReference`, and local
`Reference/HintPath`. It honors nullable settings, implicit usings, define
constants, C# language versions 8 through 12, unsafe code, optimization, checked arithmetic and
warnings as errors. Standard `NET`, `NET8_0`, `WISP` and `WASI` symbols are defined.
It reads metadata directly; it does not evaluate arbitrary MSBuild. Conditions,
property expansion, imports, targets, custom tasks and generated sources require
porting. No analyzers or source generators run.

`--reference implementation.dll` adds a portable managed assembly. Use
`--library` without WIT to emit an IL dependency with the same Wasm compiler.
Loose library builds use the output stem as their assembly name;
`--assembly-name` overrides it.
`--package file.nupkg` or `--package-dir directory` supplies offline NuGet files.
Exact `PackageReference` versions select supplied packages; transitive dependency
ranges are checked against the one supplied version of each package. No restore,
network access or version discovery occurs. Packages need compatible `lib`
implementation DLLs targeting `net8.0`, `net7.0`, `net6.0`, `netstandard2.1` or
`netstandard2.0`. Native/RID assets, build tasks, tools and analyzers are rejected.
Reference-only packages and framework assemblies cannot replace runtime DLLs.
The guest validates managed IL and transitive assembly names. This does not
certify arbitrary NuGet packages against the WASI BCL. Dependency licensing
remains the application's responsibility. Receipts record source, reference and
package hashes, compiled projects, selected methods and runtime inventory.

## WIT bindings

Authored WIT may export multiple root functions and interfaces. Each export
matches a public static, non-generic C# method by name and signature. Matching
ignores case and separators, so `echo-char` matches `EchoChar`. Overloads must
resolve to one compatible method. `--entry Namespace.Type` restricts automatic
matching. `--export api.greet=Namespace.Type.Method` maps an interface function
explicitly; root names and full canonical interface names work too. By-reference
parameters, generic entry methods and instance methods are rejected. There is
no automatic C# contract inference.

| WIT | C# |
| --- | --- |
| `bool`, integer widths, `f32`, `f64` | `bool`, matching signed/unsigned CLR integer, `float`, `double` |
| `char`, `string` | `System.Text.Rune`, UTF-8 `string` |
| `list<T>` | `T[]`, `List<T>` or supported generic collection interfaces |
| `record` | Public properties/fields; inputs need a matching public constructor or writable members and a default constructor |
| `tuple` | C# value tuple with at most seven elements |
| `enum`, `flags` | C# enums matched by member name; flags require distinct single bits, at most 64 names |
| `option<T>` | `Wisp.Option<T>`, nullable value types, or nullable references |
| `result<T,E>` | `Wisp.Result<T,E>`; absent payloads use `Wisp.Unit` |
| No result | `void` |

`Wisp.Option<T>.Some(value)` / `.None` and `Wisp.Result<T,E>.Ok(value)` /
`.Err(error)` are supplied by the compiler's runtime-support reference. They
preserve nested option distinctions that nullable references cannot express.
Strings, nested lists, records and variants use canonical ABI memory layouts.
The wrapper releases incoming allocations after lifting. Canonical post-return
releases output buffers while preserving Mono and adapter state for later calls.
Managed export exceptions trap with the exception type and message.

WIT imports, resources, general variants, async functions and streams are not
implemented. Applications remain single-threaded and interpreted. Available APIs
depend on the WASI BCL and host capabilities. Subprocesses, arbitrary P/Invoke,
networking, general cryptography and full desktop/server .NET compatibility are
unsupported. Output globalization is invariant.

Limits are 32 projects, 256 source files per project, 8 MiB total source, 128
bundled assemblies including the application, 16 MiB combined IL, and a 64 MiB
Component. Individual strings/list storage have a 32 MiB limit, lists at most
262,144 elements, and output allocations at most 64 MiB per call. These are
bounded experimental libraries, not general SDK workloads.

## Verification and resources

`verify.py` clears PATH before source builds. It checks scalar calls, collections,
LINQ, compiler errors, managed exception traps, multiple files, project references,
offline managed DLL/NuGet dependencies and typed interface calls. The optional
`WISP_WASMTIME_PYTHON=/path/to/wasmtime-python-50.0.1` enables over 300 rich calls
in one instance, including canonical post-return, nested allocations, Unicode,
64-bit boundaries and indirect parameters. Without these bindings the report
explicitly records that check as skipped. No Python Wasmtime package is needed
for builds or CLI verification. `test_project.py` checks input boundaries,
cycles, source selection and rejection of unsupported package/project actions.

On the shared development host, the initial 5.7 GiB free disk left insufficient
space above the required 5 GiB reserve. After capacity increased to 15 GiB,
bootstrap and validation resumed in a verified 4 GiB cgroup with swap disabled
for that scope. Builds use one worker. Bootstrap budgets 3 GiB disk growth plus
5 GiB free reserve. Scratch, downloads and caches belong on disk, never `/tmp`
or another tmpfs. Check RAM, swap, filesystems, inodes and competing jobs before
running these commands. Optional `WISP_CSHARP_CACHE=/disk/path` selects a Wasmtime
cache; without it, guest compilation caching is disabled. Remove only task-owned
intermediates when finished.
