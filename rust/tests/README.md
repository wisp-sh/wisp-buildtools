# Rust compiler acceptance

The active recipe targets Rust 1.99.1/WASIp3 with LLVM 24. Use Wasmtime 49.
The compiler's host target does not change these fixtures' WASIp1 application
target. Run the commands below from `rust/`.

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
requires Rust 1.71 or earlier, below the selected compiler's version.
Native Cargo resolves metadata; acceptance requires compilation inside Wasm.

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
## Source-built candidate acceptance

Build and test the candidate from the repository root:

```sh
python3 build.py build rust --jobs 2
python3 build.py test rust
```

The test command vendors the locked base64 and Serde dependencies, then runs all
six cases against `rust/dist/compiler/scripts/runner.py`. The compiler must have
matching `wasm32-wasip3`, `wasm32-wasip1` and `wasm32-unknown-unknown` sysroots.
See [source builds](../docs/source-build.md) for individual packaging commands.
Packaging alone does not establish successful macro acceptance.

For a manually prepared offline vendor directory, run from `rust/`:

```sh
python3 -I tests/acceptance.py \
  --runner-command '["python3", "-I", "dist/compiler/scripts/runner.py"]' \
  --wasmtime "$WISP_RUST_WASMTIME" \
  --vendor-dir "$PWD/build/vendor" \
  --timeout 3600 \
  --out build/acceptance
```

Use a fresh output directory for each run. The host is external to the package
and must support WASIp3. Each macro trace must identify a completed Wasmi
invocation from the compiler command that consumed it. Merely finding a macro
module or a loader label cannot pass acceptance. The local case requires derive,
function and attribute invocations; Serde requires `Serialize` and `Deserialize`.
A failed compiler or interpreter invocation stops the run.

Native `cargo run` can check that the fixtures are valid Rust. It does not
satisfy Wasm compiler acceptance. Wisp registry and cross-language integration
have separate verifiers in the Wisp repository. General build-script subprocesses
and native C dependencies remain unsupported.

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
