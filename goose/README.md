# Goose

The pinned aardappel/Goose compiler runs in WASI and emits C. The packaged C
backend runs Clang and LLD inside Wasm using the shared LLVM package. Python
only stages files and starts the Wasm tools. Application builds require no
native Goose, C compiler, linker, Node or npm.

## Build and install

From the buildtools root:

```sh
python3 build.py build goose
python3 build.py test goose
python3 build.py package goose
```

Bootstrapping needs WASI SDK 34 and the shared C backend built by `nim`.
The resulting Goose package includes its own copy of that backend, the Goose
standard library, a WASI sysroot, runtime support and licenses. Installed builds
need the shared `llvm` and `clang` packages, plus a Wisp execution host with Wasm
exception support. The frontend uses exceptions for compiler diagnostics. When the shared LLVM
host lacks that feature, `WISP_GOOSE_FRONTEND_HOST` selects an additional
exception-capable host. In Wisp this is also `build.tools.goose-frontend-host`.
Goose is pinned by commit and source archive SHA-256 in `toolchain.lock.json`.
`src/frontend-patches.json` records the wasm32 and single-threaded header fixes.

```sh
source env.sh
python3 goose/compile.py goose/examples/add.goose -o /tmp/add.wasm
```

`--project-root` selects the root containing the entrypoint. Local imports and
Goose-source standard-library imports resolve inside the mounted source trees.
Package fetching and native build recipes are unavailable; vendor local modules.
`--work-dir` retains generated C, export headers and the backend command manifest.
`-O0`, `-O1` and `-O2` select optimization, with `-O1` as the default.

## Supported profile

This initial `scalar-v1` profile supports explicitly typed scalar exports,
ordinary functions, generics, loops, conditionals, scalar globals and fixed-size
values whose compiled implementation needs no Goose data-stack regions.
Runtime arithmetic and cast checks are enabled in this profile. Fixed aggregate
layouts that differ between native Goose and wasm32 C are rejected.
The WASI reactor constructor calls `goose_init` before any export, including
running a Goose `main` once when one exists.

Dynamic arrays, dynamic strings, pools, threads, queues, native libraries and
foreign memory are unsupported. The compiler checks the emitted program's
stack-region requirements and rejects them before writing output. It does not
replace Goose's native guard pages with unchecked heap allocations. Even a
fixed-size value may require a data stack when the backend lowers it that way.
Goose's full Wasm memory model remains separate work; see its upstream
`docs/design/wasm.md` proposal.

Exports accept and return integer scalars, floats and booleans. Compound C APIs,
borrowed strings and WIT imports require additional ABI adapters. Direct world
exports must match their Wasm symbol names. Wisp derives a WIT contract for
plain typed names such as `add`; alternatively provide a matching `.wit` file.
The adapter emits Core Wasm. Wisp embeds the contract, applies its pinned WASI
Preview 1 reactor adapter and validates the final Component.

`verify.py` runs actual compiler guests, checks initialization and imports,
rejects unsupported memory use and invalid source, and repeats compilation
with native tools removed from PATH. `work/verification.json` records results.
