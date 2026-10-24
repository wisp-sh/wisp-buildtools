# Grain

Grain 0.7.2 compiles source inside a QuickJS Wasm guest. Its OCaml frontend was
translated by upstream with js_of_ocaml. The Binaryen backend in that compiler
is upstream's wasm2js JavaScript build. Both execute inside the same Wasm guest.
Application builds use the Wasmtime host supplied by Wisp. They do not run Node,
OCaml or a native compiler.

Grain is Wasm-first for application output. Its compiler is written in OCaml, and
upstream also builds it with
[js_of_ocaml](https://github.com/grain-lang/grain/blob/grain-v0.7.2/compiler/esy.json).
A direct [wasm_of_ocaml](https://ocsigen.org/js_of_ocaml/latest/js_of_ocaml/wasm_overview.html)
build would need Binaryen bindings for that runtime's WasmGC ABI. This package
hosts the existing upstream JavaScript build in QuickJS Wasm.

```sh
export WISP_WASMTIME=/path/to/wisp/wasmtime
python3 compile.py examples/math.gr --component --wit examples/math.wit \
  --world math -o /tmp/grain-math.wasm
"$WISP_WASMTIME" run --invoke 'add(20,22)' /tmp/grain-math.wasm
```

The result is `42`. Component builds also write the decoded `.wit` contract and a
`.build.json` receipt. The source and compiler hashes are recorded in that receipt.
Existing outputs are preserved. Omit `--component` and the WIT options to emit a
linked Core Wasm command. `--project-root` selects the directory containing local
`.gr` imports. `--release` enables Grain's release profile.

The shared `wasm-tools` package provides Component assembly and the WASI Preview 1
reactor adapter. Install it beside Grain. Wisp supplies the execution host, so this
package contains no Wasmtime engine. Grain uses Binaryen and does not need LLVM.

Public Grain functions must use `WasmI32`, `WasmI64`, `WasmF32` or `WasmF64` for
scalar WIT exports. Normal Grain `Number`, `String` and collection values have a
different representation. `examples/math.gr` shows the supported raw scalar ABI.
The adapter converts Grain's `_start` export to `_initialize` before Component
assembly. Top-level code then runs after its WASI adapter is ready. Component
builds reserve a 192 KiB prefix in linear memory. The adapter uses two pages in
that prefix, and Grain manages the heap above it. This prevents their allocators
from claiming the same memory.

The package contains a roughly 20 MiB JavaScript compiler, a roughly 1 MiB QuickJS
Wasm command, 1.4 MiB of Grain library source and licences. Compiler startup parses
the JavaScript, so the first compilation takes about two minutes on this machine.
For repeated builds, `--cache-dir /path/to/cache` or `WISP_GRAIN_CACHE` enables an
external cache. It stores about 114 MiB of QuickJS bytecode plus compiled Grain
library objects. This cache is optional and is excluded from the package. Without
it, build files remain in a temporary directory removed after compilation.

Bootstrap needs Linux x86_64, Python 3.12+, system `libbrotlidec` or Python `brotli`,
and the pinned WASI SDK 34.0. It extracts the compiler as data from Grain's official
release executable without executing that executable. Every upstream archive,
the extracted compiler and the SDK compiler/linker are checksum pinned in
`toolchain.lock.json`. Native Clang builds the QuickJS guest during bootstrap.

```sh
WISP_GRAIN_WASI_SDK=/path/to/wasi-sdk-34.0-x86_64-linux python3 bootstrap.py
python3 bootstrap.py --check
WISP_WASMTIME=/path/to/wisp/wasmtime python3 verify.py
```

`--rebuild` forces a bootstrap and `--offline` uses verified cached downloads.
`dist/manifest.json` inventories the installed files. `verification.json` records
actual source compilation, scalar calls, local imports, top-level WASI I/O and
rejected input checks with `PATH=/nonexistent`.

The native release, compiler source and standard library are from
[Grain 0.7.2](https://github.com/grain-lang/grain/releases/tag/grain-v0.7.2).
The guest uses [QuickJS 2026-06-04](https://bellard.org/quickjs/).
Grain is LGPL-3.0, its standard library is MIT, and QuickJS is MIT.
Upstream notices are in `dist/licenses`.
