# Bounded macro bridge check

This development check compiles the patched Rust 1.83 `proc_macro` library
with the pinned native Rust 1.82 beta compiler. It builds a tiny macro module
for `wasm32-unknown-unknown` and the real Wasmi loader for both native and
`wasm32-wasip1` hosts. The Wasm host runs under Wasmtime and interprets the
macro module inside its own Wasm instance.

It verifies all three declaration kinds, derive helper attributes, returning
the original token-stream handle, two-input attribute calls, and actual
serialized `to_string` and call-site span RPC. It also requires successful
interpreter trace records for each invocation.

The fixture constructs declarations directly. This does **not** prove that
rebuilt rustc accepts normal `#[proc_macro]` crates, infers macro dependencies,
or compiles Serde derives. Those remain integration acceptance requirements.
The native compiler is a development bootstrap tool in this check, never an
application compilation fallback.

The selected Rust source must already have
`../0001-rust-1.83-wasm-proc-macro-bridge.patch` applied. The native stage0
prefix needs the matching host, `wasm32-unknown-unknown`, and `wasm32-wasip1`
standard libraries. Use the pinned 2024-09-04 beta components, not a different
installed toolchain. Additional target component SHA256s checked against
Rust's official distribution files are:

- `rust-std-beta-wasm32-unknown-unknown.tar.xz`: `414608d21dd7cd268ad57a6be916b0be2c95433e2bc14083ff8c4a7a7cc8952b`
- `rust-std-beta-wasm32-wasip1.tar.xz`: `799ee151f0aa3c5ec132f03c42152393d966c9249972967133edef56b30d66ae`

Run from the `rust/` directory, substituting your stage0 prefix:

```sh
python3 patches/port-check/check.py \
  --source build/source-rust-cf327c206854 \
  --rustc build/source-rust-cf327c206854/.wisp-port-check/stage0/bin/rustc \
  --cargo build/source-rust-cf327c206854/.wisp-port-check/stage0/bin/cargo \
  --wasmtime build/bootstrap/wasmtime-v38.0.4-x86_64-linux/wasmtime \
  --out-dir build/macro-port-check
```

The output directory must be fresh. `result.json` labels the result as a
development bridge proof. Its native and Wasm trace files contain actual
loader and invocation records. Cargo uses the checked-in dependency lock;
compilation uses at most two jobs and has a ten-minute limit per command.

The proof reverses the selected patch in dry-run mode before and after the
run, and records its SHA256 in `result.json`. Keep generated results and traces
outside the source tree. This fixture covers the legacy Rust 1.83 bridge;
[current compiler acceptance](../../tests/README.md) covers the selected release.
