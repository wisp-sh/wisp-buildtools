# GitHub Actions builds

All workflows live in the root `.github/workflows` directory. They use the
same `build.py` commands as local builds and need only this repository.
No job checks out Wisp, downloads private compiler releases, or depends on a
developer's existing build directories.

## Automatic checks

[`ci.yml`](../.github/workflows/ci.yml) runs on pushes and pull requests. It
checks the build definitions for all tools and library profiles, runs the build command,
toolchain-bundle validation and workspace helper tests, and validates the
workflows with checksum-pinned actionlint. Build plans stay in the runner workspace.

After the checks pass, [`tools/ci-select.py`](../tools/ci-select.py) selects the
affected targets from every tool and library profile in
[`buildtools.json`](../buildtools.json). C/C++, Rust, Zig, Nim and all shared
providers are eligible alongside the other compilers. Selected targets build,
test and package on clean Ubuntu 24.04 runners.

The selector uses the changed paths and manifest entries:

- A change under a tool's directory, in its declared `sourceInputs`, or in a
  packaged input selects that target. Its transitive consumers through both
  `dependencies` and `runtimeDependencies` are selected too.
- A change to one tool entry in `buildtools.json`, such as a compiler pin,
  selects that tool and its consumers. It does not select unrelated tools.
- Shared build, packaging, workflow and bootstrap changes select all targets.
  Changes to common manifest settings also select all targets.
- Documentation changes outside tool directories and packaged inputs run the
  definition checks without compiler jobs. A packaged README or documentation
  inside a tool directory still selects that tool.

The first push, or a run whose comparison revision is unavailable, selects all
targets conservatively. The manual workflow remains available to rebuild every
target regardless of which files changed.

Pushes compare the previous and new revision across the entire push. Pull
requests compare the tested merge commit with the PR base. The job summary
lists selected targets. The runner writes `build-selection.json` with changed
paths and selection reasons. To inspect a
comparison locally:

```sh
python3 tools/ci-select.py --base HEAD~1 --head HEAD
```

Automatic runs do not cancel or replace earlier runs. A later documentation-only
push must not cancel an earlier compiler build whose changed inputs would no
longer appear in the new push's diff. Rebuild selected targets with the manual
workflow when an earlier automatic run failed or was manually canceled.

Each target has a six-hour limit. A failed compiler build fails its job. LLVM
and source-built Rust can need more memory or disk than a standard GitHub-hosted
runner provides; use the manual workflow's runner override when a larger
machine is needed.

## Full compiler builds

Run **Build all toolchains** from GitHub's Actions tab. Choose
`all` to build all tools and library profiles regardless of change selection, or
select one tool. Dependencies build first.
Each matrix job calls the reusable root
[`toolchain.yml`](../.github/workflows/toolchain.yml) workflow:

```sh
python3 build.py build TOOL --jobs 2
python3 build.py test TOOL
python3 build.py package TOOL --output-dir PACKAGE_DIRECTORY
python3 build.py validate PACKAGE_DIRECTORY/TOOL.wisp.zip
python3 build.py sizes TOOL --package-dir PACKAGE_DIRECTORY
```

Clang, LLVM, Zig and Nim share the bootstrap stage described below. Clang uses
the compiler built by that stage; the other three build their selected target
with `--no-deps` after restoring its verified dependencies. Their normal tests,
packaging and bundle validation still run.

Rust waits for the same LLVM bootstrap and reuses its LLVM 24 SDK. Its job
builds and tests Rust 1.99.1/WASIp3 and uploads one `rust-linux-x86_64` bundle.

Successful jobs upload the `.wisp.zip` compiler bundle, its `.wisp.json`
inventory and `.sha256` file, plus `sizes.html`, `sizes.md` and `sizes.json`.
The Markdown size and requirement report also appears in the Actions job
summary. These commands validate without checking out or executing Wisp.
The sibling Wisp CLI can independently validate the same bundles with
`wisp toolchain validate BUNDLE`; see [package details](packages.md).

Public artifact uploads contain the compiler bundles and size reports. Build
plans, raw test output, configurations and bootstrap logs stay in the runner
workspace. Keep any development evidence that needs longer retention in the
shared private evidence repository. Uploading workflow artifacts does not publish
a GitHub release. The reported download size measures the Wisp bundle itself, before
GitHub wraps the uploaded files in its artifact download.

### Publishing a release

Give **Build all toolchains** a `release_tag`, such as `toolchains-2026.10.26`,
to publish its bundles. After every selected target builds, tests and validates,
a final job collects the uploaded bundles and runs
[`tools/release-manifest.py`](../tools/release-manifest.py). It writes
`toolchains-linux-x86_64.json` listing each bundle's tool, package version,
platform, shared-tool requirements, release URL, size and SHA-256, and refuses a
bundle whose bytes differ from its recorded digest or whose shared dependency is
missing from the release. The job then creates the GitHub release with the
manifest, every `.wisp.zip` bundle, its inventory and its digest file. Nothing
is published when any target fails.

