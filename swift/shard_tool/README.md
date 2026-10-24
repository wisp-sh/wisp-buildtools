# Swift compiler artifact shards

This native Rust bootstrap transforms an existing compiler `.wasm` image. It
does not compile application source. Dependencies are pinned in Cargo.lock and
built offline with one Cargo job, no debug information, and no incremental cache.
The transformer scans instruction bytes and only replaces direct calls whose
defined-function index changes. It holds the input bytes, compact module
metadata, and one output shard, without a whole-module instruction IR.

Prepare images explicitly before entering application compiler guards:

```sh
python3 /home/luap/wisp-buildtools/swift/shard.py \
  --input /home/luap/wisp-buildtools/llvm/dist/swift-shared/swift-frontend-llvm.so.wasm \
  --out /home/luap/.t3/projects/wisp-pkgs/.cache/swift-small/frontend \
  --shards 8 --name swift-frontend
```

The CLI takes `/home/luap/.t3/projects/wisp-pkgs/.cache/compiler-memory.lock`.
Override the path with `WISP_COMPILER_MEMORY_LOCK` if needed. If a parent already
holds that flock, pass `--lock-held` or `ensure(..., lock_held=True)`. Taking a
second flock through another descriptor can deadlock. Direct invocations of the
Rust executable require the caller to hold the common lock. Keep the lock while
doing heavy Wasmtime validation/JIT work too. Scratch stays beside the selected
output, never in `/tmp`. Installed distributions are never changed.

Application runtime code must only use the read-only verification path:

```python
from shard import verify
manifest = verify(source_path, cache_directory, shards=8, name="swift-frontend")
```

`verify` reads and hashes files. It requires canonical distinct artifact names
and paths, contiguous function ranges covering the declared total, matching
per-shard counts, exact load order, coordinator global names, fixed dispatch
table ABI and matching byte totals. It never starts Cargo, transforms images, takes
the memory lock, or loads Wasmtime. An optional `tool_sha256=` requires an approved
bootstrap binary hash. The parent must still verify its pinned upstream source
and provider inventories. `ensure` is an explicit preparation API and must not be
called by guarded application compilation.

Outputs are immutable. An existing directory is checked, never rebuilt in
place. The Rust tool writes to a sibling pending directory, validates every
module with wasmparser without JIT, fsyncs each file, then publishes the directory
with a rename. Validation or transformation failure removes pending artifacts
and leaves the installed source alone. Choose another output directory when
changing the tool, source, shard count, or name. A killed process can leave a
hidden pending directory, which is never treated as a completed cache.

## Loading contract

Read `manifest.json`. Its `sourceSha256` is the exact original backend hash;
`toolSha256` identifies the native transformer. Each `state`/`shards` record gives
an absolute `path`, relative `file`, byte size, SHA256 and exported names. Shard
records also give the original defined-function ordinal range, with an exclusive
end. `totalModuleBytes` includes state and every shard and can exceed source size.
The dispatch table is separate from all original tables.

1. Instantiate the original primary, and register its exports under the original
   import namespace. Resolve every other original import exactly as before.
2. Allocate one table according to `dispatchTable`, initially null, with the
   exact reported minimum and maximum. Define it under the reported module/name.
   For the current frontend these are `wisp-swift-shards` / `functions`, a
   non-shared 32-bit `funcref` table with minimum and maximum 146227. The host
   must not expose or modify this table.
3. Instantiate `state.path` and register its exports under `stateNamespace`,
   currently `wisp-swift-state`. It defines the original globals once. Shards
   import these under `globalExports`, currently `__wisp_global_0` and so on.
   Imported and defined global indices remain in their original order. The
   coordinator also owns any original non-function exports and imported-function
   exports. `state.exports` lists those plus generated global names and memory.
4. Instantiate every `shards[].path` in the manifest order. Each shard fills only
   its own dispatch slots and its owned original active element entries. If the
   original backend exports defined functions, their owning shard exports them;
   use the per-module `exports` records to resolve that backend namespace. The
   current Swift backend only exports `memory`, so it needs no exported-function
   registration. Every shard exports the exact original imported `memory` 0 for
   WASI's immediate-caller memory lookup.
5. Keep state and all shards alive, then invoke the original entry/start driver.
   No guest function may run before all shards are instantiated. There is no
   generated start function. `loadOrder` records primary, state and shard order.

The original imports and type indices stay unchanged in every module. A shard
appends the dispatch table after original table imports, then appends state
globals after original global imports. Owned direct calls use remapped local
indices. Cross-shard `call` and `return_call` become the original callee ordinal
followed by `call_indirect` or `return_call_indirect` with the original function
type and dedicated dispatch table. Original indirect calls still use their
original table. Function bodies, globals, memory accesses, exception tags and
other instructions keep their bytes unless a direct-call index needs rewriting.
Each original defined function is emitted exactly once.

