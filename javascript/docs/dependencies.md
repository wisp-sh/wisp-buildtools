# Dependencies and bootstrap

Source builds require Python 3, a Wasmtime host, and the checksummed compiler guests. Node and npm are absent from the build path. Native Rust and Go are needed only when creating those guest artifacts during bootstrap.

```sh
python3 bootstrap.py
python3 bootstrap.py --check --json
python3 bootstrap.py --rebuild
python3 bootstrap.py --rebuild --offline
```

Normal bootstrap reuses verified artifacts. `--check` performs read-only checks and fails if anything is missing or changed. `--rebuild` invokes Rust/Cargo and Go to create the Wasm compiler guests from pinned source inputs. `--offline` requires the upstream archives, Cargo dependencies and target libraries to be cached. Set `WISP_JAVASCRIPT_BOOTSTRAP_GO` or pass `--go` to select the native Go bootstrap compiler. Compiler source builds never execute user JavaScript or TypeScript natively.

## Compiler inputs

| Input | Pin | Role |
| --- | --- | --- |
| esbuild | 0.28.2 | JavaScript/TypeScript parser and bundler, compiled to `wasip1/wasm` |
| Rust | 1.99.0 | Bootstrap compiler for the Component generator guest, with the `wasm32-wasip1` target |
| Go | 1.27.2 | Bootstrap compiler for the esbuild guest |
| componentize-qjs | 0.4.5 | Upstream QuickJS synchronous Wasm runtimes and shim-generation source |
| wit-dylib, wit-parser, wit-component, wasm-encoder, wasmparser, wasm-compose | 0.258.0 | WIT bindings, Component encoding/validation and import composition in the Rust guest |
| wasi-preview1-component-adapter-provider | 47.0.4 | Preview 1 reactor adapter linked into generated Components |
| upstream wasm-tools | Pinned by `../wasm-tools/toolchain.lock.json` | Component validation, stripping, WIT extraction and verifier fixtures, executed as WASIp1 |

`checksums/wasm-tools.json` pins the installed local guests, the upstream runtime archive URL/hash, and the extracted runtime hashes. Runtime Wasm is extracted from the published `componentize-qjs` Rust source crate. This avoids the npm native addon and its Node loader. A normal bootstrap never rewrites checksum pins to accept changed artifacts.

`tools/build-esbuild-wasi.py` downloads the esbuild source ZIP from the official Go module proxy and checks its SHA256 before compiling. The pinned Go module sum is recorded alongside that archive hash. The guest wrapper uses esbuild's public Go API, preserving WIT imports with its resolver plugin. The WASI dependency graph uses only the Go standard library; bootstrap removes upstream's unused native-only `x/sys` module requirement in the staged `go.mod`, retains the original Go language version, and records that file's digest. It disables module network resolution during compilation.

The builder writes `.toolchains/esbuild-build.json` with the source archive, wrapper, staged module, Go version and output hashes. Its target is `wasip1/wasm`; it does not use esbuild's JavaScript/Node host loader. The guest's file-loader hook hashes the exact source bytes returned to esbuild, and the host requires a guest digest for every bundled input. It does not reread files after compilation to guess their identity. `tools/componentize-wasi/Cargo.lock` pins the Rust guest's dependency graph. Guest source and runtime licenses remain upstream licenses. The Rust bootstrap remaps repository, Cargo registry and standard-library source paths before compilation. It passes each remap as an encoded Cargo argument so directories containing spaces work too. This keeps the compiler guest identical when the pinned Rust installation includes `rust-src` or uses another local directory.

## Runtime and receipts

The Python wrapper starts esbuild, Component generation and wasm-tools as Wasm guests under Wasmtime. QuickJS validates source initialization inside the generated Wasm Component. No NAPI addon, native Component linker, JCO process, native esbuild, or Wizer snapshot participates.

The final Component embeds the bundled source and compiles it to QuickJS bytecode when each new instance initializes. A build validation instance catches top-level exceptions. The final artifact is not a saved build-time heap.

With `--stub-wasi`, tiny Wasm implementations return zero for `wall-clock.now`, `monotonic-clock.now` and `insecure-seed`. These permit QuickJS startup without external WASI services. Other WASI calls trap; the default build retains normal WASI imports.

Build receipts record `fullyWasmHostedBuild: true` and `nodeRequired: false`. This describes the compiler stages. Python still coordinates file operations and Wasmtime is the native execution host. Receipts identify exact installed artifacts and outputs; they do not establish reproducible builds of upstream QuickJS from source or trust in a replaced host runtime.

`verify.py` places rejecting commands for Node, npm, native esbuild/componentize-qjs/JCO/wasm-tools, Cargo, Rust and Go first on PATH. It executes successful and failing source builds, bootstrap checks and Component calls with those guards active, and fails if any guard was invoked.

When updating dependencies or the bootstrap recipe, review source pins and licenses, rebuild the Wasm guests, update the explicit artifact hashes, and run the verifier. Before updating the componentizer pin, compare clean builds from different checkout and Cargo-home locations, with and without `rust-src`. Generate the pin with the bootstrap remaps; an unremapped build embeds local paths and has a different hash. Ordinary compilation checks the installed hashes before accepting source files.
