# Cross-language acceptance sources

These original source-only sketches are retained for reference. The implemented
Wisp examples and their passing disposable-registry verifier are now in the
sibling Wisp repository:

- [Go importing Rust](../../../../wisp/examples/go-import-rust/README.md).
- [C++ importing Rust](../../../../wisp/examples/cpp-import-rust/README.md).
- [Rust importing Go and C++](../../../../wisp/examples/rust-import-wasm/README.md).

The [verifier](../../../../wisp/examples/rust-import-wasm/verify.py) publishes real
provider artifacts, fetches them into fresh consumers, stops the registries,
compiles the consumers using the Wasm-hosted compilers, and invokes the composed
results. Native Go, ordinary Cargo check and relocated clangd checks verify the
editor-facing packages too. Generated source tampering is rejected.

Applications use normal Go imports, C++ includes and Cargo dependencies. Wisp
creates the guest bindings and composes the dependency Wasm. This proves the
scalar compiler integration checkpoint. Serde derives still require the macro
compiler port before the first usable Rust release.
