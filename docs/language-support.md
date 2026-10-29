# Language support

Wisp's compiler packages compile application source inside WebAssembly. Language
features available inside a program and values that can cross its public
Component interface are separate limits. A working compiler does not imply that
every library from that language can be packaged, or that every consumer target
can execute the result.

This support assessment covers the checked-in compiler recipes. Compiler
execution is exercised on Linux x86_64. Python orchestrates the build and
Wasmtime runs the compiler guests. Rust also requires native Cargo for locked,
offline metadata resolution. Native compilers and other tools listed in each
package's bootstrap instructions are needed to build the compiler packages.

The levels below describe verified scope. See the root README for project maturity.

## Support levels

- **Integrated** means Wisp source builds, generated bindings and cross-language
  consumers have local end-to-end acceptance evidence.
- **Verified adapter** means the compiler adapter has executable verification
  examples, with a narrower Component interface or library subset.
- **Experimental** means the supported subset is deliberately small or needs
  additional integration before a general package can use it.
- **Command package** means a WASI executable and its data can be distributed.
  This does not provide automatic Rust, npm, Go or Python library bindings.

These levels describe recorded coverage. Each release candidate needs its own
clean-install acceptance run against the current compiler recipes.

## Source languages

| Language and package | Level | What works | Public interface and current limits | Verification entry point |
| --- | --- | --- | --- | --- |
| C, `clang` | Integrated, scalar API | Ordinary C compilation and linking inside the LLVM Component, multi-file projects and configured headers. | Inferred public functions use scalars. Pointers, structs, varargs and ownership need authored bindings. System libraries must have compatible Wasm builds. | `clang/verify-clang-wasm.sh`; Wisp `examples/build-cpp` and source acceptance |
| C++, `clang` | Integrated | Ordinary functions, namespaces, supported plain records, strings, byte buffers and typed results. Installed Go and Rust Components can be imported through generated headers. | No arbitrary pointer APIs, recursive public records, overloaded or generic public functions. Inference reads configured sources, not arbitrary included record definitions. Native OS libraries and build systems are not automatically ported. | Wisp `examples/build-values/cpp`, `examples/cpp-import-go`, `examples/cpp-import-rust` |
| Go, `go` | Integrated | Go 1.27.2 compiler, assembler, linker and packaged standard library. Scalar, string, byte-buffer, record and error exports. Cross-language Component composition. | Portable Go guest imports are scalar-only. Rich exported values share a bounded 4 MiB transient arena. Dependencies must be available locally; the direct compiler adapter does not fetch modules. Native Go package use has separate runtime/platform requirements. | `go/tests`; Wisp `examples/build-go`, `examples/build-values/go`, import examples |
| Rust, `rust` | Integrated | Source-built Rust 1.99.1/WASIp3 compiler with linked LLVM 24. `std`, vendored crates, basic build scripts, procedural macros and Serde derives execute through Wasm stages. Rich exports and installed Go/C++ imports. | Cargo metadata runs natively and offline. Vendor dependencies first. Public generics, borrowed public values, arbitrary lists, recursive records and async source APIs are outside inference. A Rust crate that needs unsupported native facilities still needs porting. | `rust/tests/acceptance.py`; Wisp Serde and import acceptance |
| Zig, `zig` | Verified adapter | Zig 0.17.0 bootstrap frontend, comptime, generics, local imports and the freestanding parts of the standard library. Emits C, then compiles it through the separate Wasm Clang backend. | Scalar Component interface. `ReleaseSmall` disables Zig runtime safety checks. No Zig build runner, package fetching, native targets or Zig LLVM backend. General libc, OS APIs and C interop need separate integration; optional memory helpers do not establish full allocator support. | `zig/verify.py` |
| AssemblyScript, `assemblyscript` | Verified adapter | Upstream AssemblyScript 0.28.20 plus Binaryen inside Wasm, local modules, arrays, loops, indirect calls and runtime selection. | Core Wasm exports can be wrapped with scalar WIT. Managed strings/objects use AssemblyScript's ABI and are not automatically public WIT values. No JS transforms, npm resolution or `asconfig.json` frontend. Requires the Wasmtime Python callback host, not just a CLI executable. | `assemblyscript/verify.py` |
| Nim, `nim` | Verified adapter | Nim modules, generics, templates, compile-time macros, sequences and global initialization. C generation and compilation run in Wasm with shared LLVM. | Scalar Component exports. No network, threads, subprocesses, NimScript shell execution or native library loading. OS-specific standard-library modules and C libraries need individual verification. | `nim/verify.py` |
| JavaScript, `javascript` | Verified adapter | Synchronous ES modules in QuickJS, bundled local/package modules, WIT imports, lists, records, Unicode strings, results and instance state. | No Node builtins, DOM, runtime filesystem module loading or tested async/Promise API. Source and interpreter are embedded; functions are interpreted rather than individually compiled to Wasm machine code. WIT 64-bit integers mapped to JS numbers need exact-range care. | `javascript/verify.py` |
| TypeScript, `javascript` | Verified adapter | The JavaScript path plus Wasm-hosted esbuild 0.28.2 parsing, type erasure and bundling. | The adapter does not run TypeScript's type checker. WIT defines the public types. JavaScript runtime limits also apply. Run a separate typecheck in the application's CI. | `javascript/verify.py`, including TypeScript cases |
| Python, `python` | Experimental | CPython-backed Component with one annotated two-argument integer callable. Included standard-library modules, classes, comprehensions and lambdas have examples. | Main adapter exposes `add(s32, s32) -> s32`, with `--entry` selecting the function. One source file, 1 MiB source limit and 64 MiB Component admission limit. The additive authored-WIT adapter supports multiple exports and rich values after separately provisioning a matching runtime template. New templates bundle local and vendored pure Python modules, including relative and namespace-package imports. No Python contract inference, native extensions, package data or package installation. | `python/verify.py` |
| Grain, `grain` | Verified adapter | Pinned upstream compiler and Binaryen running as JavaScript inside QuickJS Wasm. Local imports, Core Wasm output and scalar Components; optional external compiler cache. | Public exports use `WasmI32`, `WasmI64`, `WasmF32` or `WasmF64`. Grain `Number`, strings and heap collections need additional ABI conversion. Startup can be slow without the external cache. | `grain/verify.py` |
| Swift, `swift` plus `llvm-swift` | Verified adapter | Swift 6.3.3 frontend/linker, core SDK, generics, ARC and collections inside the application. WASI commands and explicitly exported scalar Components. | Strings and aggregate values need public ABI adapters. No SwiftPM, compiler plugins or macros. Additional SDK modules need matching prebuilt artifacts. Compiler binaries derive from pinned upstream WASI releases; this is not a local source rebuild of Swift. | `swift/verify.py` |
| Java, `java` plus `jvm` and `teavm` | Verified adapter | ECJ 3.46.100 compiles Java 21 source to bytecode in the Wasm JVM. TeaVM 0.16.0 emits C, then the Wasm backend produces WASI. Local classpaths and scalar exports. | Uses TeaVM's portable class-library subset. Annotation processing is disabled. Arbitrary OpenJDK APIs, JNI, objects/strings/arrays across WIT and dynamic native libraries are not provided. Exports need `@Export` and matching WIT. | `java/verify.py` |
| Kotlin, `kotlin` plus `jvm` and `teavm` | Verified adapter | Kotlin 2.4.21 frontend, JVM 17 bytecode, standard library within TeaVM's supported application APIs, scalar Component exports. | No JNI, dynamic class loading, arbitrary JVM reflection or general WIT strings/objects/resources. Optional compiler profiling and native helpers are excluded. Source compilation and target runtime have different JDK API coverage. | `kotlin/verify.py` |
| OCaml, `ocaml` | Source/library adapter and command package | OCaml 4.14.2 bytecode/WASI compiler, ordered local modules, public `.mli` discovery, common value bindings and installed Wisp source integration. Command C-stub/archive APIs retain CPDF/CamlPDF 2.9.2 support. | `int` is signed 31-bit; `int32`/`int64` preserve full signed WIT ranges. Strings, bytes, immutable records, lists, options, results and constant variants are supported. No Component imports into OCaml, public recursive/polymorphic APIs, Dune/PPX/opam resolution, threads or dynamic C libraries. Exception-capable hosts are required. | `ocaml/verify.py`; Wisp `ocaml-source-acceptance.yml` for installed consumers and CPDF regression |
| Goose, `goose` | Experimental, scalar-v1 | Pinned upstream Goose frontend, generics, loops, conditionals, scalar globals and initialization. Wasm-hosted C backend. Arithmetic and cast checks are enabled. | Dynamic strings/arrays, pools, threads, queues, native libraries and foreign memory are unsupported. Programs requiring Goose data-stack regions are rejected, including some fixed aggregates. Public interfaces are typed integer/float/bool scalars. | `goose/verify.py` |
| Bend 2, Wisp `tools/bend` | Experimental, separate recipe | Wisp has a pinned Bend 2 compiler recipe, numeric inference and an executable example. | No standalone `bend` entry exists in this repository's compiler bundle manifest. Install through the Wisp recipe. Other APIs require an adjacent authored WIT contract; do not assume general heap-value or library compatibility. | Wisp `tools/bend/README.md`, `examples/build-bend` |

## Shared packages

`llvm` supplies LLVM/Clang/LLD shared Wasm modules for Clang, Nim, Zig, Goose and
OCaml backends. Rust links the matching static SDK at compiler-build time.
`llvm-swift` is tied to the exact Swift compiler images and cannot replace that
provider. `jvm` and `teavm` are shared compiler/runtime data packages for Java and
Kotlin. `wasm-tools` supplies Component assembly, validation and adapters.
Install exact dependency versions declared by each bundle.

## Consumer packages and publication

Rust, npm, Go and browser output support depends on the Component contract and
host imports, separately from the source language. See Wisp's
[target support](https://github.com/wisp-sh/wisp/blob/main/docs/target-support.md)
for resources, asynchronous values, networking and platform coverage.

The server's initial library publication targets are npm, Cargo and Go. PyPI
remains coming soon. Python source compilation above is unrelated to publishing
Python wheels. WASI command packages such as CPDF publish downloadable command
and source archives through the separate package CI route, not library bindings.

Run `python3 build.py test TOOL` for the package's declared checks. A saved
verification report or successful archive validation proves its recorded scope;
neither replaces a fresh build, installation and consumer test for a release.
