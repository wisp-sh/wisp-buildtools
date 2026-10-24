# Compiler packages and binary sizes

The tools use Wisp bundles named `<tool>.wisp.zip`. JavaScript and TypeScript
share `javascript`. Package existing artifacts and generate measured reports:

```sh
source env.sh
python3 build.py package all --output-dir dist/packages
python3 build.py sizes all --package-dir dist/packages
```

`dist/packages/sizes.html` lets you select tools, inspect their requirements and
compare download and installed sizes. Selecting a compiler includes its required
packages once. `sizes.md` has the table; `sizes.json` contains exact byte counts,
complete inventories and dependency totals. Configured Wisp execution hosts are
measured separately, outside compiler totals.

## Package boundaries

Each package contains its own compiler guests, libraries, adapters and notices.
Dependencies are separate packages, declared as external `wisp:<tool>` shared-tool
requirements with an exact package version:

| Consumer | Required package |
| --- | --- |
| Clang | LLVM |
| Nim | LLVM and Clang resource headers |
| Zig | Clang, which requires LLVM |
| Rust | LLVM 24 linked into its WASIp3 compiler Component |
| Swift | llvm-swift, the backend profile bound to its compiler and linker images; wasm-tools for Components |
| JavaScript / TypeScript | wasm-tools |
| Python | wasm-tools |
| Grain | wasm-tools |
| Java, Kotlin | jvm and teavm |
| TeaVM | jvm and wasm-tools |

LLVM supplies real shared core modules: LLVM/Clang/LLD, libc, libc++ and libc++abi.
Clang and Nim import these modules through the Component Model and share memory
within each invocation. Their packages do not embed LLVM code. Zig uses the
installed Clang Component. Nim and Zig use Clang's shared resource headers. The static LLVM archives and
native WASI SDK remain bootstrap inputs outside runtime packages.

Rust 1.99.1 reuses the static LLVM 24 SDK when building its WASIp3 compiler
Component. Its runtime package contains the linked backend. It does not depend
on the retired `llvm-rust` split provider. The shared SDK is a build dependency.

Swift's `llvm-swift` provider contains backend code split from its published WASI
compiler and linker images. Its private table ABI is bound to those exact images.
It includes LLVM, Clang, LLD and their support functions. It uses a separate
profile from the LLVM 24 SDK. The Swift entry points load their primary
module and matching backend before invoking the compiler or linker.

Java and Kotlin use the shared `jvm` interpreter and JDK data package. Their
frontend JARs execute inside that Wasm guest. The `teavm` package contains the
shared bytecode compiler and portable Java API SDK. It runs inside the same guest
to produce standalone WASI applications. JDK module images and compiler JARs are
support bytes in format accounting; their inventory roles identify library and
compiler contents. Native Java is used only while bootstrapping these packages.

Wisp owns Wasmtime executables and Python bindings. Compiler bundles contain no
Wasmtime engine. Select the hosts with `WISP_WASMTIME`, `WISP_RUST_WASMTIME` and
`WISP_WASMTIME_PYTHON`. `wisp toolchain hosts` reports configured engines,
capabilities and measured sizes. See Wisp's `tools/shared-hosts` for its pinned
host build recipe. The buildtools CI copy records the Wisp source hashes.

## Container and measurements

Packaging writes `TOOL.wisp.zip`, `TOOL.wisp.json` and `TOOL.wisp.zip.sha256`.
The ZIP contains `release.json` and `toolchain.tar.gz`, using Wisp's release
container and integrity conventions with `schema: 4`, `kind: "toolchain"` and
identity `wisp:TOOL`. The package version is `0.1.0`; the upstream compiler
version is recorded separately.

Each file has a SHA-256, size, mode, format and role. Entrypoints, execution hosts,
package dependencies and bootstrap requirements are recorded separately.
`package-profiles.json` defines the runtime allowlists. Sources used only to build
compilers, static SDK archives, build trees and caches stay outside the bundles.
Portable payloads use platform `wasm32-wasi`; admission rejects native binaries
under that platform.

Evidence directories, local benchmarks, logs, timing dumps and macro traces are
excluded even when a containing runtime directory is included. Rust bundles
retain compiler and sysroot inventories, source locks, the build-driver snapshot
and the compact acceptance summary. The exact build-report hash stays in the
toolchain metadata; verbose build reports and their local paths stay outside the
release bundle. Python bootstrap metadata retains tool names and checksums,
with local executable locations reduced to basenames. Packaging stages those
metadata changes without modifying the installed compiler files.

