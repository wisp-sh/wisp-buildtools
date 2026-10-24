# LLVM hosted by WASI

This directory owns the LLVM source, WASI patches and external shared Wasm
libraries used by [Clang](../clang) and [Nim](../nim). Their runtime components
import the same LLVM, Clang CodeGen, LLD, libc and libc++ modules from this
package. The Wisp package contains these shared modules rather than static SDK
archives or a Wasmtime executable. See [the shared-library boundary](SHARED.md).

The static SDK described below is a bootstrap input for building the shared
libraries. It is not installed as part of the runtime LLVM package. Rust 1.99.1
also uses this static LLVM 24 SDK to build its WASIp3 compiler Component. Its
backend is linked into that Component; the old `llvm-rust` provider is retired.

## Package the existing build

Python 3.11 or newer is required. The LLVM source archive, extracted source tree
and the C/C++ toolchain's configured WASIp3 build must already exist. Packaging does not rebuild
LLVM or run native LLVM code.

```sh
python3 scripts/sdk.py package \
  --out artifacts/llvm-24-d297af3-wasip3-o1 \
  --archive dist/llvm-24-d297af3-wasip3-o1.tar.gz
python3 scripts/sdk.py verify artifacts/llvm-24-d297af3-wasip3-o1
```

The packager consumes [toolchain.toml](toolchain.toml), verifies the upstream
archive checksum and the configured target, and rejects missing library
requirements. `--build-dir` and `--source-dir` can select relocated inputs with
the same configuration. `--without-clang` packages just LLVM and LLD. Existing
output directories and archives are never overwritten.

The SDK contains:

| Path | Contents |
| --- | --- |
| `include/llvm`, `include/llvm-c` | Upstream public headers plus generated configuration, intrinsic and target headers. |
| `include/lld` | Public LLD headers and generated version header. |
| `include/clang` | Public and generated Clang headers, unless disabled. |
| `lib/lib*.a` | Static libraries containing WebAssembly object files. |
| `lib/cmake/WispLLVM` | Relocatable imported targets for the packaged library subset. |
| `bin/llvm-config` | Python metadata utility that runs on the build host. |
| `share/llvm/sdk.json` | Actual target, LLVM version, dependency graph and archive object inventory. |
| `share/llvm/provenance` | Pinned inputs, applied patch inputs, original CMake cache and generated build variables. |
| `licenses` | LLVM, LLD and optional Clang license texts. |
| `SHA256SUMS` | Sorted checksums for every other packaged file. |

Verification hashes every file and checks every archive member is a Wasm object.
It rejects extra files, symlinks, unsafe checksum paths and native libraries. A
checksum manifest records integrity; callers should also pin the SDK archive's
checksum when distributing it. Tar archives normalize timestamps, owner names,
IDs and permissions so identical inputs produce identical archive bytes.

The SDK does not include a C/C++ runtime or sysroot. Use the pinned WASI SDK 34.0
for those. Native table generators such as `llvm-tblgen` remain build-host tools.
`bin/llvm-config` reports metadata about the Wasm libraries and requires Python;
it is not a Wasm compiler executable.

## Link a compiler against the SDK

Configure a consumer with the WASIp3 toolchain and use the SDK's CMake imports:

```cmake
find_package(WispLLVM REQUIRED CONFIG)
add_executable(my-compiler main.cpp)
target_link_libraries(my-compiler PRIVATE LLVMCore LLVMWebAssemblyCodeGen lldWasm)
```

Set `WispLLVM_DIR` to the installed `lib/cmake/WispLLVM` directory. Imports reject
a compiler target other than `wasm32-wasip3`; static Wasm archives cannot be
linked into a native executable. The SDK exports only libraries that were
actually built. It does not copy LLVM's full build-tree CMake exports, which
reference unbuilt libraries, native table generators and absolute source paths.

The [sdk-smoke example](examples/sdk-smoke/main.cpp) creates LLVM IR, emits a
Wasm object and calls LLD in process. The verification script copies the SDK to
a path containing a space, builds that compiler against the relocated SDK,
runs it inside Wasmtime, then invokes its generated `add(20, 22)` function and
requires `42`:

```sh
python3 scripts/verify-sdk-smoke.py \
  --sdk artifacts/llvm-24-d297af3-wasip3-o1 \
  --wasi-sdk ../clang/.toolchains/wasi-sdk-34.0-x86_64-linux \
  --wasmtime ../clang/.toolchains/wasmtime-v49.0.1-x86_64-linux/wasmtime \
  --out artifacts/sdk-smoke
python3 -m unittest discover -s tests -v
```

This builds one consumer source file and links the packaged libraries. It does
not rebuild LLVM. Its result and configure/compiler logs are retained under the
requested output directory.

## Source and patch ownership

[toolchain.toml](toolchain.toml) pins LLVM revision
`d297af3b3d7a086d64f1477cfa682a16fcfa3260`, WASI SDK and host tool checksums.
The downloaded source archive and extracted tree live in `.sources`, outside
Git. The C/C++ build links to those shared sources under `clang/.toolchains`.
The extracted source tree already includes the port and the C/C++ toolchain's
current C++ entry point.

The existing combined port is now also available as two patches:

- [0001-llvm-wasi.patch](patches/0001-llvm-wasi.patch) contains LLVM's WASI
  filesystem, memory, process, randomness, signals and CMake adaptations.
- [0002-clang-in-process.patch](patches/0002-clang-in-process.patch) contains the
  original Clang entry point and driver changes.

The original
[0001-clang-wasip3-in-process.patch](patches/0001-clang-wasip3-in-process.patch)
remains unchanged for existing C/C++ builds. Apply either the combined patch or
the two split patches, never both. The `clang` directory owns its
[driverless compiler patch](../clang/patches/0002-driverless-clang-wasm.patch).
Its [build script](../clang/build-clang-wasi.sh) still configures and builds the
libraries. This directory packages those products; shared configure/build
scripts can replace that driver when another compiler's LLVM revision is fixed.

## ABI and Rust constraints

The current SDK is LLVM `24.0.0git`, hosted by `wasm32-wasip3`, with only the
WebAssembly backend. It uses static linking and disables threads, RTTI,
exceptions and assertions. The `-lrt -ldl` requirements refer to compatibility
archives supplied by the pinned WASI sysroot.

One inherited build setting needs care. Existing generated LLVM headers record
`LLVM_HOST_TRIPLE` as `x86_64-unknown-linux-gnu`, even though the actual object
files target Wasm. The SDK preserves those headers and records that value as
`configured_llvm_host_triple`. Its separate `host_target` field and
`llvm-config --host-target` describe the archive ABI. The relocated smoke test
uses an explicit WebAssembly application target and verifies that path. A fresh
shared build should set LLVM's host triple correctly before relying on
host-dependent LLVM behavior.

The [Rust source profile](../rust/toolchain.json) selects Rust 1.99.1 and this
LLVM 24/WASIp3 SDK. Its patch adapts the LLVM wrapper API and supplies the WASI
compiler host implementation. Rust verifies the SDK's revision and file
inventory before linking its static libraries. The LLVM/LLD component probe and
all six wrapper files have been checked against the existing SDK.

Historical SDK metadata predates this port and can still label Rust as
`not-verified`. Rust's own compiler build and application acceptance receipts
record the new port's validation. Static SDK reuse does not make Rust a dynamic
consumer of Clang's runtime LLVM modules.
