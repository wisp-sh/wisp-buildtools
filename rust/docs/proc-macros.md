# Procedural macros in the Wasm-hosted compiler

The selected Rust 1.99.1 compiler executes procedural macros through Wasmi
inside its own Wasm process. Applications use ordinary Cargo dependencies and
Rust macro declarations. The runner compiles macro crates for
`wasm32-unknown-unknown` and application crates for `wasm32-wasip1`.
Native Cargo resolves locked, offline metadata. Macro compilation and execution
run inside Wasm.

[The compiler patch](../patches/0001-rust-1.99-wasi-proc-macros.patch) supplies the
macro loader and serialized token bridge. The compiler's `DynClient` dispatches
RPC messages between the macro and rustc's `proc_macro` server. The compiler
and its macro sysroot must share that bridge revision. Do not mix compiler
and standard-library artifacts from different builds.

## Restrictions

Macro guests receive the compiler RPC import, with no direct filesystem,
environment or process APIs. The interpreter has bounded execution resources.
Crates that depend on host OS facilities need a port. Native shared-library
macros and general build-script subprocesses are unsupported.

Build scripts are separate Wasm programs. They can read package sources, write
under `OUT_DIR`, and emit the Cargo directives supported by the runner. See
[the Rust README](../README.md) for the supported directives and other limits.

## Verify macro execution

From the repository root:

```sh
python3 build.py build rust --jobs 2
python3 build.py test rust
```

The [acceptance suite](../tests/README.md) includes derive, function and
attribute macros, helper attributes and unmodified Serde derives. It requires
completed interpreter traces tied to the compiler invocation and hashes of
the actual macro artifacts. A loader setting alone cannot pass those checks.
Each application must execute and print `42`.

Logs, macro traces and Cargo metadata stay in the selected test output
directory. The installed compiler's `runtime-adapter.json` records the tested
compiler and runner hashes, completed cases and acceptance result hash.
Packaging retains that summary and the source locks. It excludes raw test
output. Compiler acceptance and Wisp registry interoperability are separate
checks.
