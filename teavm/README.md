# TeaVM

This shared backend compiles JVM bytecode to standalone WASI Core Wasm. The
compiler runs inside the separate `jvm` package's Wasm interpreter. Java and
Kotlin share this package and the JVM host. Java uses its portable API
signatures; Kotlin reads the shared JVM provider's JDK signatures. Wisp supplies
Wasmtime; there is no embedded engine or Node dependency.

The backend uses TeaVM 0.16.0. [TeaVM 0.14 removed its standalone WASI target](https://teavm.org/docs/release-notes/0.14.0.html), so this package uses the upstream C backend. A Wasm-hosted Clang/LLD driver compiles the generated C to WASI. It loads the shared LLVM provider; upgrading TeaVM does not rebuild LLVM. Generated applications have ordinary WASI imports and need no JavaScript host.

```sh
python3 build.py build teavm
export WISP_WASMTIME=/path/to/wisp-provided/wasmtime
python3 teavm/compile.py path/to/classes --main example.Main \
  --component --wit path/to/wit --world example -o /tmp/example.wasm
python3 teavm/verify.py
```

Omit `--component` to emit an ordinary WASI command. `--timeout` defaults to 1,200 seconds for the JVM analysis, C compilation and Component assembly together. Extra class directories or
JARs can be supplied with repeated `--classpath` options. Inputs must use the
pinned TeaVM class library's Java APIs. TeaVM's ahead-of-time model restricts
reflection, runtime class loading, resources, and JNI; see its
[upstream overview](https://teavm.org/docs/intro/overview.html). A full OpenJDK
runtime does not become part of the generated application.

The Component adapter supports explicitly named scalar static exports. Names must be C identifiers. The WASI reactor startup initializes TeaVM and runs Java `main` after the Preview 1 adapter is ready. Its dedicated `cabi_realloc_adapter` uses WASI libc's aligned allocator for fresh 64 KiB adapter blocks. The Java heap and these blocks are separate allocations. Java strings and object references are not WIT strings or canonical ABI pointers.

The generated application heap starts at 4 MiB and grows logically up to 128 MiB. WASI reserves the maximum heap and GC workspaces in linear memory at startup.
This is separate from the compiler JVM's 512 MiB heap. Verification includes
retained 8 MiB allocations and growth under different command-line layouts.

`dist/jars/` contains the TeaVM compiler and its pinned dependencies.
`dist/java-sdk.jar` contains portable API signatures derived from the same class
library using the upstream `teavm-javac` signature converter.
`dist/launcher/` contains the small batch entry point, and `dist/fixtures/`
contains a bootstrap-built test class. The shared JVM and JDK class-library data
remain in the separate `jvm` package. Compiler JARs execute as bytecode inside
Wasm, and do not run as native Java processes during application builds.

Bootstrap ports four C runtime resources for WASI: cooperative scheduler waits use WASI clocks, virtual memory uses zeroed libc allocations, the home directory comes from `HOME` with `/` as the fallback, and every dynamic array class entry is aligned to eight bytes for packed class pointers on wasm32. The manifest records each original and patched resource hash. Bootstrap also retains two checked repairs to the shared `GC` class: accept an exact eight-byte free-chunk remainder and include the free-chunk header in heap growth. Upstream 0.16 still has both boundary bugs. Every other compiler class remains unchanged. Wasm exceptions and WASI SDK's setjmp support implement Java exception handling. The C compile flags preserve wrapping signed arithmetic and allow the runtime's object-pointer casts.

`dist/bin/c-backend.wasm` is a small batch driver linked against the existing shared LLVM binaries. It applies Clang backend options for WASI setjmp lowering. Bootstrap verifies that all four extracted provider modules have unchanged code, and runtime checks the provider manifest hash. `dist/sysroot/` contains the pinned WASI SDK 34 headers, startup objects and libraries. Clang resource headers remain in the shared `clang` package and are checked against the bootstrap inventory. The Java verifier allocates 8 MiB under 16 command-line layouts and checks its Component. SDK conversion also renames nested-class metadata, which ECJ needs for valid record and lambda bootstrap attributes.

Bootstrap needs the existing LLVM SDK headers, WASI SDK 34, pinned native wasm-tools, and a native JDK 21 or newer. `WISP_BOOTSTRAP_JAVA` selects its `java`
executable, which invokes `com.sun.tools.javac.Main`; a separate `javac` binary
is unnecessary. `bootstrap.py --offline` rebuilds from cached checksum-verified
inputs. `bootstrap.py --check` verifies the distribution and current bootstrap
sources without writing files. Runtime builds verify the complete payload
inventory, hashes, and current lock. Notices for TeaVM and bundled dependencies
are in `dist/licenses/`.