The packer also rejects payload bytes containing the build machine's checkout
or home path. This includes compiler binaries and sysroot metadata. Rebuild
older artifacts with path remapping before distributing them; changing their
bytes after compilation would break their integrity inventories. Size reports
use bundle filenames and paths relative to each measured host directory.

| Measurement | Included |
| --- | --- |
| Download | Complete `.wisp.zip`, including manifest and ZIP framing. |
| Installed payload | Sum of regular-file bytes in the package inventory. |
| Wasm | Core modules, Components and Wasm archives. |
| Native | Native executables or shared libraries in the package; zero for these toolchains. |
| Support | Other file formats: JavaScript and JVM compiler code, JDK data, adapters, headers, source libraries, metadata, documentation and notices. |
| Complete installation | Consumer plus the transitive package dependencies, counted once. |
| Unique content | Identical files counted once by SHA-256 across the selection. |

MiB means 1,048,576 bytes. Payload totals exclude generated receipts under
`share/wisp/toolchains`, filesystem allocation, execution caches, build outputs,
temporary disk space and RAM. External host measurements cover only the listed
executables or Python binding files, excluding system libraries. They are not a
measurement of the complete machine footprint.

Support is a file-format category. Grain's JavaScript compiler is included there
because it is text executed by the QuickJS Wasm guest; its inventory role is
`compiler`. The role and format fields let you distinguish compiler code from
headers, library sources and documentation within that total.

## Hosts after installation

Python 3.12+ provides file I/O and orchestration. Source compilation runs in Wasm.
The tested execution hosts currently run on Linux x86_64.

| Tool | Wisp execution host |
| --- | --- |
| Clang, LLVM, Nim, Zig | Wisp Wasmtime 49.0.1 with WASI Preview 3 and external core-module imports. |
| Rust | Wasmtime 49 with WASIp3 component support. |
| AssemblyScript | External Wasmtime Python bindings 49.0.0 for compiler callbacks. |
| Go, JavaScript, Python, wasm-tools | External Wasmtime 49.0.1. |
| Grain | External Wasmtime 49.0.1; QuickJS, the Grain compiler and its backend execute inside Wasm. |
| Swift | External Wasmtime Python bindings 49.0.0 for compiler/backend linking; the shared wasm-tools package uses the CLI host for Components. |
| Java, Kotlin, TeaVM, JVM | External Wasmtime 49.0.1 with WASI Preview 1, Wasm tail calls and exceptions. |

Rust Cargo projects also use native `cargo metadata --locked --offline`.
Dependencies must already be vendored. Rust compilation, build scripts and
procedural macros run in Wasm. Native Go is optional for Wisp's module installation
step; the direct Go adapter neither fetches modules nor invokes a native compiler.
Node, npm, native compiler toolchains, componentize-py, Binaryen and WAC are
bootstrap requirements where listed, outside installed source compilation.
Python's snapshot bootstrap checks the official Wasmtime executable hash;
`WISP_BOOTSTRAP_WASMTIME` selects that Wisp-owned host for the root build command.
Normal source builds use `WISP_WASMTIME`.

## Validate and install

```sh
python3 build.py validate dist/packages/javascript.wisp.zip
wisp toolchain validate dist/packages/javascript.wisp.zip
python3 build.py install dist/packages/javascript.wisp.zip \
  --prefix /tmp/wisp-javascript
```

Keep required bundles alongside the selected bundle, for example
`wasm-tools.wisp.zip` beside `javascript.wisp.zip`. Installation resolves that
local dependency graph, checks exact identities and versions, verifies every
payload, and writes one installation atomically into a new prefix. Pass several bundle paths to install multiple tools into the same prefix. Shared
providers are installed once, including when several dependency paths reach them.
Missing, cyclic, conflicting or damaged providers prevent publication.

Installation preserves relative tool directories so adapters can find their
shared packages. It performs no network access and executes no entrypoint.
Supply Wisp's execution hosts when invoking an installed compiler. Validation
checks container integrity; trusted distribution still requires pinning the
bundle SHA-256. Language-specific API limits remain those of each adapter.
