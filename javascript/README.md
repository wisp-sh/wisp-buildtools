# JavaScript and TypeScript to Wasm Components

This toolchain accepts JavaScript or TypeScript source and an authored WIT contract. It emits a typed WebAssembly Component containing QuickJS and the bundled application source. JavaScript executes as QuickJS bytecode inside Wasm.

All compiler stages run in Wasm. esbuild parses and bundles source in a WASIp1 guest; a Rust WASIp1 guest generates WIT bindings and links the Component; upstream wasm-tools validates, strips and inspects it in WASIp1. Python handles files and starts the Wasmtime host. Neither source builds nor generated applications require Node, npm, JCO, native esbuild or a native componentizer.

## Build and run

Install Python 3.11 or later and Wasmtime. The workspace discovers its pinned Wasmtime installation, or accepts `WISP_WASMTIME`. To rebuild the compiler guests from source, bootstrap also needs Rust 1.99.0/Cargo with the `wasm32-wasip1` target and Go 1.27.2. Those native compilers run only during bootstrap.

```sh
python3 bootstrap.py
python3 bootstrap.py --check
python3 compile.py examples/add.js -o build/add-js.wasm --stub-wasi
python3 compile.py examples/add.ts -o build/add-ts.wasm --stub-wasi
python3 compile.py examples/typecheck/pass/add.ts --wit examples/add.wit \
  -o build/checked-ts.wasm --stub-wasi --typecheck
python3 compile.py examples/imports.ts --wit examples/imports.wit \
  --world imported-arithmetic -o build/imports.wasm --stub-wasi
python3 verify.py
```

Once the pinned compiler guests are installed, bootstrap reuses them. `--check` checks hashes without rebuilding or downloading. `--rebuild` rebuilds them from pinned sources; `--offline` requires the source archives and Cargo dependencies to be cached. See [dependencies and bootstrap](docs/dependencies.md).

```sh
wasmtime run --invoke 'add(20,22)' build/add-js.wasm
# 42
```

`compile.py` infers an adjacent `.wit` file when `--wit` is omitted. CLI paths resolve against the caller's directory. `--wit` accepts a file or WIT package directory; `--world` selects a world. `bin/js-component-build` invokes the same Python compiler.

Each successful build publishes three files:

- `OUTPUT.wasm`, the WebAssembly Component.
- `OUTPUT.wasm.wit`, its extracted public contract.
- `OUTPUT.wasm.build.json`, source/WIT/dependency hashes, compiler and recipe hashes, stage descriptions, and output size/hash.

Existing outputs are preserved. Failed source compilation, WIT parsing, linking, initialization or size checks publish none of those files. `--emit-js FILE` additionally saves the bundled JavaScript.

The synchronous QuickJS runtime and custom-section stripping are enabled by default. The output cap is 16,777,216 bytes. `--no-opt-size`, `--keep-custom-sections`, and `--max-bytes N` adjust these choices. The runtime's private `init` export is hidden. Use another name for application exports.

`--stub-wasi` supplies zero-valued wall-clock time, monotonic time and the insecure random seed needed to initialize QuickJS; other WASI calls trap. It preserves authored non-WASI imports. Use it for pure libraries or libraries that call only their authored WIT imports. Omit it when the application needs WASI. The host must supply the imports listed in the generated WIT. Authored imports beginning with `wasi:` are also stubbed by this option.

## How compilation works

