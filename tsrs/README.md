# tsrs TypeScript checker

This shared tool package runs tsrs 0.18.0 inside a WASIp1 guest. It supplies the
JavaScript adapter's optional `--typecheck` stage. Native Python hosts the
compiler through `tsrs_host.fs` callbacks; application type checking stays in
Wasm. No Node, npm or native TypeScript compiler is required for source builds.

The selected upstream package is `@tsrs/wasm@0.18.0-ts7.1.0-dev.20260929`, from
commit `5f92808bc92bb97b06212153ab73d28d551c0b9b`. It targets the TypeScript
revision `b85298b6a81f772d080b0455de0ca9d744cd6fd6`. The unreleased 0.19 source
upgrade is not selected. [Upstream Wasm interface](https://github.com/oxc-project/tsrs/blob/v0.18.0/npm/tsrs-wasm/README.md).

## Why tsrs instead of ts-rust

Compared with the existing ts-rust application recipe, tsrs has an official
single-threaded WASIp1 package, upstream
Wasm/native differential gate, source-path checks and documented fresh-instance
API. These fit this toolchain's hosting model. Both projects are Rust ports of TypeScript;
raw test counts and native PGO/BOLT benchmarks are not directly comparable.
[Upstream Wasm gate](https://github.com/oxc-project/tsrs/blob/v0.18.0/tools/wasm/gate.sh).

The local measurements also favor the selected artifact. Both used Wasmtime
Python 50.0.1, Cranelift opt0, serial compilation, the host CPU baseline, and the
same strict one-file no-emit project. Values below are three-sample medians.
Precompilation happened outside the samples. Every request got a fresh instance.

| Measurement | ts-rust 0.7.0 reference | Selected tsrs 0.18.0 |
| --- | ---: | ---: |
| Compiler Wasm | 18,219,437 bytes | 9,766,093 bytes |
| Native companion | 41,905,968 bytes | 20,962,008 bytes |
| Module load from Wasm | 14,954.01 ms | 7,628.79 ms |
| Module load from native companion | 83.00 ms | 69.97 ms |
| Type check after native loading | 560.77 ms | 336.33 ms |
| Native load plus type check | 639.97 ms | 403.85 ms |
| Wasm load plus type check | 15,490.23 ms | 7,972.78 ms |

These compare pinned artifacts, including their different optimization choices,
on a shared server. They do not isolate checker algorithms or predict large npm
project performance. Precompilation removes module compilation from startup; it
does not change the guest's optimization level. Raw measurements are in
[benchmarks/tsrs.json](benchmarks/tsrs.json) and
[benchmarks/ts-rust.json](benchmarks/ts-rust.json). The legacy reference module
had only equal-length Rust builder-path remapping before its measurements.

## Build and package

`toolchain.lock.json` pins the npm archive and each runtime payload by SHA-256.
`source.json` pins the source archive, Cargo.lock, and every registry crate.
The default bootstrap installs the verified upstream Wasm and license notices.

```sh
export WISP_HOSTS_DIR=/path/to/wisp/.tools/shared-hosts
source env.sh
python3 build.py build tsrs
python3 build.py test tsrs
python3 build.py package tsrs --output-dir dist/tsrs-packages
python3 build.py validate dist/tsrs-packages/tsrs.wisp.zip
python3 build.py precompile tsrs --automatic --output-dir dist/tsrs-native
```

The upstream module contains redacted `/home/.cargo` source paths and an ISO
4217 documentation URL. The package scanner approves the exact pinned upstream
module; source variants have a narrowly scoped exception for that exact public
URL text. Actual checkout and T3 project paths remain forbidden.

The normal build, test and package paths publish `.cwasm` companions through
`cwasm_profiles.py` and `cwasm.py`, under `precompiled/tsrs/`. The host checks
source and native hashes, the Wasmtime library, CPU baseline and engine settings.
A different host prepares another companion from Wasm. The package's Wisp bundle
version is `0.1.0`; its upstream version is the full 0.18.0 version above.

This is a shared checker package, so it has no application-language entry in
`toolchains.json` and does not emit Components itself. It uses external Wasmtime
Python 50.0.1. The common root build commands also need the selected Wasmtime CLI
host, as with other toolchain packages.

## Source reproduction

The source bootstrap uses the exact Rust 1.99.0 compiler and Cargo identities in
`toolchain.lock.json`, with the `wasm32-wasip1` target installed. Native tools are
used only to bootstrap this compiler package, as permitted by the repository's
stage rule. They are never called by `typecheck.py` or the application adapter.

```sh
export WISP_TSRS_BOOTSTRAP_RUSTC=/path/to/pinned-rust-1.99.0/bin/rustc
python3 tsrs/bootstrap.py --rebuild
```

The bounded source profile uses opt1, one codegen unit, no LTO, no debug info and
panic abort. It remaps builder, Cargo and Rust source paths. It skips Binaryen,
so it does not claim byte-for-byte identity with the upstream size-optimized
release. The manifest records the source profile, source pins, tool identities
and resulting module hash. Application verification must pass for either profile.
Use `--reinstall` to restore the pinned upstream artifact after a source test.
`--offline` requires the archive and Cargo dependencies to be cached.

Source bootstrap budgets 3 GiB of growth beyond the 5 GiB disk reserve. Use a
limited cgroup and one worker, and monitor the host before and during builds.
Scratch, downloads, Cargo output and native caches must be on disk. Nothing
belongs in `/tmp` or `/dev/shm` on this server.

## Application use and limits

```sh
python3 javascript/compile.py javascript/examples/typecheck/pass/add.ts \
  --wit javascript/examples/add.wit --world arithmetic --stub-wasi --typecheck \
  -o build/checked.wasm
```

Checking defaults off. A root `tsconfig.json` controls the project when present.
Otherwise the adapter checks TypeScript files under the root with strict ES2022
and bundler-resolution defaults; `node_modules` supplies dependencies rather
than additional root files. All inputs must be under `--module-root`. No-emit
checking runs before esbuild, and differing bundled/checked input hashes reject
publication. Receipts record the option, compiler and TypeScript versions,
arguments, checked hashes and elapsed time.

The host retains compiled code only. Every request gets a fresh Store, instance,
file map, environment and output buffers, with no filesystem preopens. See
[host-contract.md](host-contract.md) for the preserved Wisp request contract.
WIT still defines public Component types. Type checking does not add Node APIs
or web globals to QuickJS. Authored WIT imports need TypeScript declarations if
they are used in a checked project.

Watch, language-server and API-server modes are outside this host's contract.
Deep programs can still exceed stack or linear-memory limits. The checker is a
TypeScript port, not a claim of complete conformance on arbitrary npm projects.

## Recorded validation

The common commands above run the host protocol suite and actual passing,
failing and repeated compiler requests. The protocol suite reports 37 tests.
The invalid project returns diagnostic 2322; the next valid request succeeds.
The broader workspace checks use:

```sh
python3 -B -m unittest test-cwasm test-packages test-build
```

The same benchmark method used for the comparison is available with:

```sh
python3 tsrs/benchmark.py --repetitions 3 --output build/tsrs-benchmark.json
```

The source build finished in `4m 02s`. Its 16,174,854-byte opt1 module has
SHA-256 `dc49e6e19797bed27819d1d0ee3fe276afe7af6b8db808331e977ba987f1787c`.
It passed the actual valid/error/valid-again sequence and the builder-path scan.
[Source verification record](source-verification.json) records its tool hashes.
A later offline build reused the valid Cargo cache and finished in `0.63s`.
The pinned upstream module was restored for packaging and the comparison.

The full JavaScript verification command was:

```sh
WISP_WASMTIME="$PWD/build/verification-host.py" python3 -B javascript/verify.py \
  --output-dir "$PWD/build/javascript-tsrs-verify"
```

The local verification host reused the pinned compiler companions and disabled
JIT caching for disposable Components to bound disk growth. Output was
`21/21 checks passed`, including both type-check cases. The failing project
reported diagnostic 2322 and published no module, WIT or receipt. The verifier
blocked Node and native compiler commands throughout. The 97 workspace tests
also passed. Package validation reported `valid: true`, and installation
reported `installed: true`. Native export with `precompile --automatic` produced
a matching 20,962,008-byte companion.

The installed package also passed a direct check:

```sh
python3 build.py install dist/tsrs-packages/tsrs.wisp.zip --prefix "$PWD/build/tsrs-installed"
python3 -B build/tsrs-installed/tsrs/typecheck.py \
  --module-root "$PWD/javascript/examples/typecheck/pass" \
  --source "$PWD/javascript/examples/typecheck/pass/add.ts" \
  --output "$PWD/build/tsrs-comparison/installed-check.json"
```

The result recorded `passed: true`, `compiler: tsrs@0.18.0-ts7.1.0-dev.20260929`
and `host: Wasmtime Python 50.0.1`.

Builds and probes ran serially under explicit memory limits. Downloads, source,
Cargo output and caches stayed on ext4. Initial low disk availability deferred
heavy builds; they ran after another session freed capacity. The abandoned
Wasm-hosted ts-rust rebuild was stopped after choosing tsrs. No shared checkout
was edited.

## Proposed wisp-pkgs dependency

Recipes needing TypeScript checking should depend on `wisp:tsrs@0.1.0` rather
than rebuild a checker as an application. Keep the existing ts-rust application's
identity and upstream Node API distinct; switching its published ABI needs its
own migration. Neither shared wisp-pkgs checkout was edited.
