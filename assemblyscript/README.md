# AssemblyScript compiler in Wasm

Compile AssemblyScript into Core WebAssembly with the upstream compiler and Binaryen backend running as Wasm modules in Wasmtime.

```sh
python3 bootstrap.py
python3 compile.py examples/add.ts -o add.wasm
python3 verify.py
wasmtime run --invoke add add.wasm 20 22
```

The last command returns `42`. The workspace coordinator can wrap the generated Core Wasm as a Component for Wisp.

Bootstrap requires Node 20+, npm 10+, Python 3.10+ and network access. It uses the pinned npm compiler once to compile upstream AssemblyScript 0.28.20 into Wasm, extracts the matching Binaryen 131.0.0-nightly.20260721 Wasm, and generates fixed ABI adapters. Source archives have pinned SHA-256 checksums. `package-lock.json` pins npm package integrity. `dist/manifest.json` records every resulting compiler and standard-library file.

Normal builds require Python, the bootstrapped `dist/` directory, and Wisp's Wasmtime Python API. Set `WISP_WASMTIME_PYTHON` to the host module directory outside the compiler package, or install the binding in the host Python environment. Version 49.0.0 is tested. The compiler package contains no Wasmtime wheel, Python binding, or native engine library. Bootstrap uses the same host API to generate its fixed ABI adapters. They do not invoke Node, a native AssemblyScript compiler, or a native Binaryen executable. Each build checks the compiler, backend and standard-library hashes against `dist/manifest.json` before loading the compiler. Successful builds replace the output atomically. A failed compilation preserves an existing output. `verify.py` removes executable tools from `PATH` while compiling its examples.

The fixed adapters between AssemblyScript's compiler and Binaryen's ABI are
compiled into Wasm during bootstrap and included in the checksummed package.
User builds load those modules directly and never compile host-generated WAT.
The manifest and verification report record `fullyWasmHostedBuild: true`.

Wisp's host supplies the Wasmtime engine, file reads, UTF-16 strings, and Emscripten ABI callbacks. A Wasmtime CLI alone cannot provide the callback ABI used by these modules. Parsing, type checking, code generation and Binaryen passes run inside Wasm. The bundle uses two Wasm modules with separate memories. It is not a single WASI command module.

The compiler accepts ordinary upstream AssemblyScript, its standard library, and relative TypeScript file imports. `examples/algorithms.ts` uses a separate module, arrays, loops, floating-point exports and assertions. `examples/abi.ts` checks indirect function calls, an explicit host import, 64-bit integers and a UTF-16 string. Runtime `abort` traps within the generated module, so programs that only use abort need no `env.abort` import. Other explicit external imports remain the application's responsibility. JavaScript transforms, `asconfig.json`, npm package resolution, and generated JavaScript bindings are not exposed by this frontend.

`--runtime incremental` is the default. `minimal` and `stub` are available. Builds keep assertions and default to `-O0`. `-O1` through `-O3` request upstream optimization and can take longer on larger programs.

`bootstrap.py --scan-source` runs the optional `is-malicious` scanner before running the downloaded sources. Configure the scanner credentials in your environment if you use that option.

AssemblyScript and Binaryen use Apache-2.0. Bootstrap copies their license notices into `dist/licenses/`. The Emscripten callback code in `src/runtime.py` follows the glue shipped in the pinned Binaryen npm package.
