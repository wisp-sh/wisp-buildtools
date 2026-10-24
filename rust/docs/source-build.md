# Build Rust 1.99.1 for WASIp3

Run the complete build from the repository root:

```sh
python3 build.py build rust --jobs 2
python3 build.py test rust
```

Rust depends on the repository's LLVM 24 SDK. CI prepares/restores that SDK
through the shared LLVM bootstrap job, then builds Rust with `--no-deps`.
Rust verifies its LLVM revision, host target, version and complete SHA-256
inventory. It links the existing static archives instead of building LLVM again.
Rust's upstream LLVM 23 is not a required build input for this port.
Public distributions also need a Clang/LLVM SDK built with the repository's
file-prefix remapping flags. Old static archives can carry local source paths
into the Rust compiler even when rustc's own paths are remapped.

The LLVM 24 compatibility code is in
[the compiler patch](../patches/0001-rust-1.99-wasi-proc-macros.patch). It also
ports the interpreted proc-macro bridge, embedded LLD and serial WASI compiler
execution. The newer upstream macro patch remains an unapplied reference.

`prepare-source.py` verifies the official source and backtrace archives before
extraction. It verifies Rust's original `src/stage0` independently from the
selected bootstrap. The build uses the exact matching official native Rust
1.99.1 release, with its full compiler commit checked before execution. This
allows a single cross-compilation stage without a custom native Rust compiler
or a second LLVM build. The pinned `compiler_description` preserves the exact
native compiler version string in crate metadata, so its standard libraries
remain readable by the cross-compiled compiler.

Individual commands, from `rust/`:

```sh
python3 scripts/prepare-source.py --jobs 2
patch --batch --fuzz=0 -d build/source-rust-58cf8f9c35b3 -p1 \
  < patches/0001-rust-1.99-wasi-proc-macros.patch
python3 -I scripts/build-compiler.py --dry-run \
  --macro-patch patches/0001-rust-1.99-wasi-proc-macros.patch
python3 -I scripts/build-compiler.py --build --jobs 2 \
  --macro-patch patches/0001-rust-1.99-wasi-proc-macros.patch \
  --wasm-opt ../clang/.toolchains/binaryen-version_133/bin/wasm-opt
```

The pinned Binaryen 133 release runs without a shared-library override. Older
Debian Binaryen packages need `--wasm-opt-library-dir` pointing to their
`usr/lib/x86_64-linux-gnu` directory. The root build command detects both layouts.
Optimization uses an explicit Wasmtime 49 feature set. Do not use
`--all-features`: Binaryen 133 can emit compact imports that this host rejects.

Set `WISP_RUST_WASMTIME` to Wasmtime 49 before building. WASI SDK 34 is expected
under `rust/downloads/wasi-sdk-34.0-x86_64-linux`. The build provisions
checksum-verified native Rust archives automatically; `--bootstrap-prefix` can
select an existing installation with the exact matching version and commit.
`--llvm-sdk` overrides the SDK directory without weakening verification.
Linux x86_64, Python 3.12+, native C/C++, CMake, Ninja and patch are required.

The driver runs `x.py build --stage 1 compiler/rustc`, then
`x.py build --stage 0 library`. Both use the matching native release.
The first produces the WASIp3 compiler; the second builds matching host,
WASIp1 application and unknown-unknown macro sysroots. Keeping these commands
separate avoids an unnecessary native stage1 compiler and LLVM build.
The configuration explicitly disables debug assertions as well as debug logging;
upstream's `compiler` profile otherwise enables assertions. On WASI, sysroot
searches reuse the runner's explicit `--sysroot` instead of looking for a driver DLL.
`remap-debuginfo` maps compiler, dependency and sysroot source paths to stable
Rust paths. Cached binaries built without it must be rebuilt before public packaging.

Outputs go to `build/compiler-source-build-1.99.1/`. A dry run resolves the
bootstrap steps but does not establish a successful compiler link. Actual
builds write `bootstrap.log` and `build-report.json`. Reports record source,
patch, SDK inventory, runner, native tool and configuration hashes. The exact
build driver and source lock are snapshotted before compilation. Packaging
rejects changed inputs and requires a successful compiler `--version` probe.

The installed compiler must be a Component within the 256 MiB tool input limit.
Binaryen removes duplicate functions and applies `-O1` to the largest embedded
Core module, then the driver restores
it into the unchanged Component envelope. Other Core modules and the Component
ABI remain intact. This removes redundant compiler code before Wasmtime's
first compilation. The raw Component and optimizer hashes are retained.
The compiler host uses Wasmtime's single-pass register allocator with inlining
disabled; the default allocator is too slow for this compiler module.
The compiler links LLVM into its Component and uses the shared Wasmtime 49 host.

After a successful build, package a fresh candidate:

```sh
python3 -I scripts/build-compiler.py --package \
  --candidate-out dist/compiler
```

If that destination already contains an older compiler, preserve or move it
before packaging. Packaging ships the compiler, matching sysroots, runner,
license notices, exact build report and original provenance snapshots. It
creates `runtime-adapter.json` with pending status and a verification directory
on a clean build. Application acceptance must run before the candidate is
considered usable; packaging alone records no passed macro acceptance.
