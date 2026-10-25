# Wizer host adapter

This package exposes normal Wizer 50.0.0 through the explicitly approved native
Wisp execution host. Application initialization runs as Wasm in Wasmtime. The
native host captures initialized memory and globals and writes the snapshot.
This is a documented exception to requiring every build transformation to run
inside a compiler guest.

Wizer is useful for interpreter startup, compiler initialization and embedded
lookup tables. It saves initialization work. It does not accelerate the main
computation. Each use must check that initialization leaves state safe to reuse,
particularly randomness, clocks, open resources and request-specific data.

## Host selection and pinning

Set `WISP_WIZER_WASMTIME` to a Wasmtime host that includes its `wizer` subcommand.
`WISP_BOOTSTRAP_WASMTIME` is the fallback. `toolchain.lock.json` accepts either:

- The pinned official Linux x86_64 Wasmtime 50.0.0 binary, SHA-256
  `f95e5beba6d176b447c2939607bae968ea47f87b156ddf559762f1bb3c00e9b7`.
- Wisp's source-built host with exactly the pinned Wasmtime CLI source, feature
  list including Wizer, Component preload patch and matching binary receipt.

The source-built host must have a passing Wizer snapshot smoke in its receipt.
Another native executable or an older host receipt is rejected. The host is external and Wisp owns its
installation. This bundle contains the Python adapter and provenance metadata,
so it has no Wasm compiler payload or `.cwasm` companion. Its bundle version is
`0.1.0`, and its upstream Wizer version is `50.0.0`.

A guest port was explored before the native host exception was approved. That
port is not part of the package. The selected implementation uses the normal
upstream Wizer implementation and WASI host.

## Build, test and package

```sh
export WISP_WIZER_WASMTIME=/path/to/wasmtime-50.0.0-with-wizer
export WISP_HOSTS_DIR=/path/to/wisp/.tools/shared-hosts
source env.sh
python3 build.py build wizer
python3 build.py test wizer
python3 build.py package wizer --output-dir dist/wizer-packages
python3 build.py validate dist/wizer-packages/wizer.wisp.zip
```

`build` verifies the host and writes a checked adapter manifest. It does not
install or rebuild Wasmtime. The package has no runtime compiler-package
dependency. `wasm-tools` is a build/test dependency for fixture generation and
validation. It remains a Wasm guest.

Wisp's host source recipe enables the `wizer` feature and checks a snapshot that
returns 42 from initialized memory and globals. It also verifies removal of the
initialization export. Build that host with one worker and a disk-backed cache.

## Snapshot a module

```sh
python3 wizer/snapshot.py input.wasm -o initialized.wasm \
  --init-func wizer.initialize
```

For an initializer that reads WASI files and environment values:

```sh
python3 wizer/snapshot.py input.wasm -o initialized.wasm --allow-wasi \
  --mapdir /inputs::/absolute/project-inputs --env MODE=build \
  --stdin initializer-arguments.txt
```

Only Core Wasm input is accepted. The input is staged under the stable `input.wasm` argv name, and its bytes are
checked again before publication. The default initialization export is `wizer.initialize` for
compatibility with existing tools; pass another name explicitly when needed.
The CLI requires fresh output and receipt paths and publishes the complete
receipt before making the module visible. Each invocation creates a fresh
Wasmtime process and instance. Directories and
environment variables are opt-in; no process environment or directories are
inherited by the guest. Standard input is the supplied bytes, and output is
captured. Network access and name lookup are disabled. Unknown function imports
trap if initialization calls them.

The host receives one compiler worker, a 1 GiB per-memory limit, execution fuel,
a Wasm execution timeout and an outer process timeout. The default execution
budget is 120 seconds and five billion fuel units. Native module compilation is
outside the Wasm execution timeout but inside the process timeout. Use an
external cgroup to bound native JIT memory and CPU during large builds.

The native host's directory preopens permit file writes as well as reads.
Provide staged input directories and only the capabilities initialization needs.
Neither fuel nor execution timeouts establish deterministic initialization.
Clocks and randomness remain real WASI values when enabled.

