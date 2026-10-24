# Rust compiler acceptance

The active recipe targets Rust 1.99.1/WASIp3 with LLVM 24. Run this suite again
for that compiler; the dated Rust 1.83 results below remain historical evidence.
Use Wasmtime 49 for the new compiler. The compiler's host target does not change
these fixtures' WASIp1 application target.


The fixtures contain ordinary Cargo projects. They do not require users to write
WIT or ABI annotations. Every compiler case must produce a `wasm32-wasip1`
command module that prints `42` followed by a newline.

| Case | Required behavior |
| --- | --- |
| `scalar` | Public `u32`, `bool` and `u64` functions, including a value above 2^53 |
| `std` | `Vec`, `String`, generics and `Result` behind scalar public functions |
| `base64` | Vendored `base64 = 0.22.1` with an encoding round trip |
| `build-script` | Run `build.rs` in Wasm, read an input, generate source, apply cfg and compile-time environment output |
| `derive` | Execute local derive, function and attribute macros, including a derive helper attribute |
| `serde` | Unmodified vendored Serde derives, JSON round trips, borrowed generics, renamed fields and dependency build scripts |

`serde` is pinned to `1.0.228`, and `serde_json` to `1.0.150`. Cargo lockfiles pin
the registry dependency graphs and checksums. Their cached package metadata
requires Rust 1.71 or earlier, below the candidate compiler's Rust 1.83 base.
Lockfiles were resolved offline with native Cargo. No fixture source was compiled
by native Rust as acceptance evidence.

List the requirements without running a compiler:

```sh
python3 tests/acceptance.py --list
```

Run one case with an explicit compiler runner and Wasmtime:

```sh
python3 tests/acceptance.py \
  --runner scripts/runner.py \
  --wasmtime /absolute/path/to/wasmtime \
  --case scalar \
  --out artifacts/acceptance-scalar
```

`--runner-command '["python3", "-I", "/absolute/path/to/runner.py"]'` supports runners
that are not directly executable. Repeat `--case` to select several cases, or
omit it to run all six. `--vendor-dir /absolute/path/to/vendor` passes an already
prepared offline dependency directory to the runner. The runner may resolve
metadata or prepare vendored sources with native Cargo before compilation, but
it must compile application code, dependencies, build scripts and procedural
macros inside Wasm.

The output directory must be new. The harness stores runner logs, the build
report, application logs and `results.json`. It stops at the first failure.
Exit `2` means the runner or a required toolchain capability is unavailable.
Missing tools never count as passed tests. `--list` only lists requirements.
The four cross-language flows now have a passing separate Wisp registry verifier
at [../../../wisp/examples/rust-import-wasm/verify.py](../../../wisp/examples/rust-import-wasm/verify.py).
It publishes and fetches real releases, stops the registries, then compiles the
consumers inside Wasm and checks editor support. These standalone compiler cases
do not run that separate verifier or claim fresh integration results.

## Results: 2026-10-18

All six standalone cases passed with the source-built candidate. Each resulting
Wasm application printed `42\n`. The [suite result](../build/acceptance-macro-candidate/results.json)
records the runner command, application path, digest and stdout for every case.

| Case | Result | Build evidence |
| --- | --- | --- |
| `scalar` | Passed | [report](../build/acceptance-macro-candidate/scalar/build-report.json) |
| `std` | Passed | [report](../build/acceptance-macro-candidate/std/build-report.json) |
| `base64` | Passed | [report](../build/acceptance-macro-candidate/base64/build-report.json) |
| `build-script` | Passed | [report](../build/acceptance-macro-candidate/build-script/build-report.json) |
| `derive` | Passed | [report](../build/acceptance-macro-candidate/derive/build-report.json) |
| `serde` | Passed | [report](../build/acceptance-macro-candidate/serde/build-report.json) |

The compiler SHA-256 is
`c3e46e19dc28108f0d6e8613dc425aa186aff499a201a0c086ab28625b6c6468`;
the tested runner SHA-256 is
`dd1d8a9800159e8307f7a331d424052f5dbfaad28a601b9322df4862e9f7c4ed`.
The [source build report](../build/compiler-source-build/build-report.json) and
[packaged provenance](../dist/compiler/source-build.json) identify the
matching source revisions, patches, sysroots and original build inputs. Their
build-time acceptance flags describe the build/package step. The acceptance
results above record the later test run.

