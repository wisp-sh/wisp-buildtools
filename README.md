# Wisp build tools

> Pre-alpha. APIs, package formats and supported interfaces may change.

Source repository: [wisp-sh/wisp-buildtools](https://github.com/wisp-sh/wisp-buildtools).

See the [language support matrix](docs/language-support.md) for verified features, API limits, hosts and per-language checks.

Build and package WebAssembly-hosted compilers for Wisp. Each language directory
contains its bootstrap scripts, compiler adapters, examples and verification
commands. Compiler packages can be built from this repository alone. Wisp is
needed for the optional application integration checks.

| Directory | Current contents |
| --- | --- |
| [clang](clang/README.md) | C/C++ compiler hosted in a WASIp3 Component |
| [go](go/README.md) | Go compiler, assembler and linker hosted in WASIp1 |
| [goose](goose/README.md) | Goose WASI frontend and shared C backend with scalar APIs |
| [grain](grain/README.md) | Grain compiler and standard library running inside a QuickJS Wasm guest |
| [ocaml](ocaml/README.md) | OCaml bytecode compiler and runtime hosted in WASI, with a Wasm C backend |
| [llvm](llvm/README.md) | Shared LLVM Wasm libraries, sources and patches |
| [rust](rust/README.md) | Wasm-hosted Rust compiler and offline Cargo orchestration |
| [swift](swift/README.md) | Swift compiler and linker hosted in WASI with an external backend package |
| [java](java/README.md) | Java 21 source compilation through ECJ and TeaVM inside the shared Wasm JVM |
| [kotlin](kotlin/README.md) | Kotlin compilation through the shared Wasm JVM and TeaVM backend |
| [jvm](jvm/README.md) | Shared Wasm JVM and JDK class data for JVM compiler tools |
| [teavm](teavm/README.md) | Shared bytecode to WASI backend and portable Java API SDK |
| [zig](zig/README.md) | Zig Wasm frontend and hosted Clang/LLD backend |
| [assemblyscript](assemblyscript/README.md) | AssemblyScript and Binaryen Wasm modules |
| [nim](nim/README.md) | Nim WASI frontend and hosted Clang/LLD backend |
| [javascript](javascript/README.md) | JavaScript and TypeScript Components using QuickJS and esbuild Wasm |
| [python](python/README.md) | CPython WASI source compilation and Component packaging |
| [tsrs](tsrs/README.md) | Shared WASIp1 TypeScript checker with isolated filesystem callbacks |
| [wizer](wizer/README.md) | Adapter for normal Wizer in the approved native Wasmtime host |
| [wasm-tools](wasm-tools/README.md) | Shared WASIp1 WIT and Component assembly tools |

## Getting started

Use Linux x86_64 with Python 3.12 or newer. Bootstrap requirements differ by
language; read its README before starting a build. Downloads, compiler packages
and build caches are generated locally and ignored by Git.

```sh
git clone https://github.com/wisp-sh/wisp-buildtools.git
cd wisp-buildtools
python3 build.py list
python3 build.py plan javascript --json
```

The plan lists the build commands and prerequisites without running them.
For the same pinned prerequisites used in CI, see [CI builds](docs/ci.md).
Start with one toolchain; building LLVM or Rust can require substantial memory
and disk space. See [CONTRIBUTING.md](CONTRIBUTING.md) for development checks.

## Build toolchain packages

Use the same root commands for every toolchain:

```sh
python3 build.py list
python3 build.py plan javascript --json
python3 build.py build javascript
python3 build.py test javascript
python3 build.py package javascript
python3 build.py sizes javascript
```

`build` prepares dependencies and builds the compiler package. `test` runs its
verification suite. `package` creates a Wisp toolchain bundle, inventory and
checksum under `dist/`, plus interactive HTML, Markdown and JSON size reports.
Use `all` to select every toolchain. Application source compilation is the
separate `toolchains.py build` interface documented below.

To compare all installed compilers without rebuilding them:

```sh
python3 build.py package all --output-dir dist/packages
python3 build.py sizes all --package-dir dist/packages
# Open dist/packages/sizes.html
```

The report separates download, installed, Wasm, native host and shared dependency
bytes. Each tool lists its host requirements and external package dependencies.
Clang and Nim load the shared LLVM package. Zig uses the shared Clang package.
Wisp supplies the execution hosts; compiler bundles do not include Wasmtime.
See [packages and size accounting](docs/packages.md) for bundle contents,
validation, installation and the boundary between compiler and host sizes.

### Native compiler artifacts

`build.py build` and `build.py package` prepare `.cwasm` companions for every
compiler guest and shared LLVM module. Packages include those artifacts under
`precompiled/TOOL/`, their source and native hashes, execution profiles, and the
shared native loader. The original Wasm inventories remain available for
provenance checks and rebuilding after a host upgrade. Composition inputs such
as Preview 1 adapters and CPython templates remain Wasm data.

Source the environment file to select the native launcher and Wasmtime 50 host:

```sh
source env.sh
python3 build.py build javascript
python3 build.py package javascript
```

CLI adapters and shared `--preload` modules load matching native companions.
AssemblyScript, its ABI shims, and Swift's primary and backend modules use their
Python embedding's own serialized artifacts with Wasmtime Python 50.0.1.
These profiles retain each embedding's engine configuration and target a CPU
baseline for the host architecture. Java, Kotlin and TeaVM share the native JVM
guest. Wisp's worker-based compiler calls prepare and reuse checked native
artifacts in a host-only compiler cache.

To convert an existing installation without rebuilding its Wasm guests:

```sh
python3 build.py precompile all --automatic --output-dir dist/native-tools
```

`--automatic` selects the execution profiles in `cwasm_profiles.py`. The output
contains native guests and `cwasm-manifest.json`; distribute these together with
the toolchain's libraries and notices. Native companions in regular toolchain
bundles use platform `linux-x86_64`. `package --source-only` is available for
bootstrap archives that intentionally omit native companions.

Native artifacts bind to the host binary or Python library, engine settings,
source hash and target. A different host prepares a new local artifact instead
of using incompatible code. The default private cache is `build/cwasm`; set
`WISP_CWASM_CACHE` to choose another disk-backed directory. Compilation uses one
worker and serializes jobs sharing that cache. Publication removes obsolete
native generations from each tool's artifact directory.

For the Wisp worker's fixed execution profile, use `precompile TOOL --worker
--wasmtime /path/to/wisp-worker`. Direct native input requires `run
--allow-precompiled` or `serve --allow-precompiled`. Protocol clients explicitly
opt in with `Client::spawn_with_precompiled` after authenticating artifacts.
Ordinary application inputs continue through Wasm validation. Wasmtime cannot
validate arbitrary machine code, so native artifacts must come from a trusted
compiler or authenticated package.

Precompilation removes compilation from startup. It does not change the selected
optimization level. See [Wasmtime precompilation](https://docs.wasmtime.dev/examples-pre-compiling-wasm.html).

GitHub Actions definitions live in the root [`.github/workflows`](.github/workflows).
See [CI builds](docs/ci.md) for automatic checks, full builds and runner requirements.

## Use installed compilers with Wisp

From Wisp:

```sh
source ../wisp-buildtools/env.sh
```

The environment file selects the C/C++ Component, Go compiler package, Rust
bootstrap root, Wisp-owned execution hosts and native Go module installer using paths
relative to this directory. Rust uses `WISP_RUST` and the separately pinned
`WISP_RUST_WASMTIME`; build it with `python3 build.py build rust`.
It does not install or build tools. Native Go is needed only by examples that
install ordinary Go modules, not by the Go compiler guests.

## Compile application sources

The workspace command selects the installed compiler in each language directory.
Wisp consumes their outputs through its prebuilt Component units. Each language's
README describes bootstrap requirements, supported APIs and the host/guest boundary.
Compiler payloads contain Wasm, source libraries and adapters. The shared Wisp
execution hosts used for verification currently target Linux x86_64.

From the Wisp checkout:

```sh
source ../wisp-buildtools/env.sh
python3 "$WISP_TOOLCHAINS" list
python3 "$WISP_TOOLCHAINS" bootstrap all
python3 "$WISP_TOOLCHAINS" verify all
```

`bootstrap` installs or builds the compiler packages and may need network access.
`build` runs the installed compiler without downloading dependencies. It resolves
source, output and forwarded arguments relative to the caller's current directory.
Output files must be absent. When an adapter emits build receipts or WIT
sidecars, those files remain beside the output.

Source builds run every language compiler, optimizer, binding generator and
linker stage inside Wasm. Native Python handles file I/O and orchestration, and
Wasmtime hosts the guest tools. Native compilers, Node and npm are used only to
bootstrap the compiler packages where a language's README lists them. Rust also
uses native Cargo for locked, offline metadata resolution on every project
build; its compiler, build scripts and procedural macros execute in Wasm. Native
Go remains an optional module installer for Wisp dependency setup. The direct Go
adapter compiles local packages without it.

```sh
python3 "$WISP_TOOLCHAINS" build zig \
  "$WISP_ZIG_TOOLCHAIN/examples/add.zig" -o out/zig.wasm
python3 "$WISP_TOOLCHAINS" build assemblyscript \
  "$WISP_ASSEMBLYSCRIPT_TOOLCHAIN/examples/add.ts" -o out/assemblyscript.wasm
python3 "$WISP_TOOLCHAINS" build nim \
  "$WISP_NIM_TOOLCHAIN/examples/add.nim" -o out/nim.wasm --export add
python3 "$WISP_TOOLCHAINS" build javascript \
  "$WISP_JAVASCRIPT_TOOLCHAIN/examples/add.js" -o out/javascript.wasm --stub-wasi
python3 "$WISP_TOOLCHAINS" build typescript \
  "$WISP_JAVASCRIPT_TOOLCHAIN/examples/add.ts" -o out/typescript.wasm --stub-wasi
python3 "$WISP_TOOLCHAINS" build python \
  "$WISP_PYTHON_TOOLCHAIN/examples/add/main.py" -o out/python.wasm
python3 "$WISP_TOOLCHAINS" build grain \
  "$WISP_GRAIN_TOOLCHAIN/examples/math.gr" --component \
  --wit "$WISP_GRAIN_TOOLCHAIN/examples/math.wit" --world math -o out/grain.wasm
python3 "$WISP_TOOLCHAINS" build swift \
  "$WISP_SWIFT_TOOLCHAIN/examples/add.swift" --component \
  --wit "$WISP_SWIFT_TOOLCHAIN/examples/add.wit" --world math -o out/swift.wasm
python3 "$WISP_TOOLCHAINS" build java \
  "$WISP_JAVA_TOOLCHAIN/examples/Math.java" --main Math --component \
  --wit "$WISP_JAVA_TOOLCHAIN/examples/math.wit" --world math -o out/java.wasm
python3 "$WISP_TOOLCHAINS" build kotlin \
  "$WISP_KOTLIN_TOOLCHAIN/examples/Math.kt" --main MathKt --component \
  --wit "$WISP_KOTLIN_TOOLCHAIN/examples/math.wit" --world math -o out/kotlin.wasm
```

Zig, AssemblyScript and Nim produce Core Wasm. Assemble their scalar library with
the included WIT contract. Nim also needs the WASI reactor adapter, which runs its
initializer before exported calls. After building `wasm-tools`, select its adapter:

```sh
export WISP_PREVIEW1_ADAPTER="$WISP_BUILDTOOLS_ROOT/wasm-tools/dist/wasi_snapshot_preview1.reactor.wasm"
```

```sh
python3 "$WISP_TOOLCHAINS" componentize out/zig.wasm \
  --wit "$WISP_BUILDTOOLS_ROOT/examples/scalar-library/world.wit" \
  --world scalar -o out/zig.component.wasm

# For Nim, pass the matching adapter as well:
python3 "$WISP_TOOLCHAINS" componentize out/nim.wasm \
  --wit "$WISP_BUILDTOOLS_ROOT/examples/scalar-library/world.wit" \
  --world scalar --preview1-adapter "$WISP_PREVIEW1_ADAPTER" \
  -o out/nim.component.wasm
```

JavaScript, TypeScript and Python emit Components directly. Create a Wisp project
from a Component, then build it through Wisp's normal admission path:

```sh
python3 "$WISP_TOOLCHAINS" wisp-project out/zig.component.wasm \
  --name demo:zig --version 0.1.0 -o out/zig-project
wisp build out/zig-project --component-only --out out/zig-release
"$WISP_WASMTIME" run --invoke 'add(20,22)' out/zig-release/component.wasm
# 42
```

`wisp-project` extracts the Component's actual contract, assigns the requested
release identity to the generated wrapper package, preserves imported/exported
interface identities, copies the artifact, and records its SHA-256. Pass `--wit`
and `--world` to supply an authored contract instead; its package name and version
must match the release identity. The staged WIT is parsed before publication.
The helper runs the pinned upstream `wasm-tools` module through Wasmtime.
`WISP_WASM_TOOLS` or `--wasm-tools` can select another WASI Core Wasm build;
native binaries are rejected. WIT parsing, canonical ABI generation, Component
assembly and validation run in that guest. `bootstrap` prepares the shared
package automatically. `wasm-tools` on PATH is never used for source builds.
When extracting a contract, choose a wrapper package identity distinct from its
nested interface packages. Use the original authored `--wit` and `--world` when
the release should share an exported interface's package identity.

Zig and Nim run compiler and linker code inside Wasmtime. AssemblyScript's
frontend and Binaryen backend run as separate Wasm instances through Python.
JS parsing and bytecode compilation run in QuickJS Wasm, and TS transpilation
runs in a WASI port of esbuild. The JS WIT binding generator and Component
linker also execute inside Wasm. Runtime initialization happens in the final
Component instead of using a native Wizer snapshot during source builds.
TypeScript type checking is outside that adapter's contract.
Python's source compiler runs in guest CPython; its runtime template is built
during bootstrap. A small WASI packager creates each Python source-provider
Component. These boundaries are recorded in the language receipts, with
`fullyWasmHostedBuild: true` describing the source build stages.

Grain's upstream OCaml compiler and Binaryen backend are JavaScript builds
running inside QuickJS Wasm. Its adapter emits Core Wasm or, with `--component`
and WIT, a Component using the shared reactor adapter. Raw Wasm scalar exports
are supported; Grain heap values need additional ABI bindings. See
[Grain](grain/README.md) for startup costs and the optional external compiler cache.

Swift's frontend and linker run inside Wasm and load their exact matching
`llvm-swift` backend modules. It supports ordinary Swift sources, generics, ARC
and the core SDK. `--component` exposes scalar exports through an authored WIT
contract. SwiftPM, compiler plugins and macros are outside this profile. See
[Swift](swift/README.md) for compiler and runtime boundaries.

Java and Kotlin compile sources to JVM bytecode inside the shared `jvm` Wasm
guest. The `teavm` package compiles that bytecode to standalone WASI modules in
the same guest. Java uses TeaVM's portable API SDK; Kotlin uses the JVM
package's JDK class data for source compilation. Both use TeaVM's library
implementations when generating applications. Native Java is a bootstrap tool.
Application compilation uses Python, Wisp's Wasmtime host and the installed packages. This profile uses
TeaVM's supported API subset and scalar WIT exports. See [Java](java/README.md)
and [Kotlin](kotlin/README.md) for language and library limits.

Python's initial adapter exposes `add(s32, s32) -> s32`, backed by an annotated
Python callable. `--entry NAME` selects its implementation. Multiple exports,
other signatures and additional package imports need further adapters; see its
README for the included standard-library modules and runtime limits.

Run the workspace integration check after bootstrapping. It compiles each add
example, assembles Components where needed, imports them through Wisp, calls
`add(20,22)` before and after admission, and checks rejection of a changed hash.
It keeps per-language logs and an atomic report that records incomplete runs.

```sh
python3 ../wisp-buildtools/test-workspace.py
python3 ../wisp-buildtools/verify-workspace.py \
  --wasm-only --wisp "$PWD/target/debug/wisp" --out ../wisp-buildtools/out/integration-new
```

`--wasm-only` puts failing guards ahead of native compiler and Node executables
on `PATH`, selects the pinned WASI Component tools, and records every attempted
guard invocation. The run passes only when all selected language builds and
Wisp admission checks succeed with no attempted native-tool calls. Python and
Wasmtime remain the host orchestration and execution programs. This checks
source builds after bootstrap; native bootstrap compilers do not run during
the acceptance check.

## Shared LLVM builds

LLVM sources download to `llvm/.sources/`. The WASI port patches live in
`llvm/patches/`; C/C++ builds use those shared sources and patches.
Build directories, wasi-sdk and native build helpers live in `clang/.toolchains/`.
The source bootstrap SDK is implemented in `llvm/scripts/sdk.py`.
`llvm/scripts/shared.py` turns its verified PIC objects into shared Wasm LLVM
and C/C++ runtime libraries, then links small Clang and Nim consumers with
external core-module imports. The installed `llvm` package contains shared
libraries and their integrity manifest; static bootstrap archives remain local.
Rust 1.99.1 builds a WASIp3 compiler Component using this LLVM 24 SDK.
It links the static backend into its Component and uses Wasmtime 50.

## Package recipe Action

The [package-build Action](.github/actions/package-build/action.yml) provisions
compiler dependencies for `wisp-sh/wisp-pkgs`. It installs pinned bootstrap
prerequisites in dependency order, builds each compiler, and exports its paths
for application recipes. Swift also prepares the streamed compiler shards.
Run `python3 test-ci-package.py` to check dependency and environment propagation.

## License

Wisp-authored code in this repository uses Apache-2.0. Upstream sources, compiler
payloads and packaged applications retain their own licenses and notices.
