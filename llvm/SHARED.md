# Shared LLVM runtime

Clang and Nim link their C++ entry points against one LLVM 24 shared Wasm
library. This is shared-everything dynamic linking: driver, LLVM and C++ runtime
modules use the same linear memory and indirect-function table inside a tool
invocation. Each compiler component imports its libraries from the separately
installed LLVM package. The large modules are absent from the compiler files.

`dist/shared/manifest.json` records the provider's ABI, source inputs and every
module's size and SHA-256. The current profile is `llvm24-wasip3`:

| Import | Installed file | Bytes |
| --- | --- | ---: |
| `wisp-llvm24` | `lib/llvm24.so.wasm` | 88,002,215 |
| `wisp-llvm24-libc` | `lib/libc.so.wasm` | 2,564,760 |
| `wisp-llvm24-libcxx` | `lib/libcxx.so.wasm` | 5,500,557 |
| `wisp-llvm24-libcxxabi` | `lib/libcxxabi.so.wasm` | 1,025,769 |

Existing packages may declare the same complete inventory with `vise-` instead
of `wisp-`. These import names are part of the binary ABI. Both verification
helpers accept either complete naming scheme and preload the names recorded in
the manifest. Mixed naming schemes and duplicate entries are rejected. Older
packages keep their original Wasm files and checksums without rebuilding LLVM.

The total shared-module payload is 97,093,301 bytes. Clang's consumer component
is 3,242,325 bytes; Nim's C backend is 3,247,061 bytes. Their own headers,
frontend, standard library and application sysroots are accounted for in their
packages. These sizes describe the source build and may change with compiler
inputs; the package size report reads the actual inventory.

`lib/llvm24.so.wasm` contains LLVM's WebAssembly backend, the Clang compiler
libraries required by these drivers, and Wasm LLD. These are C++ libraries with
one pinned ABI. The provider is built once; it is not copied into Zig, Clang or
Nim packages. Zig uses the Clang package as its C backend.

Set `WISP_LLVM` to the installed directory containing the manifest. Wisp supplies
a Wasmtime host supporting external core-module imports. The runtime helper
[`shared.py`](shared.py) verifies provider paths, sizes and hashes and returns
the host's `--preload NAME=FILE` arguments. The packaged provider includes no
native host. Stock Wasmtime 50's component CLI rejects preloads; Wisp's host
connects them with `component::Linker::root().module(...)`.

## Bootstrap

The source build first produces the pinned static Wasm SDK. The shared builder
verifies that SDK's complete checksum inventory, LLVM revision and host ABI,
then compiles the two small C++ entry points and links the library:

```sh
python3 llvm/scripts/shared.py build \
  --out llvm/dist/shared \
  --clang-output clang/.toolchains/build-p3/bin/clang-wasm.component \
  --nim-output nim/work/backend-build/c-backend
python3 llvm/scripts/shared.py verify llvm/dist/shared
```

Only bootstrap uses native Clang and linking tools. User compilation runs all
compiler and linker code inside Wasm. The shared library has a `dylink.0`
section. `wasm-tools component link` generates each consumer's shared-memory
linking machinery; `component unbundle` removes the library modules and turns
them into external imports. The builder requires Clang and Nim to produce
byte-identical shared module payloads before publishing either consumer.

The static SDK and its CMake metadata remain available under `artifacts/` for
bootstrap development. They are excluded from the runtime Wisp package.

Rust 1.99.1 builds a WASIp3 Component with LLVM 24 linked into it and uses the
same Wasmtime 50 execution host. It has no separate shared LLVM provider.

The linking convention and external-module mechanism are described in the
[WebAssembly dynamic linking ABI](https://github.com/WebAssembly/tool-conventions/blob/main/DynamicLinking.md),
[component linking design](https://github.com/WebAssembly/component-model/blob/main/design/mvp/Linking.md)
and [Wasmtime module imports](https://docs.wasmtime.dev/api/wasmtime/component/struct.LinkerInstance.html#method.module).