The local macro case records three completed Wasmi invocations: `Answer`,
`checked_answer` and `answer_literal`. The Serde case records 26 executions,
including six completed `Serialize`/`Deserialize` invocations and six executed
Wasm build scripts: `serde_core`, `proc-macro2`, `quote`, `serde`, `zmij` and
`serde_json`. Its JSON round trips exercise generic values, borrowed lifetimes,
and a field renamed to `display_name`. Application source, dependency source,
macro crates and build scripts were compiled inside Wasm.

Separate Wisp integration runs also passed with this compiler:

- [Wisp Serde result](../../../wisp/out/rust-serde-candidate/result.json):
  admitted the generated Component and executed it with result `42`, with six
  real macro invocations and six Wasm build scripts.
- [Registry interoperability result](../../../wisp/out/rust-interop-macro-verification/result.json):
  published and fetched packages, stopped the registries before consumer builds,
  and passed Go/C++ importing Rust and Rust importing Go/C++, including editor
  checks.
- [Activation result](../../../wisp/out/rust-active-smoke/activation-result.json):
  selected the source-built compiler through Wisp's shared environment
  with Rust overrides unset, built an ordinary Rust project and executed
  `add(20,22)` with result `42`.

The packaged profile is `verified-local-source`. Its
[aggregate verification](../dist/compiler/verification.json) records the
compiler, runner, inventory and immutable receipts. The
[original tested manifest](../dist/compiler/verification/tested-toolchain.json)
is archived separately from the promoted local manifest. Promotion changed
descriptive release and verification fields while preserving compiler, runner,
inventory and original receipt bytes.

The Wisp negative case correctly rejects a Serde derive on a union and leaves
no application artifact. After the Rust diagnostic, the current compiler's
fatal-error path aborts with exit `134` and an `unreachable` Wasmtime backtrace.
Error reporting is noisy. This does not establish clean handling of every
macro diagnostic.

The Python test suite also passed: 92 tests. These policy and scheduler tests
are separate from the actual compiler acceptance runs. Macro filesystem,
environment and process APIs, general build-script process spawning and native
C build dependencies remain unsupported.

## Source-built candidate acceptance

The source build must finish before these commands can run. Its installed
sysroot needs `std` and `proc_macro` for `wasm32-wasip1-threads`,
`wasm32-wasip1` and `wasm32-unknown-unknown`. The build checks those target
libraries before recording success. Packaging checks the compiler hash and
creates a separate candidate without replacing the bootstrap:

```sh
python3 -I scripts/build-compiler.py --package --offline \
  --source build/source-rust-cf327c206854 \
  --out build/compiler-source-build \
  --candidate-out dist/compiler
```

The candidate includes its own runner, pinned Wasmtime, compiler, sysroot and
license inventory. Packaging enables the macro loader for testing and initially
records `firstUsableRelease`, `deriveAcceptancePassed` and
`serdeAcceptancePassed` as false. Later acceptance and local activation have
separate evidence; successful packaging alone does not establish that macros
work.

From the `rust/` directory, run all six cases against that exact candidate:

```sh
rust_candidate_dir="$(pwd)/dist/compiler"
rust_candidate_runtime="$WISP_RUST_WASMTIME"
rust_candidate_runner="$(python3 -I -c 'import json,sys; print(json.dumps([sys.executable,"-I",sys.argv[1]]))' "$rust_candidate_dir/scripts/runner.py")"
python3 -I tests/acceptance.py \
  --runner-command "$rust_candidate_runner" \
  --wasmtime "$rust_candidate_runtime" \
  --vendor-dir "$(pwd)/build/vendor" \
  --timeout 3600 \
  --out build/acceptance-macro-candidate
```