Wisp's approved library build pins this manifest by SHA-256 and installs each
bundle with `wisp toolchain install BUNDLE --sha256 SHA256`. A new release
changes the manifest digest, so consumers adopt it explicitly.

The default runner is `ubuntu-24.04`. Automatic builds have no explicit matrix
concurrency cap; available GitHub runner capacity determines how many targets
run at once. The manual workflow allows up to two targets in each of its LLVM
and other-toolchain matrices, plus one Rust job that also waits for LLVM. All default to two
native bootstrap compile jobs within each target to limit memory use.

A full Rust or LLVM build can exceed the memory, disk or six-hour job allowance
of a standard GitHub-hosted runner and fail its job. To use a larger self-hosted
Linux x86_64 machine, run the manual workflow and give its runner input as a
JSON label array:

```json
["self-hosted", "linux", "x64", "buildtools"]
```

The selected runner needs a current GitHub Actions runner and `sudo` access to
Ubuntu's package manager. JS/TS and Python compiler bootstrap need `rustup` on
`PATH`; Rust acceptance also needs native Cargo. The manual workflow's `jobs`
input selects one, two, four or eight native compile jobs per target; it does
not change matrix concurrency. All target jobs retain the same six-hour
timeout. Targets outside the shared LLVM and Rust groups still build their
dependencies in each isolated matrix job.

## Shared LLVM bootstrap

[`llvm-bootstrap.yml`](../.github/workflows/llvm-bootstrap.yml) builds Clang and
the LLVM 24 provider once when any Clang, LLVM, Zig or Nim job is selected.
Other compiler families can start independently. The selected LLVM consumers
restore the resulting `llvm-bootstrap-state` artifact instead of rebuilding
Clang separately in every job.

[`tools/ci-llvm.py`](../tools/ci-llvm.py) exports the state to
`.ci/llvm-bootstrap.tar.gz`. It includes the Clang Component and resource
headers, shared LLVM modules, verified LLVM static SDK, Nim C backend, native
WASI SDK and wasm-tools, the patched execution host, licenses and build
receipts. It excludes the large LLVM source and object trees. Compiler
`.wisp.zip` packages alone do not contain this bootstrap state.

The workflow caches this archive under an exact input key covering the recipe,
source inputs, patches, SDK and bootstrap pins, workflow configuration and Linux
runner ABI. Restore verifies the current source fingerprint, Clang build
receipt, SDK inventory and every payload file before replacing outputs. Archive
paths and links must remain inside the allowed state directories. A rejected
cache falls back to a source build. Source builds can also reuse checked
compiler objects through ccache; native execution hosts have a separate cache
and must pass their receipt and capability checks before reuse.

The automatic workflow accepts repository variables `LLVM_RUNNER_JSON` and
`LLVM_BUILD_JOBS` for the shared LLVM group. Defaults remain `ubuntu-24.04` and
two native compile jobs. The manual workflow uses its runner and jobs inputs.
To inspect or verify state locally:

```sh
python3 tools/ci-llvm.py key
python3 tools/ci-llvm.py verify
```

## Rust build

Rust 1.99.1 uses the shared LLVM 24/WASIp3 SDK. Both automatic and manual
workflows schedule the LLVM bootstrap when Rust is selected, restore its
verified SDK and run:

```sh
python3 build.py build rust --no-deps --jobs 2
python3 build.py test rust
python3 build.py package rust --output-dir PACKAGE_DIRECTORY
python3 build.py validate PACKAGE_DIRECTORY/rust.wisp.zip
```

Rust uses the exact matching official native release to cross-compile its
WASIp3 compiler and matching sysroots. It builds no native LLVM, separate LLVM
23, or custom native Rust compiler. LLVM's existing bootstrap cache handles
SDK reuse. The previous `llvm-rust` split target and Rust-specific LLVM object
cache are retired from the active workflow.

The Rust job checks for 15 GiB free and removes unused preinstalled SDKs only
on GitHub-hosted runners. `RUST_RUNNER_JSON` and `RUST_BUILD_JOBS` select a larger
runner and compile job count for automatic builds. Manual builds use their
runner and jobs inputs.

Bootstrap failures print the last 100 lines of
`rust/build/compiler-source-build-1.99.1/bootstrap.log` in the Actions console.
The full log, report, configuration, SDK probe and preparation receipts stay
in the runner workspace.

## Bootstrap tools

[`tools/ci-prerequisites.py`](../tools/ci-prerequisites.py) installs the
native tools needed to build the Wasm compiler packages. Their archives and
the exact Python bootstrap executables are pinned in
[`tools/ci-tools.lock.json`](../tools/ci-tools.lock.json). Rustup installs the
explicit Rust 1.99.1 toolchain and its `wasm32-wasip1` target for JS/TS and
Python bootstrap, and Wisp's shared execution host for the LLVM consumers.
The exact Wisp-owned recipe is copied under `tools/shared-hosts` with an origin
hash inventory, so CI can build it without a sibling checkout. Its Component
linker supplies shared LLVM core modules through `--preload`. Rust reuses its
LLVM 24 SDK with WASI SDK 34 and the matching official Rust bootstrap.
Both workflow host caches derive their version from `tools/shared-hosts/wasmtime.lock.json`.
All targets execute compilers with Wasmtime 50. Python's reproducible
snapshot bootstrap also downloads stock Wasmtime 50.0.0, whose exact binary hash is
pinned in `python/toolchain.lock.json`.

