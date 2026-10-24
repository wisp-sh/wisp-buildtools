# Rust compiler hosted in WebAssembly

The active source recipe builds Rust 1.99.1 as a `wasm32-wasip3` compiler
Component using the existing LLVM 24 SDK. It builds application libraries for
`wasm32-wasip1` and procedural macro libraries for `wasm32-unknown-unknown`.
The compiler host target and application target are separate settings.

[toolchain.json](toolchain.json) records the selected release.
[source-lock.json](source-lock.json) pins official Rust sources, the matching
native bootstrap release, WASI SDK 34 and the external LLVM SDK inventory.
Run the standalone acceptance suite after building the compiler, including the
Serde derive and build-script cases. Compiler packages install to `dist/compiler`.

Build from the repository root:

```sh
python3 build.py build rust --jobs 2
python3 build.py test rust
```

The build dependency prepares LLVM 24 through the shared Clang/LLVM bootstrap.
CI restores that SDK once and supplies it to Rust. Rust does not rebuild native
LLVM or a separate LLVM 23. The Rust patch adapts its LLVM C++ wrapper to LLVM 24
and links the SDK's static LLVM and LLD libraries into the compiler Component.
Reusing the build SDK does not make this compiler dynamically interchangeable
with Clang's shared runtime provider.

See [source build instructions](docs/source-build.md) for individual commands,
source verification and build reports.

Native Cargo resolves locked metadata and vendors sources. Application Rust
compilation, build scripts and procedural macros execute in Wasm. No native
rustc is used to compile application sources. The packaged runner checks its
compiler and sysroot inventory before use.

After a successful build, run from this directory:

```sh
export WISP_RUST_WASMTIME=/absolute/path/to/wasmtime
python3 -I dist/compiler/scripts/runner.py build \
  --manifest-path examples/derive/Cargo.toml \
  --out-dir build/my-derive-example --offline \
  --report build/my-derive-example/report.json
"$WISP_RUST_WASMTIME" run build/my-derive-example/app.wasm
# 42
```

Use Wasmtime 49 with WASIp3 support. The runner enables `-Sp3=y` for the
compiler; applications still use their own target's runtime interface.
`env.sh` selects the shared Wasmtime 49 host for Rust too.

Use a fresh output directory for the first invocation. Add `--resume` to continue
an interrupted build in the same directory. Resume verifies the source snapshot,
compiler identity, command arguments, completed output hashes, execution logs,
and build-script generated files before reusing receipts. Changed sources require
a fresh directory. `--recover-receipts` can seal older local receipts that lack
resume hashes. This explicitly trusts their existing outputs and logs; use it only
for your own interrupted builds. It does not authenticate historical outputs.

Vendored dependencies must
match Cargo's checksum inventory. Basic build scripts can read package sources,
write beneath `OUT_DIR`, and emit `rustc-cfg`, `rustc-check-cfg`, `rustc-env`,
warning and rerun directives. General subprocess execution, native C build
dependencies, multi-package workspaces, custom targets and the full Cargo command
interface are outside this runner's supported subset. Native Enzyme/offload
plugins are unavailable in the WASI compiler.

Procedural macros use a bounded Wasmi interpreter and rustc's serialized token
bridge. Their modules do not receive filesystem or process APIs. The compiler
uses a serial code generation path because this WASIp3 host has no native
threads. Native compiler code paths retain their threaded implementation.

The [acceptance harness](tests/README.md) covers scalar, `std`, base64, generated
build-script sources, local macros and unmodified Serde derives. Each resulting
application must print `42`. Compiler invocations, hashes, output and macro
traces stay in the requested result directory. `runtime-adapter.json` records the tested compiler and runner hashes plus the
acceptance result hash. Raw logs and traces stay in the results directory. Wisp
registry interop has its own verifier. See [procedural macros](docs/proc-macros.md)
for the execution model and restrictions.
