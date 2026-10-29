# StarlingMonkey spike, 2026-10-25

The original decision under the fully Wasm-hosted snapshot rule was **no-go**.
The native Wizer follow-up below supersedes that snapshot restriction. Full
migration acceptance remains unverified. QuickJS remains the
engine. The prebuilt StarlingMonkey runtime works cold as a CLI Component, and
the Rust splicer runs as a WASIp1 guest. However, skipping Wizer does not produce
a working authored-WIT library: the first arithmetic export traps, including
when feature stubbing is omitted. No native snapshot step was added in that original spike.

This is a failure of the stock snapshot-free library path tested here. It does
not prove that a new cold library initializer or Wasm-hosted snapshotter cannot
be implemented.

## Pinned inputs and stage boundaries

[checksums/starlingmonkey.json](../checksums/starlingmonkey.json) pins the release,
source, lock correction, bootstrap compiler identities and measured guest.
SpiderMonkey was not built from source.

| Input | Bytes | SHA-256 |
| --- | ---: | --- |
| ComponentizeJS 0.23.0 archive | 12,318,973 | `b2e4b6fdee055805370950f127c21d7c549dcc7a1e907f20c5ee769ebba7ad10` |
| Prebuilt runtime | 10,520,737 | `6094cd93d966f9bec9b01332db6e0eedbaf22f60b745c9f823f21e33cbd4d365` |
| Source archive | 162,093 | `8178fb678ec1b8a33bbf3fa8db066bffe4dbf6a92c235150f56622b44ea27e10` |
| Built WASIp1 splicer CLI | 16,512,750 | See the spike lock |