Use a fresh output directory for each run. The harness forwards its explicit
Wisp-owned Wasmtime path to the runner. The package does not contain a runtime.
The host must provide legacy WASI threads; Wasmtime 38.0.4 is tested. An inherited runtime cannot select a
different compiler process. The fixtures remain in this source repository;
the external runner loads the compiler and sysroot from the candidate.

The prepared `build/vendor` contains the twelve locked registry packages used
by `base64` and `serde`. The runner checks their file inventories and checksums.
Native Cargo reads these locked graphs; source compilation stays inside Wasm.
The local derive project has no registry dependencies. Its application calls
all three macro kinds and consumes `#[answer(42)]` as a derive helper attribute.
The Serde project checks an ordinary `Score` and a borrowed `Envelope<'a, T>`
with a renamed `display_name` field. Both applications must still print `42`.

For the in-process loader, acceptance requires actual completed invocation
traces. A macro file or a `loader` label alone cannot pass. Each trace must
match its hash, belong to the consuming compiler command and name a macro
compiled earlier for `wasm32-unknown-unknown`. The local case requires named
derive, function and attribute invocations; Serde requires `Serialize` and
`Deserialize`. A failed compiler or interpreter invocation stops acceptance.

Native `cargo run` can check that these fixtures are valid Rust. Its output is
fixture verification only and does not satisfy these Wasm compiler cases.
The negative Serde union diagnostic is covered by the separate Wisp run above,
including the current compiler abort/backtrace limitation.

## Runner protocol

The harness invokes exactly this interface:

```text
<runner argv> build
  --manifest-path /absolute/path/to/Cargo.toml
  --target wasm32-wasip1
  --out-dir /absolute/path/to/fresh/case-directory
  --offline
  --report /absolute/path/to/build-report.json
  --wasmtime /absolute/path/to/the/configured/wasmtime
  [--vendor-dir /absolute/path/to/vendor]
```

Exit `0` means a report and executable application Wasm exist. Any failure must
return nonzero. Use exit `2` for an unavailable compiler or unsupported case.
The report uses schema version 1:

```json
{
  "schemaVersion": 1,
  "manifestPath": "/absolute/path/to/Cargo.toml",
  "target": "wasm32-wasip1",
  "compiler": {
    "path": "/absolute/path/to/rustc.wasm",
    "sha256": "SHA256_OF_COMPILER",
    "host": "wasm"
  },
  "artifact": {
    "path": "application.wasm",
    "sha256": "SHA256_OF_APPLICATION"
  },
  "executions": [
    {
      "kind": "compiler",
      "program": "/absolute/path/to/rustc.wasm",
      "sha256": "SHA256_OF_COMPILER",
      "argv": ["/absolute/path/to/wasmtime", "run", "/absolute/path/to/rustc.wasm", "..."],
      "cwd": "/absolute/path/to/work-directory",
      "exitCode": 0
    }
  ],
  "dependencies": [],
  "buildScripts": [],
  "proceduralMacros": []
}
```

`argv` must record actual command arguments. Paths and hashes must identify the
local Wasm files that ran. The harness checks their Wasm headers and SHA-256,
requires Wasmtime to match its configured executable, and rejects an application
artifact outside the case output directory. It then runs the application itself
and checks its output. Runner reports provide execution records; the runner's
implementation must also be reviewed to establish that it records real calls.
A `host: "wasm"` string alone does not prove compilation occurred in Wasm.

A registry dependency record is
`{"name": "base64", "version": "0.22.1", "source": "vendored"}`.
Build script and macro records contain `package`, `program`, `sha256` and an
`execution` index into `executions`. The referenced execution must have kind
`build-script` or `procedural-macro` and invoke the same Wasm file through
Wasmtime. A macro loaded inside the Wasm compiler may instead use
`"loader": "compiler-wasm-runtime"` and reference that compiler execution.
Its macro artifact must still be Wasm. It must also provide `compilation`,
`runtime: "wasmi"`, `macroName`, `macroKind`, `macroIndex`, `trace` and
`traceSha256`. The trace records a successful load and a started/completed
invocation from that compiler execution. Native shared libraries fail validation.

## Harness checks

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

These tests exercise report validation using small header fixtures. They do not
invoke rustc, claim any fixture compiled, or substitute for compiler acceptance.
