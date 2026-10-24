# Rust 1.83 Wasm macro port

This document records the Rust 1.83 implementation and its 2026-10-18 results.
The active recipe now targets Rust 1.99.1/WASIp3 with LLVM 24; see
[source-build.md](source-build.md). The new port uses `DynClient` and its own
compiler patch. Historical acceptance below does not verify the new release.


The source-built Wasm compiler now executes ordinary procedural macros through
Wasmi 0.40.0. On 2026-10-18 it passed all six standalone acceptance cases,
including unmodified Serde derives and dependency build scripts. It is now
packaged at `dist/compiler` as the `verified-local-source` profile; see the
[aggregate verification](../dist/compiler/verification.json) and
[default-selection smoke test](../../../wisp/out/rust-active-smoke/activation-result.json).
The downloaded legacy bootstrap still has no macro loader.

## Linked compiler acceptance

The [source build receipt](../build/compiler-source-build/build-report.json)
records the completed native/Wasm LLVM 19 and Rust compiler build. The
candidate compiler is `rustc 1.83.0-dev`, hosted on `wasm32-wasip1-threads`;
its SHA-256 is `c3e46e19dc28108f0d6e8613dc425aa186aff499a201a0c086ab28625b6c6468`.
It uses matching source-built standard libraries. The original acceptance used
runner SHA-256 `dd1d8a9800159e8307f7a331d424052f5dbfaad28a601b9322df4862e9f7c4ed`.
The current adapter's hash and acceptance checks are recorded separately in
[runtime-adapter.json](../dist/compiler/runtime-adapter.json).

The [actual six-case results](../build/acceptance-macro-candidate/results.json)
record scalar, `std`, base64, build-script, derive and Serde passing, with each
resulting Wasm application printing `42`. The
[derive report](../build/acceptance-macro-candidate/derive/build-report.json)
contains three completed macro invocations: derive, attribute and function.
The [Serde report](../build/acceptance-macro-candidate/serde/build-report.json)
contains six completed `Serialize`/`Deserialize` invocations and six executed
Wasm build scripts. It checks JSON round trips with generics, borrowed lifetimes
and a renamed field. The reports include the actual compiler commands, artifact
hashes and interpreter traces.

This acceptance used the packaged Wasm compiler for source compilation,
dependencies, build scripts and macro compilation. Native Cargo only resolved
locked metadata. The original build receipt and
[archived tested manifest](../dist/compiler/verification/tested-toolchain.json)
retain their packaging-time acceptance flags. Later test results and local
activation are separate evidence. See [the acceptance instructions](../tests/README.md)
to reproduce the run.

The separate [Wisp Serde check](../../../wisp/out/rust-serde-candidate/result.json)
admitted and executed the resulting Component with result `42`. The
[registry interoperability check](../../../wisp/out/rust-interop-macro-verification/result.json)
also passed Go/C++ importing Rust and Rust importing Go/C++ with this compiler.

## Patch

[`../patches/0001-rust-1.83-wasm-proc-macro-bridge.patch`](../patches/0001-rust-1.83-wasm-proc-macro-bridge.patch)
applies to `oligamiq/rust` revision
`cf327c2068549194a29160499c2ecafa9061e46e`. It adapts the serialized-bridge
approach in bjorn3's PR 157709 to the older typed `Client<I, O>` API.

It keeps native macro declarations and typed clients intact, then adds
compiler-owned dynamic clients. Wasm macro declarations expose scalar
functions for their name, kind and derive helper attributes. The interpreter
forwards existing rustc RPC messages, including token-stream and span handles,
between separate memories.

The old bridge encoded `usize` at the host pointer width. A native 64-bit
compiler talking to a 32-bit guest failed on string RPC. The backport uses
checked `u64` wire integers for lengths and offsets. Compiler and macro
`proc_macro` libraries must therefore come from this same patched source.

The first macro target is `wasm32-unknown-unknown`. Macro compilation and the
application compilation that loads it use `-Zwasm-proc-macros`. A macro module
is an ordinary `--extern macro_name=/absolute/path/macro.wasm` dependency.
The loader uses Wasmi 0.40.0, with its binary parser and `std` feature, inside
the compiler. The macro guest has only the RPC dispatcher import. Direct
WASI filesystem and process APIs are outside this initial target.

Macro instances have a 256 MiB memory limit, a 64 MiB RPC buffer limit, and
a 500 million instruction fuel limit. The Serde acceptance case runs within
these limits; broader workloads have not been characterized. Interpreter traps
use rustc's normal macro error channel.

## Earlier bridge proof