Every JS and TS module graph bundles through `esbuild@0.28.2`, cross-compiled to WASIp1. It resolves local modules and compatible packages under `--module-root DIR`, which defaults to the source directory. TypeScript parsing, type erasure, and syntax lowering happen inside that guest. Type checking is optional and defaults off. `--typecheck` runs the separate
[tsrs package](../tsrs/README.md) over the module root before esbuild and
rejects errors without publishing outputs. Provision that package alongside
JavaScript and set `WISP_WASMTIME_PYTHON` to the external Wasmtime Python
50.0.1 host. A root `tsconfig.json` controls the project when present; otherwise
the checker checks all TypeScript files with strict ES2022/bundler defaults and
allows `.ts` import extensions. Dependencies and declarations must be within
`--module-root`; the checker gets a frozen file map with no filesystem preopens.
It runs with `--noEmit`. Receipts record whether checking ran, its compiler hash,
arguments and checked input hashes. A bundled input whose bytes differ from
the checked snapshot rejects the build. WIT defines public Component types
independently of TypeScript annotations. See [esbuild's TypeScript caveats](https://esbuild.github.io/content-types/#typescript-caveats).

The componentizer guest uses `wit-dylib` and `wit-component` to generate bindings and link the upstream `componentize-qjs@0.4.5` runtime. It embeds the JavaScript source and generates initialization code. Wasm-tools then strips custom sections, validates the binary and extracts its WIT, also inside Wasm. Receipts report `sourceCompilerWasmHosted: true`, `fullyWasmHostedBuild: true`, and `nodeRequired: false`.

This build does not use Wizer snapshots. QuickJS parses and evaluates the source once when each Component instance initializes. A validation instance does the same during the build, so initialization failures reject publication. The final Component contains source and the engine, rather than a snapshot of initialized memory. JavaScript functions are interpreted by QuickJS, not compiled into individual Wasm functions.

Python orchestration and the Wasmtime host remain native executables. "Fully Wasm hosted" refers to the compiler, binding generation, linking and inspection stages. Native Rust and Go rebuild those compiler guests only during bootstrap. The upstream QuickJS runtime is a checksummed prebuilt Wasm artifact from the pinned source crate.

## Imports and modules

A WIT interface is available under its fully qualified module name:

```js
import hostMath from 'toolchain:javascript/host-math';

export function add(a, b) {
  return hostMath.add(a, b);
}
```

`examples/imports.ts` and `examples/imports.wit` provide the complete example. The verifier composes the resulting Component with a separate Wasm math provider and checks the arguments and result through the canonical ABI.

URI imports such as `toolchain:` remain external for WIT binding. Every statically resolved JS/TS source module is bundled and hashed in the receipt. The guest computes each hash from the exact bytes it gives to esbuild, so later edits cannot change the recorded input identity. The source guest can read only the chosen module root and its temporary output directory. Set `--module-root` to a project directory that includes any needed package dependencies. Runtime filesystem module loading is unavailable.

The receipt records esbuild's source inputs. Automatically discovered `tsconfig.json` files and package-resolution metadata are not included in that list, so it does not claim a complete build configuration closure.

## Limits and verification

This toolchain targets synchronous ES module libraries. It provides neither Node builtins nor a browser DOM. Bundling an npm package does not make dependencies on those APIs work. Async Component APIs and promises are outside the tested contract.

Build validation supplies trapping authored-import stubs, so call those imports from exported functions rather than top-level initialization. At runtime, initialization happens once per instance and state persists between calls. Upstream maps 64-bit WIT integers to JavaScript numbers; the verifier covers `f64` and `u32`, not integers beyond JavaScript's exact range.

`verify.py` compiles and executes JS, TS, local modules, typed imports, lists, records, Unicode strings and WIT results. It composes a Wasm harness that calls the same instance twice, checks initialization failures and atomic error handling, verifies caller-relative paths, and blocks Node/native compiler commands throughout. When tsrs is installed, it also checks a valid TypeScript project and rejects
a project with diagnostic 2322 before esbuild. It explicitly reports those
optional checks as skipped when the package is absent. It writes `build/verify/verification.json` with exact results, output sizes and hashes.

The workspace invokes `bootstrap.py`, `compile.py SOURCE -o OUTPUT --wit PATH --world NAME`, and `verify.py`. This directory emits Components directly; the parent workspace handles Wisp project integration.

## StarlingMonkey investigation

The [StarlingMonkey spike](docs/starlingmonkey.md) records a no-go migration
decision. Its checksummed prebuilt runtime runs without snapshots as a CLI
Component. The WASIp1 splicer builds, but authored-WIT exports trap when the
stock pipeline skips Wizer. A cold library initializer or Wasm-hosted snapshotter
is still needed. QuickJS remains the default engine and
the runtime limits above still apply.
