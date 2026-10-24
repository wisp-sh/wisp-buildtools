# Swift compiler hosted in Wasm

This package runs the real Swift 6.3.3 frontend and LLVM linker in WebAssembly.
The SDK contains Swift's standard-library modules and Wasm runtime archives.
Application builds need Python orchestration, Wisp's shared Wasmtime hosts and
the external `llvm-swift` and `wasm-tools` packages. They use no native Swift
compiler, native linker, Node or SwiftPM.

```sh
python3 bootstrap.py
python3 compile.py examples/main.swift -o /tmp/swift-program.wasm
"$WISP_WASMTIME" run /tmp/swift-program.wasm
python3 compile.py examples/add.swift --component --wit examples/add.wit \
  --world math -o /tmp/swift-component.wasm
"$WISP_WASMTIME" run --invoke 'add(20,22)' /tmp/swift-component.wasm
python3 verify.py
```

Pass several source files to compile a module. Component inputs use Swift's
`@_expose(wasm, "name")` and `@_cdecl("name")` attributes with a matching scalar
WIT contract. Swift structs, collections, generics and ARC work inside those
functions. Strings and other aggregates need public ABI conversion before they
can cross the authored WIT boundary.

Component builds use the SDK's reactor CRT and its `_initialize` export.
A generated Swift shim gives the Preview 1 adapter allocations from the same
libc heap as Swift. The dedicated `cabi_realloc_adapter` export handles the
pinned adapter's two persistent page allocations. Verification exercises WASI
printing and an 8.4 MB array, including linear-memory growth.

`WISP_LLVM_SWIFT` selects the external provider, defaulting to the canonical
sibling `llvm/dist/swift-shared`. The provider uses an exact-image shared table
ABI. It holds LLVM, Clang, LLD, C/C++ runtime and unattributed support functions.
Compiler primary modules retain identified Swift frontend functions and own the
memory, data and tables. This provider is tied to these compiler images and is
separate from the LLVM 24 C++ ABI. Its manifest records the original module
hashes, function counts, selection policy and compressed selected-name evidence.

Wisp provides the execution engines outside compiler packages.
`WISP_WASMTIME_PYTHON` points to its Wasmtime 49 Python binding when that binding
is outside the host Python installation. The loader instantiates the primary,
then its backend, then invokes the entry point. `WISP_WASMTIME` runs the shared
Wasm tools and completed programs. Engine compilation caches remain host files
and are excluded from packages.

Bootstrap consumes checksum-pinned published WASI artifacts from
[swift-toolchain-wasm](https://github.com/tothambrus11/swift-toolchain-wasm/releases/tag/swift-6.3.3-wasm).
It preserves their original modules under `.cache/originals`, separates the
backends with pinned Binaryen 133 tools, and strips debug custom sections. Compact imports and custom descriptors stay disabled for the pinned host. The host caches compiled modules and disables JIT optimization of the compiler itself.
`WISP_WASM_OPT` and `WISP_WASM_OPT_LIBRARY_DIR` select those bootstrap tools.
`--check` verifies installation; `--offline` uses verified cached downloads;
`--rebuild` repeats the post-link derivation. Application compilation runs no
Binaryen native tools. Building the upstream compiler from source needs about
40 GB; this installer uses its published modules and does not claim a local
source build or a source archive ownership inventory.

The upstream port omits compiler plugin processes, SwiftPM and Swift-implemented
optimizer passes. Macros are unavailable in this profile. The shipped SDK is the
upstream core SDK; additional SDK modules require matching prebuilt Swift 6.3.3
artifacts. Upstream licenses and port notices are retained in `dist/licenses`. The Wisp
loader and build scripts use the license in `LICENSE`.

For source packages with many files, `--whole-module` compiles all inputs in one
frontend invocation and links one object. Repeat `--define NAME` for conditional
compilation flags, for example `--define COLLECTIONS_SINGLE_MODULE`. Both options
are recorded in the build receipt. The default still compiles one primary file
at a time. The application source files retain their original boundaries.

Precompiled target dependencies can be passed with repeated
`--module-search-path DIRECTORY` and `--link-object FILE` arguments. The runner
stages them into its private build directory and records their checksums. These
inputs must match the target Swift compiler and WASI SDK. This supports source
package drivers that emit modules separately without SwiftPM.

The Collections experiment adds opt-in host profiles selected by
WISP_SWIFT_HOST_BACKEND. `winch-compatible` lowers bottom function nulls in
element segments only. `winch-sharded` additionally attempts a Binaryen
compiler-artifact split. `winch-original` uses the checksum-pinned cached
original images. No profile rebuilds Swift from source. Their Collections
builds were blocked by 2.0-2.3 GiB OOM kills on the constrained host and are not
claimed as verified. multisplit.py uses the pinned native Binaryen bootstrap
tools, only for compiler images, and must run under the caller's memory lock.
precompile.py warms one compiler image in isolation under that same lock.

`--minimal-imports` omits implicit concurrency and string-processing module
imports for source packages that do not need them. Module search paths and
linked target objects are recorded in receipts. Application compilation and
linking still execute in Wasmtime in every profile.

The `streamed` host profile uses a streaming compiler-image transform instead of
Binaryen's in-memory IR. Prepare the exact LLVM frontend image explicitly:

```sh
python3 shard.py --input ../llvm/dist/swift-shared/swift-frontend-llvm.so.wasm \
  --out /absolute/cache/frontend --shards 8 --name swift-frontend
export WISP_SWIFT_HOST_BACKEND=streamed
export WISP_SWIFT_SHARDS=/absolute/cache/frontend
```

Preparation uses an offline locked native Rust tool for compiler artifacts.
Application execution only verifies the prepared files and runs Wasm compiler
guests. It never builds or transforms an image implicitly. The host loads the
original primary, shared global coordinator, all function shards, then the entry
module. The linker continues to use its original backend.

The eight derived frontend shards preserve all 146,227 original functions and
share memory, globals and callback tables. The largest module is 16,807,404 bytes
instead of 84,550,193 bytes. Aggregate module bytes increase to 95,132,364.
Transformation peaked at 132,284 KiB RSS. These numbers describe preparation;
compiler loading and application compilation need separate measurements.
See `shard_tool/README.md` for supported layouts and differential fixture results.

`WISP_SWIFT_CACHE_CONFIG` accepts a Wasmtime cache configuration file to isolate
cold and warm measurements. `WISP_SWIFT_PROFILE_LOG` enables JSONL module-loading
and guest-execution measurements; `%p` in the path expands to the process ID.
The loader maps source bytes during compilation to avoid two full Python copies.
Run `precompile.py swift-frontend:state` or
`precompile.py swift-frontend:shard-000` to warm an individual prepared module.
Callers must hold the shared compiler memory lock during warmup and execution.