The bounded check uses the pinned native bootstrap compiler,
`rustc 1.82.0-beta.1 (fb1a6f7f4 2024-09-03)`. The patched `proc_macro` library
compiles for native, `wasm32-unknown-unknown`, and `wasm32-wasip1`. The actual
Wasmi loader typechecks against that patched library.

The interpreter check passes with both a native test host and a Wasm test
host running under Wasmtime 38.0.4. In both hosts it interprets a separate
Wasm macro module and verifies:

- Function, attribute and derive declaration discovery.
- A derive helper attribute.
- Returning the original token-stream handle.
- Two independent attribute input token streams.
- Serialized token-stream `to_string` calls and call-site span `source_text`.
- Actual loader and successful invocation trace records for all three clients.

The fixture constructs macro declarations directly. It is a development
bridge proof. It does not establish that rebuilt rustc compiles unmodified
proc-macro crates or Serde derives. Native rustc is used to compile the port
check itself, not as an application build fallback.

Run the checked-in
[`../patches/port-check/check.py`](../patches/port-check/check.py) with the
matching native stage0 prefix and target standard libraries. Its
[README](../patches/port-check/README.md) provides the command and component
checksums. The output directory contains `result.json` and separate native
and Wasm execution traces. Dependencies have a checked-in Cargo lock.

The separate [native compiler development check](../scripts/check-native-macros.py)
then compiled the real local macro crate with the source-built native stage2
compiler, interpreted its Wasm module and ran the resulting WASI application.
Its [result](../build/native-macro-integration-check-explicit-proc-macro/result.json)
is explicitly development evidence, separate from Wasm-hosted acceptance above.
Direct rustc macro-crate commands need `--extern proc_macro`, which the runner
also supplies for macro crates.

## Whole compiler typechecks

The complete Rust compiler source passes `x.py check` at stage 0 for both
`x86_64-unknown-linux-gnu` and `wasm32-wasip1-threads`, using the pinned native
Rust 1.82 beta and SDK 24. Those checks ran before the full source build and
without building LLVM. They check cross-crate types and the Wasm host
configuration; linking and execution are covered by the later evidence above.

[`../patches/port-check/typecheck-receipt.json`](../patches/port-check/typecheck-receipt.json)
records the final patch and source-file hashes, exact commands, configuration
templates and successful log hashes. Render `${SOURCE}` and `${RUST_WASM}`
in those templates using your checkout paths. Save them at the filenames in
the recorded commands, then run from the selected Rust source directory:

```sh
python3 x.py --config .wisp-port-check/compiler-check.toml \
  check compiler/rustc --stage 0 -j2
WASI_SYSROOT="$WASI_SDK_PATH/share/wasi-sysroot" \
  python3 x.py --config .wisp-port-check/compiler-check-wasm.toml \
  check compiler/rustc --stage 0 -j2
```

Set `WASI_SDK_PATH` to the checksum-verified SDK 24 directory. The Wasm
check needs `WASI_SYSROOT` even though LLVM is skipped, because the fork's
LLVM build script validates that variable before its check-only return.

## Compiler execution trace

Set `WISP_WASM_PROC_MACRO_LOG` to a writable JSONL file inside the compiler's
WASI output directory. The actual interpreter loader writes schema version
1 records containing `event`, `status`, `runtime`, `module`, `index`, `name`,
`kind`, and `macroTarget`.

`event=load,status=succeeded` means the module instantiated and its
macro declaration was read. `event=invoke,status=started` is followed by
`succeeded` only after that guest client returns its serialized response,
or `failed` if interpretation traps. Trace write failures reject the macro.
`runtime` is `wasmi`; `macroTarget` is `wasm32-unknown-unknown`; `kind` is
`function`, `attribute`, or `derive`.

The runner must use a fresh trace file per compiler execution and match
successful invocation paths to the macro artifacts it independently hashes.
A trace file's existence or a successfully compiled macro module alone is
not macro execution evidence.

## Supported subset and further checks

The local case covers multiple macros in one module and derive helper
attributes. The unmodified Serde case covers generics, borrowed lifetimes and
renamed fields. The separate Wisp negative case rejects Serde derives on a union
with the expected Rust diagnostic and no application artifact. The compiler's
fatal-error path then aborts with exit `134` and an `unreachable` Wasmtime
backtrace. Rejection works, but diagnostic failure paths remain noisy and need
further work.

Macro modules target `wasm32-unknown-unknown` and have no direct filesystem,
environment or process APIs. Build scripts run separately as WASI commands.
The Serde dependency build scripts handle unavailable `Command` probes with
conservative defaults; those defaults compiled and executed successfully in the
acceptance run. General process spawning and native C build dependencies remain
outside the supported subset.