The source commit is `4d812f5a7b524cea5bcfd565cac55f3fab876a57`.
[Release artifact](https://github.com/bytecodealliance/ComponentizeJS/releases/tag/0.23.0).

The pinned [componentize.js](https://github.com/bytecodealliance/ComponentizeJS/blob/4d812f5a7b524cea5bcfd565cac55f3fab876a57/src/componentize.js)
generates bindings with the Rust splicer, launches native Wizer on
`componentize.wizer`, then stubs unused WASI imports and assembles the Component.
Its Weval alternative is also native. [Wizer 10](https://github.com/bytecodealliance/wizer/blob/v10.0.0/Cargo.toml)
uses Wasmtime 36 to execute initialization and capture memory and globals.
No supported WASIp1 snapshotter was established. The original stage rule excluded native Wizer for application builds, so it
was not run in that spike. The later approval is recorded below.

The prebuilt embedding's `call` function assumes initialized runtime state.
The [initialization wrapper](https://github.com/bytecodealliance/ComponentizeJS/blob/4d812f5a7b524cea5bcfd565cac55f3fab876a57/embedding/embedding.cpp)
loads the source and generated bindings and registers the exported functions.
A no-snapshot library adapter needs to perform that work at runtime, supply
embedded source/initializer data, and initialize before calls and reallocations.
Simply skipping Wizer is insufficient.

## WASIp1 splicer bootstrap

The upstream Cargo manifest requires `js-component-bindgen` 2.11, but its lock
contains 2.10.1. The initial `--locked` build refused to proceed. The reviewed
[lock correction](../tools/starlingmonkey-spike/wasip1-lock.patch) makes the
requirement exact at 2.11.0 and updates that one package. Other dependency
versions remain unchanged. Both the patch and resulting lock hash are pinned.

After extracting the pinned source archive into
`build/starlingmonkey-spike/source`, the bootstrap commands were equivalent to:

```sh
patch --batch -p1 -d build/starlingmonkey-spike/source \
  -i "$PWD/javascript/tools/starlingmonkey-spike/wasip1-lock.patch"
export RUSTC=/home/luap/.rustup/toolchains/1.99.0-x86_64-unknown-linux-gnu/bin/rustc
export CARGO_BUILD_JOBS=1 CARGO_INCREMENTAL=0
export CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_DEV_CODEGEN_UNITS=1
export CARGO_TARGET_DIR="$PWD/build/starlingmonkey-spike/cargo-target"
export TMPDIR="$PWD/build/starlingmonkey-spike"
/home/luap/.rustup/toolchains/1.99.0-x86_64-unknown-linux-gnu/bin/cargo build \
  --locked --offline --manifest-path build/starlingmonkey-spike/source/Cargo.toml \
  --target wasm32-wasip1 -p spidermonkey-embedding-splicer --bin splicer -j1
```

Missing archives were fetched before the final offline build. The result was:

```text
Finished `dev` profile [unoptimized] target(s) in 2m 55s
```

Native Rust bootstrapped the tool package only. Both `splice-bindings` and
`stub-wasi` subsequently executed inside the Wasm guest. Component assembly and
validation used the pinned wasm-tools guest. No Node or native binding generator
was used during these application probes.

## No-snapshot results

The CLI runtime was assembled directly with the pinned Preview 1 reactor
adapter. It read bundled JS from a preopened directory at startup, with no
snapshot. Three fresh official Wasmtime 50.0.0 processes used
`-Ccache=n -Oopt-level=0 -Cparallel-compilation=n -Shttp`.

| Measurement | Result |
| --- | ---: |
| Cold CLI Component | 10,576,712 bytes, about 10.09 MiB |
| Cold process samples | 14.643 s, 14.308 s, 14.477 s |
| Median cold process time | 14.477 s |
| Largest child process RSS during the CLI spike | 301,076 KiB |
| CLI spike scope peak, including charged cache | 361,381,888 bytes |
| Existing QuickJS add Component | 958,589 bytes |
| Stubbed no-snapshot arithmetic Component | 10,503,709 bytes |
| Unstubbed no-snapshot arithmetic Component | 10,575,483 bytes |

The cold process times include process startup, opt0 JIT compilation,
instantiation and JS execution. They do not isolate SpiderMonkey initialization.
The CLI Component hash is
`8a688d356787bfd414983ce87c3375462451048f505e4960d96ab2ba2d917910`.
All measured Components fit the existing 16,777,216-byte cap. It was not raised.

The authored-WIT probe used `examples/add.wit`, generated bindings in the WASIp1
splicer, and assembled the result without Wizer. Both binaries validated.
The stubbed variant trapped in `random_get` during initialization. The unstubbed
variant, executed on the HTTP-capable official host, also failed:

```text
--invoke add(20,22)
exitCode: 134
uncaught exception: unknown (can't convert to string)
Redirecting call to abort() to mozalloc_abort
... <unknown>!add
wasm trap: wasm `unreachable` instruction executed
```

The failed invocations took 16.347 s and 12.467 s respectively, including JIT.
These are rejection timings, not usable library startup measurements. The
splicer emits bindings and initialization inputs; the prototype does not add a
new adapter to embed and initialize those inputs at library startup.
[Raw results](starlingmonkey-results.json) retain sizes, timings and diagnostics.

The Wisp-owned Wasmtime 50 host also rejected the unstubbed CLI probe with:

```text
Error: Cannot enable wasi-http when the binary is not compiled with this feature.
```

The already installed official Wasmtime 50.0.0 binary worked for the CLI probe.
Its SHA-256 is pinned. Wisp was not edited. Feature stubbing can remove HTTP
imports from pure worlds, but it does not solve the missing cold initializer.

## APIs, npm packages and async

The cold CLI probe passed URL parsing, text encoding, base64 conversion,
SHA-256 through `crypto.subtle`, and a self-contained readable stream. It found
`fetch`, writable/transform streams and timer globals. Network requests and
actual timers were not exercised. `Buffer` and `process` were undefined.

An exported ES-module `async function add` returned `await Promise.resolve(a+b)`
and produced 42. This checks export syntax and microtask resolution inside the
runtime. Async WIT exports remain unverified because the library pipeline fails
its first synchronous export.

The package probes used the existing facades and exact pinned archives, bundled
through the WASIp1 esbuild guest. Their recipe logic checks ran in StarlingMonkey:

| Package | Logic checks | Remaining workarounds |
| --- | --- | --- |
| yaml 2.8.1 | 7/7 passed: mapping, aliases, multiple documents, stringify, syntax error, invalid JSON, duplicate key | Keep `browser-buffer.ts`, `browser-process.ts`, and all four substitutions in `browser-target.patch`. StarlingMonkey supplies neither Node Buffer nor process. Its browser branch can use the engine's `atob`/`btoa`. |
| markdown-it 15.0.3 | 6/6 passed: headings, Unicode, inline markup, HTML escaping, linkification, table | No application shim needed. Neither old QuickJS compiler patch was used. |

These were CLI-runtime logic checks. The recipes' authored-WIT Components,
generated npm/Rust bindings and distribution checks did not pass acceptance.
No shared wisp-pkgs checkout was changed; inputs were copied/extracted into the
task's own disk-backed scratch directory.

## Acceptance decision

| Gate | Result |
| --- | --- |
| Pinned prebuilt runtime | Passed |
| WASIp1 splicer and Wasm-hosted assembly | Passed after the pinned lock correction |
| Wasm-hosted snapshotting | Not established; native Wizer excluded |
| Cold startup and Component sizes measured | Passed for CLI runtime; library variants reject execution |
| Every current JavaScript verification case on StarlingMonkey | Failed at the initial arithmetic-export probe; broader cases not run |
| yaml/markdown-it full Component recipe acceptance | Logic passed; ABI/distribution checks not completed |
| No-I/O async WIT exports | Unverified; ES-module Promise probe passed |

These missing gates prevent migration. A follow-up needs a Wasm-hosted
snapshotter or a cold authored-WIT initializer with embedded inputs, followed by
all JavaScript cases, both full recipes and async WIT exports. The production
adapter retains QuickJS and its existing limits.

## Reproduce the runtime probes

From the repository root, with compiler guests installed and the splicer built:

```sh
export WISP_WASMTIME="$PWD/cwasm.py"
export WISP_CWASM_RUNTIME=/home/luap/vise/.tools/shared-hosts/wisp-wasmtime-50.0.0/wasmtime
export TMPDIR="$PWD/build/starlingmonkey-spike"
python3 -B javascript/tools/starlingmonkey-spike/spike.py --offline \
  --runtime /home/luap/vise-buildtools/.ci/toolchains/wasmtime/wasmtime-v50.0.0-x86_64-linux/wasmtime \
  --recipes /home/luap/wisp-pkgs --recipe-cache /home/luap/.t3/projects/wisp-pkgs \
  --splicer build/starlingmonkey-spike/cargo-target/wasm32-wasip1/debug/splicer.wasm
```

Omit `--offline` to fetch the pinned runtime on a first run. The recorded CLI
and recipe run printed:

```text
yaml yaml: 7/7 recipe checks passed
markdown-it markdown-it: 6/6 recipe checks passed
```

The additional `api-check.mjs` probe returned:

```json
{"passed":true,"asyncModuleExport":42,"url":true,"encoding":true,"base64":true,"cryptoSubtle":true,"streams":true,"fetchGlobal":true,"timerGlobals":true,"nodeBuffer":false,"nodeProcess":false,"witAsyncExport":"not tested"}
```

The script now includes that probe and the optional authored-WIT probes. The
recorded run executed those additional probes separately on the same pinned
inputs. Each compiler operation remained Wasm-hosted.

## Resources

Initially only about 5.2 to 5.7 GiB was free, so heavyweight builds were deferred.
Another session later freed space, bringing availability to about 16 GiB.
The selected tsrs source build and splicer bootstrap then completed serially.
The final spike decision is based on the export failures, not the earlier disk
constraint. The original ts-rust source build was stopped when tsrs was selected.

The native compiler-package builds used one worker and cgroups with swap
disabled. The splicer bootstrap/probes were limited to 3 GiB; runtime and
JavaScript verification used 2 GiB. Direct compiler children stayed in their
scopes according to `/proc/PID/cgroup`. Scratch, caches and artifacts used ext4.
Existing host swap remained near 2.9 GiB, with no sustained swap-out observed.
No unrelated data or caches were removed, and SpiderMonkey source compilation
was not attempted.

## Approved native Wizer follow-up

The user subsequently approved normal Wizer as part of Wisp's native execution
host. Application initialization still executes as Wasm in Wasmtime; native
Wizer captures and serializes the initialized state. The buildtools
[`wizer` package](../../wizer/README.md) supplies a pinned, constrained adapter and
records `nativeSnapshotHostApproved: true` and `fullyWasmHostedBuild: false`.

With the existing official Wasmtime 50.0.0 host, the same pinned WASIp1 splicer
and runtime now produce working authored-WIT arithmetic Components. Both
`export function add` and `export async function add`, with the latter awaiting
`Promise.resolve(a+b)`, return 42 through the Component's public `add(20,22)`
export. These exercise the f64 ABI declared by `examples/add.wit`.

The synchronous Component is 11,953,396 bytes; the async Component is 11,955,266
bytes. Snapshot generation took 20.129 and 21.781 seconds. Fresh process
invocations took 13.923 and 15.513 seconds, including opt0 JIT compilation. Both
fit the existing 16 MiB cap. `check_init` returned zero before assembly, and both
Components passed wasm-tools guest validation.

The reproducible probe is:

```sh
export WISP_WIZER_WASMTIME=/path/to/the-pinned-wasmtime-50-host
python3 -B javascript/tools/starlingmonkey-spike/native_snapshot.py
```

[Raw native Wizer results](starlingmonkey-native-wizer.json) record source,
bindings, output and host hashes. A preliminary run supplied i32 metadata for
the f64 WIT signature and returned an incorrect numeric value; the recorded
passing probe uses the declared f64 parameter and return types.

This removes the initial library initialization blocker when the approved
native host capability is used. It does not claim that all JavaScript
verification cases or the full yaml/markdown-it Component and distribution
checks have been rerun on StarlingMonkey. QuickJS remains the production
adapter's default until those migration gates pass.

The same two probes also passed on the newly source-built Wisp host with Wizer
enabled, SHA-256
`14026ac1ef087f0a027bffd679ddbaa7b5400f7167bff1a0d7dc56e2d1cbfaaa`.
The synchronous and async Components measured 11,953,418 and 11,955,293 bytes.
Snapshot times were 23.714 and 25.592 seconds; fresh invocations took 17.275 and
17.980 seconds including JIT. These are single observations per variant, not
benchmark medians. Neither snapshot contained the private checkout prefix.
[Wisp host results](starlingmonkey-wisp-wizer.json) record the complete receipts.
