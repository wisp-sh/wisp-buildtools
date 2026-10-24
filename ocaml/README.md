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
`WISP_BOOTSTRAP_WASMTIME`, then `WISP_WASMTIME`. Use the ordinary Wasmtime 49
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
