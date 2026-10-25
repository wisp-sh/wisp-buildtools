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

- Wasmtime 50.0.4
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
The Cargo packager build remaps checkout paths to `/wisp-buildtools`.
The runtime bundle includes only the scalar template and its checksummed files;
experimental string templates and debug outputs are excluded.
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

## Libraries with an authored WIT contract

The additive contract adapter supports multiple named exports, synchronous
scalar values, strings, lists, records, options, tuples, enums, flags, variants
and typed results through componentize-py's generated bindings. It requires a
separate reusable compiler template for each public API. The original scalar
adapter and `--entry` remain available when `--wit` is omitted.

Provision a template once, during toolchain setup:

```sh
python3 bootstrap_contract.py --wit examples/library/library.wit --world library
```

This step uses the pinned native componentize-py, Binaryen and WAC tools recorded
in `toolchain.lock.json`, plus Cargo with the WASIp1 target. It reads the contract and generates the runtime
wrapper. It never reads application source. Like the base bootstrap, this can
require substantial memory and disk space. Use a host with sufficient capacity.
Templates are stored under `dist/contracts/<contract-sha256>/`; package bundles
include provisioned templates. Generate a new template whenever the public API
changes. Application implementations can change without rebuilding the template.

The template snapshots imported modules; it does not provide every CPython
stdlib module for later dynamic imports. Provision additional stdlib dependencies
explicitly, including modules a library imports dynamically:

```sh
python3 bootstrap_contract.py --wit library.wit --world library \
  --preload-module html.parser --preload-module xml.etree.ElementTree \
  --preload-module logging --preload-module typing
```

These imports run in guest CPython during provisioning and include their transitive
imports. The manifest records the sorted, deduplicated list as `preloadModules`.
Changing the list for an existing contract requires `--rebuild`. Bundle application
and third-party source separately; omit preloaded stdlib source. Application builds
never provision templates and retain the 1 MiB source-bundle limit.

Build the example against its template:

```sh
python3 compile.py examples/library/main.py \
  --wit examples/library/library.wit --world library -o build/library.wasm
wasmtime run --invoke 'greet("世界")' build/library.wasm
wasmtime run --invoke 'total([20,22])' build/library.wasm
wasmtime run --invoke 'checked(-1)' build/library.wasm
```

The source defines ordinary `greet`, `total`, `birthday` and `checked` functions.
WIT kebab-case function names become Python snake_case names. Alternatively,
define an interface implementation class with the name shown by the generated
bindings, such as `class Api`, to keep per-interface instance state. Root exports
can use ordinary functions or a `class WitWorld` implementation. Each source
module initializes once per Component instance. Class constructors run once.

Records use the generated dataclasses, such as
`from wit_world.exports.api import Person`. Those modules are already available
inside the template. For `result<T, E>`, return the success value directly or
raise `componentize_py_types.Err(error_value)` for the typed failure. An ordinary
Python exception traps; it is not silently converted into a declared error.
The `wit_world` directory and `componentize_py_types.py` beside each provisioned
template can also supply editor types.

Source compilation and initialization, export signature checks, source-provider
replacement, final export projection and Component validation run inside Wasm.
The temporary `wisp-python-validate` export checks all required functions before
publication and is removed from the finished Component. Failed builds preserve
an existing output. The build prints a receipt with source, contract, template
manifest and Component hashes.

This contract path supports an import-free, synchronous WIT world. Resources, handles, futures, streams, inline interfaces
and world-level type exports are rejected. Export type declarations should live
in named interfaces. The aggregate Python source bundle, including its loader, and the finished
Component retain the 1 MiB and 16 MiB limits. Standard-library imports retain
the base runtime preinitialization limits. Local and vendored pure Python
modules can be bundled as described below.

Run focused checks without provisioning new templates:

```sh
python3 -m unittest discover -s python -p 'test_*.py'
python3 python/verify_contract.py
cargo test --manifest-path python/contract-packager/Cargo.toml --locked -j 1
```

The root-export example additionally checks a zero-argument boolean function,
byte buffers and optional Unicode strings:

```sh
python3 python/bootstrap_contract.py --wit python/examples/root/library.wit --world library
python3 python/verify_contract.py --example root
```

## Local modules and pure Python packages

New contract templates load bundled Python modules inside guest CPython.
Regular packages, relative imports, namespace packages, circular imports,
function-local imports and module state follow Python import behavior.
Entrypoints execute as named modules, so decorators such as `dataclass` can
find their defining module in `sys.modules`.

Select the directory containing the top-level packages with `--package-root`.
Supply extra directories of already vendored pure Python dependencies with
repeatable `--python-path` arguments. These roots must be separate directories.
No dependency resolver or package installer runs during a build.

```sh
python3 python/bootstrap_contract.py --wit python/examples/packages/library.wit --world library --rebuild
python3 python/compile.py python/examples/packages/src/calculator/api.py \
  --package-root python/examples/packages/src \
  --python-path python/examples/packages/deps \
  --wit python/examples/packages/library.wit --world library -o out/python-packages.wasm
python3 python/verify_packages.py
```

Without `--package-root`, a new template bundles the entrypoint's directory,
including its surrounding regular package when it has `__init__.py` files.
Use an explicit root for namespace packages or a wider application source tree.
Every `.py` file under each root is included and hashed, with generated caches,
build outputs and package metadata directories excluded. The receipt records
all input hashes, the entry module and the final source-bundle hash. Application
source is never imported or parsed on the host.

Older contract templates keep their single-file behavior. Upgrade one with
`bootstrap_contract.py --rebuild` to enable module imports. Rebuilding provisions
a fresh runtime template before replacing the previous one. Later changes to
application modules require only a normal build. The original scalar adapter
continues to support its existing single-file API.

Native extensions, symlinks, overlapping roots and conflicting module names
are rejected. Names reserved by the runtime, or names that shadow a preinitialized
runtime module, also receive an error. Package data files, installed-distribution
metadata and arbitrary filesystem imports are not bundled. A pure Python package
must use the preinitialized standard-library modules supported by the runtime.