The original active element segment is divided into non-overlapping owned runs.
Imported-function entries belong to the coordinator. Defined-function entries
belong to their shard. No temporary null initializer overwrites another shard's
slot. Distinct original imported table indices may alias the same host table,
including imports with different names. The tool therefore rejects slot overlap
across all imported table indices. Reordering the remaining non-overlapping runs
preserves final table contents. Hosts must
provide the same original table capacities before instantiation.

## Supported input and explicit rejections

Supported inputs are validated core modules with ordinary function types,
original function/table/memory/global/tag imports, defined functions and globals,
and optional exports and active function-list element segments. Active offsets
must be a single `i32.const`. Both implicit table zero and explicit table-index
element encodings are accepted. Original element ranges must not overlap across
any imported tables because those imports may alias. Global initializer bytes
are preserved and validated in the coordinator.
Mutability and imported-global initializer references are preserved.

The tool rejects GC/recursive/continuation types, exact-function imports,
defined tables/memories/tags, data/data-count/start sections, passive or declared
elements, expression/null element entries, table64/shared table imports,
nonconstant or extended element offsets, overlapping element ranges across any
imported table indices, global or
body `ref.func`, and `table.init`/`elem.drop`. It rejects reserved generated import
namespaces and export-name collisions. This is deliberate fail-closed behavior,
not a claim to transform arbitrary Wasm modules. `ref.func` cannot safely become
a nullable `table.get` when the original expression has a nonnullable typed
reference. Original metadata custom sections are dropped and listed in the
manifest; the pinned input is already stripped.

## Verification

Run `python3 shard_tool/test_shard.py` for small Wasmtime 49 differential fixtures.
It holds the common lock and compares source/shards with 1, 2, 3 and 7 shards for
mutual recursion, cross-call and multivalue results, mutable shared globals,
imported-global initializers, memory writes, original function-pointer callback
slots and external function imports. Explicit table-zero element encoding and
signed LEB offset 130 have executable differential checks. Read-only verification
runs with process creation forbidden and rejects a modified artifact and modified
manifest metadata, including duplicate parts and gaps/overlaps in function ranges.
Two alias fixtures bind one host table to two imports under distinct or identical
names. Their source writes callback b then callback a and returns a; transformation
must reject that overlap before publication. A
cross-shard tail-call returns 42. Rejection fixtures check unsupported body/global
function references, start, overlapping elements, global element offsets and
element-index instructions and require that no completed/pending output survives.
Results are recorded under `fixture-cache/results.json`. Real compiler-image
transformation validates modules sequentially without compiling them. Compiler
warmup and actual Swift application compilation belong to the parent runtime.

## Measured frontend result

The pinned frontend backend hash
`d0c372c91cf1fb4affa9c5af136af4a91c18fdf9785e5036bc723c408efbad5d`
transformed successfully into eight shards in
`/home/luap/.t3/projects/wisp-pkgs/.cache/swift-small/frontend`. Source size was
84,550,193 bytes. State plus shards total 95,132,364 bytes, 12.52% larger. Each
shard contains 18,278 or 18,279 defined functions. The largest is 16,807,404 bytes;
state is 992,370 bytes. The tool rewrote 776,290 cross-shard calls.

The explicit preparation process reached 132,284 KiB peak RSS and used 104.79 CPU
seconds. Wall time was 141.29 seconds including the shared-lock wait. The initial
offline Cargo build reached 435,948 KiB peak RSS. These numbers measure artifact
transformation and validation, not real compiler JIT/execution. Raw timing and
manifest records are in `evidence/frontend-transform.*`.

An optional attempt on `wasm-ld-llvm.so.wasm` rejected its defined table section
before writing outputs. That image has one defined `funcref` table of exactly
22,285 elements; supporting it requires coordinator-owned table definitions and
shard imports of those tables while retaining original indices. The current tool
does not claim to transform that linker image. The original frontend/linker
distributions remain unchanged.

The reviewed tool additionally rejects imported-table alias overlaps and the
pure verifier checks manifest structure before artifact hashes. Its 29 fixture
records are in `evidence/reviewed-fixtures.json`. An explicit preparation with
that tool is published separately at
`/home/luap/.t3/projects/wisp-pkgs/.cache/swift-small/frontend-reviewed`, with
tool SHA256 `b06d73440c6c74276feaa7f71e1eaea69feb96e13dc2e6e05b48233ae2432405`.
It reached 132,348 KiB peak RSS, used 106.74 CPU seconds and took 185.53 seconds
including the shared-lock wait. The coordinator and all eight shards are byte
for byte identical to the earlier cache, with equal per-file sizes and SHA256
hashes. `evidence/frontend-reviewed-comparison.json` records that comparison and
the unchanged original manifest hash. The old cache retains its prior tool hash
receipt and remains valid when explicitly verified against that prior hash.
The reviewed preparation did not JIT any real compiler module.
