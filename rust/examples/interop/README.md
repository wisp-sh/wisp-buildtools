# Cross-language source examples

These small sources illustrate Go and C++ importing Rust, and Rust importing
Go and C++. Wisp generates the guest bindings and composes the dependency Wasm.
The sources alone do not perform a registry integration test.

For complete projects and the disposable-registry verifier, use these paths
in a Wisp checkout:

- `examples/go-import-rust`
- `examples/cpp-import-rust`
- `examples/rust-import-wasm/verify.py`

The verifier publishes and fetches provider releases, stops the registries
before consumer builds, then checks execution and editor packages. See the
[compiler acceptance suite](../../tests/README.md) for standalone Rust,
build-script and procedural-macro checks.
