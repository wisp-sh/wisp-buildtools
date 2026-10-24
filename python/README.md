# Python toolchain for Wisp

This toolchain packages a Python source file into a WebAssembly Component with
CPython 3.14. The Python parser, bytecode compiler, source initialization and
function execution run inside Wasm. Function bodies use ordinary Python syntax.

The initial typed adapter exposes one function,
`wisp:python/api@0.1.0.add(a: s32, b: s32) -> s32`. The default Python callable is
`add(a: int, b: int) -> int`. `--entry NAME` selects a different Python callable
behind that same public `add` export. Multiple exports, other signatures, custom
WIT worlds, package discovery and third-party Python dependencies still require
additional adapters. This package does not claim arbitrary Python library support.

## Build and run

From this directory:

```sh
python3 bootstrap.py
python3 compile.py examples/add/main.py -o build/add.component.wasm
wasmtime run --invoke 'add(20, 22)' build/add.component.wasm
python3 verify.py
```

The result is `42`. The workspace dispatcher also supports this toolchain:

```sh
cd ..
python3 toolchains.py build python python/examples/add/main.py -o out/python.component.wasm
```

Any two-argument integer function can back the adapter:

```python
def multiply(a: int, b: int) -> int:
    return a * b
```

```sh
python3 compile.py multiply.py --entry multiply -o build/multiply.component.wasm
wasmtime run --invoke 'add(6, 7)' build/multiply.component.wasm
```

The public WIT name remains `add`, so consumers should use the WIT emitted by
this toolchain rather than infer exports from Python source. Parameters and the
return value must have `int` annotations. Returned integers must fit signed
32-bit values. The adapter rejects `bool`, strings and out-of-range integers.

## Guest compilation and packaging

`bootstrap.py` uses the pinned native `componentize-py` wheel to build a reusable
CPython runtime from `runtime/app.py` and `wit/runtime.wit`. It runs Binaryen on
the runtime's core modules to reduce the Component below Wisp's 16 MiB limit,
then uses WAC to compose a tiny source-provider Component into the runtime.
The bootstrap tools are native host programs. This is a bootstrap for a
Wasm-hosted Python compiler, not a Wasm-hosted build of `componentize-py` itself.

`compile.py` passes the source file and entry name to `dist/python-packager.wasm`,
a WASIp1 Rust program. That guest packages the UTF-8 source as a constant
Component string and replaces the source provider in the prelinked template.
The host does not use
`ast`, `compile`, `eval`, `exec`, `py_compile`, or a native Python subprocess to
parse user source. The pinned upstream WASIp1 `wasm-tools` module validates
Component structure. Wasmtime
then invokes `validate()` on the candidate Component, where guest CPython calls
its own `compile` and `exec`. Only after that succeeds does the script replace
the requested output atomically. Failed compilation preserves an existing file.

All source compilation, source-provider binary generation, Component assembly
and explicit validation stages execute inside Wasm. Native Python only performs
file I/O, checks hashes, prepares arguments and launches Wasmtime. Node is not a
build or verification dependency. The build receipt records
`fullyWasmHostedBuild: true`.

The result embeds Python source and the interpreter, not ahead-of-time machine
code for each Python function. Each fresh Component instance compiles and
initializes the program once on first use, then reuses its module namespace.
`validate()` runs top-level program code during packaging, so source that never
finishes initializing fails after the configured `--timeout`, which defaults
to 30 seconds.

## Dependencies and artifacts

Bootstrap currently targets Linux x86_64 with Python 3.11 or later and Cargo
with the `wasm32-wasip1` Rust target installed. Cargo builds the small packager
only during bootstrap, using `packager/Cargo.lock`. The emitted
Wasm is portable. `toolchain.lock.json` pins the componentize-py 0.25.1 wheel and
the exact native tool binaries used here:

- Wasmtime 49.0.4
- wasm-tools 1.262.0
- WAC 0.12.0
- Binaryen 133, official Linux x86_64 release

The wheel downloads automatically and its SHA-256 is checked before extraction.
The other binaries are prerequisites. The scripts find this workspace's existing
tools in `../clang/.toolchains` and `../../wisp/.tools/build/wac`. You can select
their locations with `WISP_WASMTIME`, `WISP_BOOTSTRAP_WASM_TOOLS`, `WISP_WAC` and
`WISP_WASM_OPT`. Bootstrap verifies their SHA-256 values against the lock file.
Binaryen uses the official self-contained binaries. Compact imports are disabled
so the optimized runtime works with the pinned Wasmtime host. These hashes
describe this Linux x86_64 tool set.

The source build uses the shared `../wasm-tools` package instead of the
native bootstrap `wasm-tools`. Its release archive and extracted guest module
have pinned SHA-256 values. `../wasm_tools.py` runs that module under Wasmtime
and rejects a native `WISP_WASM_TOOLS` override. Bootstrap prepares this shared
package automatically.

`dist/manifest.json` records bootstrap inputs, native tool checksums, the guest
runtime version, output size and SHA-256 values for every packaged file.
`bootstrap.py` checks that manifest before reusing the package. `--force`
rebuilds it. Binaryen cache keys include the unoptimized module, optimizer binary,
shared library and optimization flags.
`verification.json` records checks from the latest successful `verify.py` run.
Generated downloads, tool caches and `dist` are ignored by Git.

## Current limits

- One source file and one two-argument `int` callable. The initial source limit
  is 1 MiB. The finished Component must also fit Wisp's 16 MiB limit.
- `collections`, `functools`, `itertools`, `json`, `math`, `re` and their loaded
  dependencies are initialized in the template. `sys` and built-in Python
  functionality are available. `examples/stdlib/main.py` checks classes,
  comprehensions, lambdas and these modules.
- Componentize-py 0.25.1 only makes preinitialized imports available at runtime.
  An arbitrary import such as `pathlib` is not promised to work. Add and verify
  required standard-library imports in `runtime/app.py`, then rebuild.
- Components import WASI 0.2.9 interfaces and need a WASI-capable host, such as
  Wasmtime or Wisp's component runtime. The packaging and verification scripts
  disable inherited environment, networking and standard I/O, and grant no
  filesystem preopens. Filesystem calls receive normal WASI denial results.
  Trapping stubs cannot replace these imports because CPython sometimes opens
  the source filename while constructing syntax-error diagnostics. A consuming
  host chooses its own runtime capabilities. Subprocesses and OS-dependent
  Python modules are outside this adapter.
- Compilation validates source initialization and function shape. A wrong return
  type or s32 overflow traps when that function is invoked.

See the [componentize-py 0.25.1 source](https://github.com/bytecodealliance/componentize-py/tree/v0.25.1)
and its [documented import limitation](https://github.com/bytecodealliance/componentize-py/tree/v0.25.1#known-limitations).
The wrapper uses the Apache 2.0 license in `LICENSE`; the bundled CPython uses
the notices in `LICENSE.python`. Bootstrap includes both in the distribution.
