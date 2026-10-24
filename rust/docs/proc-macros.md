# Procedural macros in the Wasm-hosted compiler

This document records the Rust 1.83 implementation and its 2026-10-18 results.
The active recipe now targets Rust 1.99.1/WASIp3 with LLVM 24; see
[source-build.md](source-build.md). The new port uses `DynClient` and its own
compiler patch. Historical acceptance below does not verify the new release.


The source-built Rust 1.83 compiler executes ordinary procedural macros in
Wasmi 0.40.0 inside its own Wasm process. On 2026-10-18 it passed the
[six-case acceptance suite](../build/acceptance-macro-candidate/results.json),
including unmodified Serde 1.0.228 derives and dependency build scripts.
Application authors use normal Cargo dependencies and
`#[derive(serde::Serialize, serde::Deserialize)]` declarations.

The [port and evidence](macro-port.md) record the implementation, source build
and interpreter traces. This compiler is packaged at `dist/compiler` with
status `verified-local-source`; its [verification record](../dist/compiler/verification.json)
collects the completed gates. The downloaded legacy bootstrap still has no
procedural macro loader. Application compilation, dependencies, macros and
build scripts run inside Wasm; native Cargo resolves metadata only.

## Bootstrap boundary

The initial artifact is oligamiq's
[`v0.2.1-release`](https://github.com/oligamiq/rust_wasm/releases/tag/v0.2.1-release).
Read-only inspection of `rustc_opt.wasm` reports Rust 1.83.0-dev and imports
from `wasi_snapshot_preview1`, `wasi.thread-spawn`, and shared `env.memory`.
The selected source in the release workflow is
[`cf327c2068549194a29160499c2ecafa9061e46e`](https://github.com/oligamiq/rust/commit/cf327c2068549194a29160499c2ecafa9061e46e),
with YoWASP LLVM
[`69bd8547e9cc1268ca41339c98b2b17d13d69be3`](https://github.com/YoWASP/llvm-project/commit/69bd8547e9cc1268ca41339c98b2b17d13d69be3)
(19.0.0git). Its LLD is linked into the compiler.

This compiler needs a legacy Preview1 threads runtime. Wasmtime 49's CLI
rejects the old `-Sthreads` and `-Spreview2` options. Bootstrap work uses a
separately pinned Wasmtime 38.0.4; it must not replace Wisp's Wasmtime 49
runtime for existing compiler Components. Running the compiler through
Wasmtime 38 does not make native compilation part of application builds.

The bootstrap compiler's
[`load_symbol_from_dylib`](https://github.com/oligamiq/rust/blob/cf327c2068549194a29160499c2ecafa9061e46e/compiler/rustc_metadata/src/creader.rs#L1241)
returns `dlopen not supported on this platform` outside Unix and Windows.
Merely compiling a proc-macro crate to Wasm cannot make it loadable.

## Implemented flow: rustc RPC and an interpreter

The backport uses the serialized-bridge approach from bjorn3's upstream draft
[PR 157709](https://github.com/rust-lang/rust/pull/157709), head
`2aa6abf53dc8c597d5f9c6e5f195c4080b5a61bf`, branch
`proc_macro_refactors5`. That reference targets a newer compiler and is kept
unapplied. The [adapted Rust 1.83 patch](../patches/0001-rust-1.83-wasm-proc-macro-bridge.patch)
preserves the older typed `Client<I, O>` API and adds compiler-owned dynamic
clients. Its first macro target is `wasm32-unknown-unknown`.

The compiled and tested flow is:

```text
vendored proc-macro source and dependencies
  -> Wasm-hosted rustc
  -> macro.wasm

application source
  -> Wasm-hosted rustc
     -> Wasmi interprets macro.wasm inside the compiler
     -> byte-buffer RPC calls back into rustc's proc_macro server
     -> rustc continues with the returned token stream
  -> application.wasm
```

The existing bridge passes serialized messages containing token and span
handles. Keeping that bridge preserves hygiene and compiler diagnostics.
Stringifying the input, expanding it outside rustc, and parsing the output
again would lose information and fail important macro cases.

The backport implements these changes:

1. Allows the `proc-macro` crate type for the macro target and retains its
   declarations and crate metadata.
2. Exports scalar functions for buffer allocation, replacement, pointer,
   length, declaration inspection and client calls from the matching
   `proc_macro` library.
3. Loads the module through the compiler's metadata loader and forwards its
   `__rustc_proc_macro_dispatch` import to the existing RPC dispatcher.
4. Looks up macro dependencies using their macro target, including transitive
   re-exports. Macro modules use the target's `.wasm` suffix and empty library
   prefix; regular dependencies remain `.rlib` files.
5. Adapts Rust 1.83's typed bridge and encodes lengths and offsets as checked
   `u64` values, so 64-bit native development hosts and 32-bit macro guests
   share the same wire protocol.
6. Builds the compiler and all three target sysroots together. Their
   `proc_macro` libraries must share this exact bridge revision.

The backport uses [Wasmi 0.40.0](https://github.com/wasmi-labs/wasmi/tree/v0.40.0),
whose Rust 1.80 minimum fits the pinned Rust 1.82 beta bootstrap. Its locked
dependencies compiled successfully for native and Wasm compiler hosts. The
optional text-format parser is disabled; macro modules are binary Wasm.

The runner builds macro crates with `--crate-type proc-macro`,
`--target wasm32-unknown-unknown`, `-Zwasm-proc-macros` and `--extern proc_macro`.
The application uses `wasm32-wasip1` and the same unstable macro flag, with
ordinary `--extern` imports for the generated modules. These flags and target
choices are runner details; the application keeps ordinary Rust source.

The local acceptance crate exercises a function macro, an attribute macro and
a derive with a helper attribute. The Serde case performs JSON round trips
with generic types, borrowed lifetimes and a renamed field. It records six
completed interpreter invocations and executes six dependency build scripts;
the resulting Wasm application prints `42`. See [the detailed results](../tests/README.md#results-2026-10-18).

Macro guests have no direct filesystem, environment or process APIs. The
unknown-unknown target supplies only the compiler RPC import. Supporting
macros that use those APIs requires more runtime integration. The separate
[Wisp negative case](../../../wisp/out/rust-serde-candidate/result.json) rejects a
Serde derive on a union with the expected Rust diagnostic and no application
artifact. The compiler then aborts through its fatal-error path with exit `134`
and an `unreachable` Wasmtime backtrace. Diagnostic rejection is correct, but
the failure output is noisy; these cases do not establish compatibility with
every macro crate or diagnostic.

## Alternatives

**Watt:** [dtolnay/watt](https://github.com/dtolnay/watt) runs a
Wasm build of `serde_derive`. It transforms entry points to `proc_macro2`,
patches that dependency, and uses a small ordinary proc-macro shim to enter
its interpreter. That shim still needs the compiler's existing dylib loader,
so Watt alone does not solve the bootstrap compiler's limitation. Adapting
its interpreter into rustc is possible, but the Wasmi draft preserves the
normal proc-macro source and bridge more directly.

**Static linking:** compiling real macro implementations into a rebuilt
compiler can support a fixed catalog. It requires matching compiler and
macro metadata and rebuilding or relinking for new macro versions. This
would be a limited catalog feature, not general Cargo proc-macro support.
It should not be presented as the final implementation.

**Dynamic linking in shared Wasm memory:** loading another module into the
compiler's memory and function table would require relocation, table and
memory allocation, symbol resolution, TLS, and lifetime handling. Standard
WASI does not provide `dlopen`. This adds a loader subsystem that the
interpreter/RPC approach avoids.

**Host callback:** the compiler could export its serialized RPC dispatcher
and ask the host runtime to instantiate the macro in a separate Wasm
instance. This keeps compilation inside Wasm but needs a custom embedding
rather than the current stock Wasmtime CLI. It is a later performance
option once the protocol works in an interpreter.

Upstream [PR 157590](https://github.com/rust-lang/rust/pull/157590), head
`faf962cc5767f6656f3c19b8373ef850cf17cc24`, instead uses Wasmtime and the
Component Model with a duplicated WIT ABI. It is a useful reference for a
hosted runtime, but is a larger dependency for a Wasm-hosted compiler.
The merged `-Zwasm-proc-macros` flag and crate lookup plumbing are partial
work; they do not imply complete macro execution support in stock rustc.

## Build and release evidence

The full LLVM 19/compiler source build completed. Its
[build receipt](../build/compiler-source-build/build-report.json) and the
candidate's [provenance file](../dist/compiler/source-build.json) identify
the compiler, runner, source pins and original build inputs. Build receipts
preserve their packaging-time acceptance flags. The later
[acceptance results](../build/acceptance-macro-candidate/results.json) establish
the six tested compiler cases. The separate
[Wisp Serde result](../../../wisp/out/rust-serde-candidate/result.json) proves
Component admission and execution, and the
[registry result](../../../wisp/out/rust-interop-macro-verification/result.json)
records all four Go/C++ and Rust import directions passing.
The [activation smoke test](../../../wisp/out/rust-active-smoke/activation-result.json)
also confirms Wisp selects the verified profile through the shared environment
and produces a Component whose `add(20,22)` returns `42`.

The existing C/C++ LLVM libraries use a different source revision and a
WASIp3 host ABI. Share source acquisition, patch management, manifests,
verification, and build tooling first. Sharing the compiled libraries
requires a deliberately compatible LLVM revision, WASI ABI, runtime and
thread configuration.

Basic `build.rs` execution is separate: compile the script and its build
dependencies to a runnable WASI command, run it under Wasmtime, capture its
Cargo directives, and pass the resulting cfg values, environment and output
files to later compiler steps. The Serde case successfully executes the
`serde_core`, `proc-macro2`, `quote`, `serde`, `zmij` and `serde_json` scripts.
Their optional compiler/process probes use conservative defaults when spawning
is unavailable. General subprocess execution and native C build dependencies
remain outside the supported subset.