Go and JavaScript's esbuild bootstrap use Go 1.27.2.
AssemblyScript bootstrap uses Node 24.20.0. Normal application compilation
continues to run compiler code in Wasm, with Python and Wasmtime as hosts. Rust
additionally uses native Cargo for offline project metadata on every build;
its build scripts and procedural macros run in Wasm. Package inventories list
external host requirements separately from these bootstrap tools. Runtime hosts
remain in CI's host directory and are excluded from compiler packages. Rust
uses the shared Wasmtime 50 host with WASIp3 enabled. AssemblyScript and Swift
use the pinned Wasmtime Python binding. Host build receipts are included in
`.ci/prerequisites.json` in the runner workspace.
GitHub Actions' checkout, setup and artifact actions are part of the CI
runner, rather than dependencies of the installed compiler packages.

Grain bootstrap extracts the pinned compiler JavaScript and standard library
from Grain's official release without executing its native binary. It builds a
QuickJS Wasm guest using WASI SDK 34. The bootstrap also needs `libbrotlidec.so.1`;
normal Grain source builds need Python, Wisp's Wasmtime host and the shared
wasm-tools guest. They do not use Node or a native Grain compiler.

Swift bootstrap verifies published WASI compiler, linker and SDK artifacts, then
uses the pinned Binaryen 133 tools to split their backend code into the separate
`llvm-swift` package. It does not build the native Swift or LLVM source trees.
Swift application compilation and linking run through Wisp's Wasmtime host.

Java and Kotlin jobs build the shared JVM from pinned Bovine sources with WASI
SDK 34. Bootstrap uses native JDK 21 to compile small launcher classes, generate
the portable API SDK and extract the pinned JDK 23 module image as data. It
never executes the downloaded JRE's native binaries. Compiler dependencies and
licenses use checksum-pinned Maven downloads. Application builds and compiler
verification run the frontends and TeaVM backend inside the shared Wasm JVM.
TeaVM 0.16 emits C, which a small Wasm-hosted Clang/LLD driver compiles to WASI.
These jobs restore the shared LLVM bootstrap and build the remaining dependencies
in manifest order. They do not rebuild LLVM in each Java or Kotlin job.
The compiler packages contain no native JVM or execution engine.

To inspect CI prerequisites without installing them:

```sh
python3 tools/ci-prerequisites.py plan javascript
python3 tools/ci-prerequisites.py check
```

GitHub Actions syntax and limits follow the official
[workflow reference](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).
Actions are pinned to commit SHAs. Workflow lint uses the upstream
[actionlint release](https://github.com/rhysd/actionlint/releases/tag/v1.7.12).

### Publishing a library build runtime

The compiler release and CLI release are separate inputs. The CLI repository's
**Release Linux CLI kit** workflow publishes `wisp-linux-x86_64.json` and its
archive with matching CLI and worker binaries, prebuilt execution hosts, the
Preview 1 adapter, native Rust, Node and Go, locked npm tooling, the upstream
binding generator, and Cargo dependency caches. Its tool versions come from the
CLI repository. The kit includes an authored C library used by runtime release
verification.

Run **Build all toolchains** with a new `release_tag`. The runtime requires at
least `clang`, `go`, `rust`, `llvm` and `wasm-tools` in that release. Other
compilers can be present; assembly selects these five and their shared-tool
dependencies. The acquisition manifest uses schema 2 and records the release
commit. The CLI's source acceptance installer also accepts this release format.

Then dispatch **Release library build runtime** with a new tag and the public
manifest URL and exact SHA-256 from each release. `tools/assemble_runtime.py`
authenticates both manifests, downloads and checks their assets, installs each
Wasm compiler through the CLI, and runs `toolchain doctor`. It never compiles a
compiler. `tools/package_runtime.py` creates the combined archive and
`runtime-linux-x86_64.json`. Each asset must fit GitHub's 2 GiB release asset
limit; expanded runtime contents are limited to 24 GiB. Use a disposable builder
with at least 30 GiB free disk and one build worker for the native CLI kit.

Before publishing, the workflow extracts the archive at a different prefix,
checks compiler inventories and execution hosts, and builds and validates the
shipped C library's generated packages. A failure stops publication. Tags must
be new; neither release workflow replaces existing release assets.

To adopt a runtime, copy its public manifest URL and exact SHA-256 into
`wisp-server/scripts/ci/runtime.lock.json`, then commit it in the approved action.
Update the reusable workflow's action references and the server's approved
workflow SHA for that commit. Consumers never select runtime inputs through
workflow inputs or server environment variables. Until the first runtime is
published, that pin remains explicitly unset and builds fail with an activation
message.