The `.build.json` receipt records the exact host binary hash, source module,
initializer, explicit grants, environment-value hashes, standard-input hash,
limits, output hash and adapter recipe. It explicitly records
`fullyWasmHostedBuild: false` and `nativeSnapshotHostApproved: true`. It does not
invent a complete application input inventory from arbitrary preopened trees;
the calling compiler must freeze and record those source inputs.

## Verification and limits

`python3 -B wizer/verify.py` runs 14 actual snapshot checks. They cover initialized
memory/global values, SIMD state, reactor initialization, removal of the
initializer export, failed initialization, fuel exhaustion, environment
isolation, explicit file reads, absence of implicit preopens, rejection of
unrecognized native hosts and native application binaries, and preservation of
existing outputs when initialization fails. The resulting
modules are validated through the wasm-tools guest and run in fresh instances.

Upstream Wizer limits still apply, including unsupported table mutation and
reference state. A successful snapshot does not establish that a program's
initialization is safe to reuse or that every export works.

StarlingMonkey's pinned native snapshot probe is separate:

```sh
python3 -B javascript/tools/starlingmonkey-spike/native_snapshot.py
```

It uses the existing pinned WASIp1 splicer and wasm-tools guests, the prebuilt
runtime, and this native snapshot adapter. It exercises the authored-WIT
arithmetic export for both a synchronous function and an async function whose
Promise settles without I/O. The full JavaScript and recipe migration gates
remain separate acceptance work.

## Recorded Wisp host verification

Set `WISP_CHECKOUT` to the Wisp worktree containing the pinned host recipe.
The recorded build used:

```sh
systemd-run --user --scope --unit=wisp-wizer-host-build \
  -p MemoryMax=4G -p MemorySwapMax=0 \
  env CARGO_BUILD_JOBS=1 CARGO_INCREMENTAL=0 RAYON_NUM_THREADS=1 \
  TMPDIR="$PWD/build/wizer" python3 -B \
  $WISP_CHECKOUT/tools/shared-hosts/build.py \
  --jobs 1 \
  --prefix $WISP_CHECKOUT/.tools/shared-hosts/wisp-wasmtime-50.0.0 \
  --cache "$PWD/build/wizer/wisp-host"
```

Cargo reported:

```text
Finished `release` profile [optimized] target(s) in 13m 40s
```
 The host is 29,421,248
bytes, SHA-256
`14026ac1ef087f0a027bffd679ddbaa7b5400f7167bff1a0d7dc56e2d1cbfaaa`.
Its external-module and Wizer snapshot smoke checks passed. The minimal Wisp
CLI omits help text; the adapter verifies the source-build smoke receipt.
Use the adapter's `--help` for its supported options.

The build used a 4 GiB cgroup with swap disabled and one compiler worker. Peak
scope memory was 2,046,070,784 bytes. The monitor observed no reserve breach or
sustained swap-out. At least 11,494,191,104 bytes of disk remained free. Scratch,
Cargo output and downloads stayed on ext4.

With `WISP_WIZER_WASMTIME` pointing to that host:

```sh
python3 -B wizer/bootstrap.py --rebuild
python3 build.py test wizer
python3 -B -m unittest test-cwasm test-packages test-build
```

Output included `Wizer: 14/14 checks passed` and `Ran 97 tests ... OK`.
[Adapter verification](verification.json) and
[host verification](host-verification.json) retain the measured evidence.

Package validation reported `valid: true`, and installation reported
`installed: true`. The installed adapter was checked with:

```sh
python3 build.py install dist/wizer-packages/wizer.wisp.zip \
  --prefix "$PWD/build/wizer/installed"
python3 -B build/wizer/installed/wizer/snapshot.py \
  build/wizer/verify/memory-global.wasm -o build/wizer/installed-output.wasm
"$WISP_WIZER_WASMTIME" run -Ccache=n --invoke get build/wizer/installed-output.wasm
```

The result was `42`. Its receipt selected `wisp-source-host`, recorded native
snapshot approval, and recorded `fullyWasmHostedBuild: false`.
