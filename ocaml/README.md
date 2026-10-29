# OCaml

OCaml 4.14.2's bytecode compiler runs in a WASI Preview 1 bytecode runtime.
The compiler emits embedded bytecode as C. The packaged C backend compiles that
C and application stubs, then links the WASI executable using the external
shared LLVM package. Application builds use Python and Wasmtime. Native OCaml,
C compilers, Node, opam and network access are unnecessary after bootstrap.

```sh
source ../env.sh
python3 ../build.py build ocaml
python3 ../build.py test ocaml
python3 ../build.py package ocaml
python3 compile.py examples/check.ml -o /path/to/new/check.wasm
```

The check example expects `input.bin` containing bytes `00 ff 80 01` in its
working directory. It prints `42` and writes the same bytes to `output.bin`.

```sh
"$WISP_BOOTSTRAP_WASMTIME" run -W exceptions=y --dir /path/to/data::/ check.wasm
```

`WISP_OCAML_WASMTIME` selects the OCaml compiler host. It defaults to
`WISP_BOOTSTRAP_WASMTIME`, then `WISP_WASMTIME`. Use the ordinary Wasmtime 50
distribution with exception support. The current Wisp worker also enables exception instructions. Older minimal
Wisp hosts need to be rebuilt before they can run this package. `WISP_WASMTIME` selects the Wisp host used for the
shared LLVM C backend. Set `WISP_LLVM_TOOLCHAIN`, `WISP_LLVM` and
`WISP_CLANG_RESOURCE_DIR` when the dependencies are installed elsewhere.

## Bootstrap

`bootstrap.py` verifies the source archive against `toolchain.lock.json`, builds
a native bootstrap compiler, cross compiles the runtime with WASI SDK 34, and
packages the bytecode compiler, standard library interfaces and archive, runtime
headers and archive, WASI sysroot and C backend. Native tools run during this
bootstrap only. Generated files live in `dist/` and `.toolchains/`.

`--wasi-sdk` selects the SDK. `--work-dir` selects fresh build storage. `TMPDIR`
controls compiler scratch storage. `--package-from` packages an existing build
with `native/` and `target/` trees; `--host-tools` selects native `ocamlrun` and
`sak` when those trees are on storage that disallows native executables.

The source patch disables computed goto interpretation for WASI, fixes a signed
header offset, uses WASI entropy, and removes dependencies on unavailable process
IDs and debugger sockets. SDK setjmp support uses modern WebAssembly exceptions.
The runtime has 32 bit OCaml integers. Native code generation, threads, dynamic
C libraries and Unix subprocesses are outside this package. Applications can
provide C stubs for the WASI operations they need.

`compile.py` accepts one main source, additional `--source` arguments in
dependency order, and `--project-root` for local modules. It emits a standalone
WASI command and a receipt with compiler and source hashes. For application
archives and C stubs, `runner.py` and `backend.py` expose the same stages to
package recipes. The sibling `wisp-pkgs/cpdf` recipe builds CPDF and CamlPDF
2.9.2 this way.

## Public libraries

```sh
python3 compile.py examples/library/support.ml examples/library/library.ml \
  --component -o /path/to/new/library.wasm
```

The last implementation's `.mli` declares the public functions and types.
The adapter derives a WIT interface, registers OCaml callbacks and generates
canonical ABI C bindings. Both application compilers and Component assembly run
in Wasm. Install the shared `wasm-tools` package as well as `llvm` and `clang`.
The command mode and the `runner.py`/`backend.py` API used by CPDF remain available.

Pass multiple sources in dependency order. Adjacent `.mli` files are added before
their implementations. `--project-root` includes local and vendored source modules
in the hash inventory. No `.cmi` or `.cmo` files are written to the source project.
`--infer` prints the public declarations and source hashes without executing
application code. Wisp uses that result to create its package's WIT interface.
`--wit` and `--world` select an authored contract, which must match the `.mli`.
`--entry-module` selects another top-level implementation module.

Public values include `int`, `int32`, `int64`, `float`, `bool`, UTF-8 `string`,
`bytes`, immutable records, constant variants, lists, options and results.
OCaml `int` is signed 31-bit on this runtime; its WIT `s32` input is range checked.
Use `int32` for the full `s32` range. `bytes` carries arbitrary binary data.
A sole `unit` parameter becomes no parameters; `unit` results have no value.
Type aliases are supported, except unit aliases. Public polymorphic, recursive,
abstract, mutable-record, labelled, optional, higher-order or payload-variant
APIs are rejected. WIT imports, resources and asynchronous APIs are unsupported.
Dune, PPX and opam resolution are not provided; vendor ordinary modules locally.

The runtime initializes once per instance. Bindings root callback values across
GC and free canonical result buffers during post-return. Returned `Error` values
remain recoverable. Uncaught exceptions trap the instance. Signature text is
limited to 64 KiB, source snapshots to 32 MiB/4,096 files, and generated result
allocations to 16 MiB per call. Existing output and receipt files are preserved
when a build fails or an output path already exists.

`verify.py` retains command/file-I/O regression checks and compiles the rich
library example. Wisp's `ocaml-source-acceptance.yml` adds fresh package
installation, npm/Rust consumers, repeated calls with full GC, source
invalidation and a CPDF/CamlPDF rebuild. Those runs use the changed installed
adapter and verify the reused compiler payload hashes. No native application
compiler fallback is allowed.
