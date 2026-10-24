# Driverless C/C++ to WebAssembly compiler

This milestone replaces the POC's call to `clang_main` with a dedicated
`clang-wasm` entry point. The compiler driver is a WASIp3 component importing
the separately installed [LLVM shared library](../llvm/SHARED.md). It writes a
relocatable object for `wasm32-unknown-unknown`, then calls the WebAssembly LLD
library in the same process to produce a Core Wasm module with no WASI imports.

## Build and run

```sh
python3 ../build.py build clang
./verify-clang-wasm.sh
mkdir -p work
cp examples/add.cpp work/input.cpp
.toolchains/wasmtime-v49.0.1-x86_64-linux/wasmtime run \
  --dir "$PWD/work::/work" \
  .toolchains/build-p3/bin/clang-wasm.component \
  /work/input.cpp -o /work/output.wasm
.toolchains/wasmtime-v49.0.1-x86_64-linux/wasmtime run \
  --invoke add work/output.wasm 20 22
```

The source is [`src/clang_wasm_main.cpp`](src/clang_wasm_main.cpp). The build applies
[`0001-clang-wasip3-in-process.patch`](patches/0001-clang-wasip3-in-process.patch)
for WASIp3 hosting and
[`0002-driverless-clang-wasm.patch`](patches/0002-driverless-clang-wasm.patch)
for the new target. The build copies the repository source into LLVM and
refreshes it when that source changes, including on incremental builds.
The external options are one input, `-o`, `-O0`, `-O1`, and
`-I`. The source suffix `.c` selects C; other inputs select C++. The cc1
invocation fixes the target triple, emits an object, disables standard and
builtin include directories, disables RTTI and thread safe statics, and keeps
exceptions at cc1's disabled default. User code defaults to `-O0`. LLD uses
`--no-entry --export-all` so standalone exported functions can be invoked.
The generated object uses static relocation, with no `__memory_base` or
`__table_base` imports, and its target feature list has no atomics. The hosted
shared compiler component reserves a 16 MiB stack. The original static
milestone used a 1 MiB stack; the wasi-sdk default was 64 KiB;
the compiler-side `-Oz` build trapped in LLVM's recursive SelectionDAG pass
while compiling `add.cpp` with that default. Relinking with 1 MiB fixed the
trap. All driverless build levels use the same stack setting; the original POC
baseline retains its 64 KiB default.

## Architecture

| POC | Driverless component |
| --- | --- |
| `main` calls `clang_main` | `main` creates `CompilerInvocation` |
| `clang::driver::Driver` builds a compile job | `CompilerInstance::ExecuteAction(EmitObjAction)` compiles directly |
| Integrated cc1 emits a wasm object | Clang CodeGen and LLVM WebAssembly backend emit a wasm object |
| In-process wasm LLD links the object | The same in-process wasm LLD API links the object |

The component host triple is `wasm32-wasip3`; its generated object and program
use `wasm32-unknown-unknown`. The output is Core Wasm with no WASI imports.
The component needs no external Clang resource directory or application
sysroot for these freestanding tests. LLVM's WASI subprocess implementation
still returns an error for any attempt to spawn another program.
`verify-clang-wasm.sh` traces `execve` when `strace` is installed and checks
that only Wasmtime itself starts. It reports an explicit skip otherwise.

## Verification

`verify-clang-wasm.sh` checks `add(20, 22) == 42`, templates, struct
initialization, `constexpr`, C input, `-I` include lookup, invalid syntax,
exceptions disabled, rejection of target overrides, output validation, and no
WASI imports. It runs guest code at `-O1`; the benchmark runs at the default
`-O0`. Verification also requires `rg`; subprocess tracing requires `strace`.
To check another compiler variant, set `CLANG_WASM_COMPONENT`:

```sh
CLANG_WASM_COMPONENT="$PWD/.toolchains/build-p3-Os/bin/clang-wasm.component" ./verify-clang-wasm.sh
```

## Local verification, October 17, 2026

All three compiler optimization levels passed the build script using existing
build directories and checksum-verified cached archives. This was an incremental
build check, not a clean LLVM rebuild. The verifier passed for those three
components and the three existing Binaryen outputs, including all six positive
samples, the three negative checks, import validation, and one-launch subprocess
traces for each variant. Binaryen optimization was not rerun.

The [verification receipt](benchmarks/2026-10-17/verification.json) records
component hashes and the checks performed. A fresh
[`-O1` benchmark](benchmarks/2026-10-17/compiler-O1.json) exercises the default
user-code optimization, three warm runs, a cache-disabled cold run, and the
compiled `add(20, 22)` result. The historical comparison tables below keep their
original September measurements.

## Size attribution

Both targets write `wasm-ld` maps. `linked-size.py` sums the mapped code and
data bytes attributed to archive members, rather than using full `.a` sizes.
The resulting totals cover linked archive contributions; linker generated
sections and runtime objects account for the remainder. Values are bytes.

| Linked library | Original | Driverless |
| --- | ---: | ---: |
| `libclangDriver.a` | 2,528,610 | 0 |
| `libclangFrontendTool.a` | 5,648 | 0 |
| `libclangSema.a` | 10,593,245 | 10,600,219 |
| `libclangAST.a` | 9,372,978 | 8,684,686 |
| `libclangCodeGen.a` | 6,935,577 | 6,744,331 |
| `libclangBasic.a` | 3,998,590 | 4,005,558 |
| `libclangFrontend.a` | 1,669,962 | 1,102,414 |
| `libclangOptions.a` | 325,768 | 316,862 |
| `libLLVMCodeGen.a` | 5,478,508 | 5,481,001 |
| `libLLVMSelectionDAG.a` | 3,559,420 | 3,559,420 |
| `libLLVMCore.a` | 3,954,069 | 3,957,537 |
| `libLLVMAnalysis.a` | 3,544,944 | 3,459,061 |
| `libLLVMVectorize.a` | 3,351,623 | 3,352,012 |
| `libLLVMipo.a` | 2,834,787 | 2,831,479 |
| `libLLVMScalarOpts.a` | 2,615,870 | 2,569,210 |
| `libLLVMPasses.a` | 1,361,706 | 1,387,233 |
| `libLLVMWebAssemblyCodeGen.a` | 1,018,650 | 1,022,013 |
| `liblldWasm.a` | 377,396 | 379,803 |
| All attributed archive bytes | 83,874,833 | 78,199,165 |

The original map contains Clang's GCC discovery, Darwin, Linux, MSVC, MinGW,
CUDA, HIP, and other platform toolchain objects. None is present in the new
map. `libclangExtractAPI.a` also falls from 775,079 bytes to zero because the
new entry point avoids `clangFrontendTool`. The new map retains 187 bytes from
`libLLVMFrontendDriver.a(CodeGenOptions.cpp.obj)`, which is LLVM codegen
configuration, not the Clang driver. `clangOptions` remains because
`CompilerInvocation::CreateFromArgs` parses the small internal cc1 argument
list.

The largest remaining linked components are Clang Sema (10.6 MB), Clang AST
(8.7 MB), Clang CodeGen (6.7 MB), generic LLVM CodeGen (5.5 MB), and LLVM Core
(4.0 MB). The selected LLVM optimization libraries (Vectorize, IPO,
ScalarOpts, InstCombine, TransformUtils, AggressiveInstCombine, and Passes)
total 13.6 MB. This milestone does not alter AST, Sema, or LLVM's backend.
Clang's generic `TargetInfo` factory still links non-Wasm frontend target descriptions from
`libclangBasic.a`, including AArch64 (523 KB), ARM (198 KB), and RISC-V
(170 KB). Those are separate from `clangDriver` and remain a possible later
fixed-target reduction.

## Measurements

The original reports, including the first runs discussed below, are preserved
in [benchmarks/2026-09-26](benchmarks/2026-09-26). These are historical
measurements of the local artifacts, not measurements from a fresh rebuild.
The original `clang-p3` artifact is retained from the earlier POC; the current
default build produces only `clang-wasm`. `benchmark-all.sh` requires that
baseline artifact, all three compiler builds, and all three Binaryen outputs.

Measurements were taken on an x86_64 host with eight logical CPUs
(AMD Ryzen 7 2700U), using Wasmtime 49.0.1. `benchmark.py` first populates
Wasmtime's cache, takes the median of three warm launches, then runs once with
`-C cache=n` for cold startup and peak RSS. It times the compiler's ready,
codegen, and link markers, and reads host peak RSS from `/usr/bin/time`.
`COMPILER_OPT=Os` and `COMPILER_OPT=Oz` select separate build directories for
compiler-side size comparisons. These flags do not change the generated user
code's default `-O0`. Raw size is a poor guide to runtime size for these
variants: the inner module's `name` section is 26,680,389 bytes at `-O1`,
38,250,214 at `-Os`, and 65,682,506 at `-Oz`. `wasm-tools strip -a` removes
it; the stripped sizes are the deployable components compared below.

```sh
COMPILER_OPT=O1 ./build-clang-wasi.sh
COMPILER_OPT=Os ./build-clang-wasi.sh
COMPILER_OPT=Oz ./build-clang-wasi.sh
python3 benchmark.py .toolchains/build-p3/bin/clang-wasm.component \
  .toolchains/build-p3/bin/clang-wasm compiler-O1 work/benchmarks/compiler-O1.json
./benchmark-all.sh  # after all three compiler builds and wasm-opt outputs exist
```

The `wasm-opt-component.py` experiment applies each Binaryen level to the
same stripped compiler-side `-O1` component. It optimizes the largest embedded
Core module, then reassembles and validates the same WASIp3 component
wrapper. Binaryen v120 does not accept a component as direct input, so this
step is necessary. `wasm-opt` remains optional. The three outputs from the
`-O1` compiler build are 65,665,131 bytes (`wasm-opt -O1`), 61,978,351
bytes (`-Os`), and 59,657,909 bytes (`-Oz`). Each passes
`verify-clang-wasm.sh`.

For the pinned Binaryen package used here on Ubuntu x86_64:

```sh
(cd .toolchains && apt download binaryen=120-4build1)
dpkg-deb -x .toolchains/binaryen_120-4build1_amd64.deb .toolchains/binaryen-120
for level in O1 Os Oz; do
  python3 wasm-opt-component.py \
    .toolchains/build-p3/bin/clang-wasm.component \
    "-$level" "work/clang-wasm-opt-$level.component"
done
```

`BINARYEN_ROOT` can point the optimizer script at another unpacked Binaryen
installation with `bin/wasm-opt` and `lib/x86_64-linux-gnu/libbinaryen.so`.

### Original POC versus driverless `-O1`

| Metric | Original | Driverless |
| --- | ---: | ---: |
| Raw size (bytes) | 118,264,535 | 110,393,917 |
| Stripped size (bytes) | 85,651,790 | 79,819,138 |
| gzip -9 (bytes) | 24,388,092 | 23,024,624 |
| Warm startup (ms) | 726.88 | 715.84 |
| Cold startup (s) | 37.65 | 35.53 |
| Warm peak RSS (KiB) | 491,328 | 459,152 |
| Cold peak RSS (KiB) | 2,097,840 | 1,953,672 |
| Guest compile/object (ms) | 7.57 | 7.22 |
| Guest LLD (ms) | 1.12 | 1.31 |
| Guest compile + link (ms) | 8.08 | 8.52 |

The matched `-O1` build removes 5,832,652 stripped bytes (6.81%) and
1,363,468 gzip bytes. Warm peak RSS falls by 32,176 KiB, and cold peak RSS
falls by 144,168 KiB. Warm startup and guest compile/link are close enough
that this measurement does not establish a speedup from removing the
driver. The POC's stripped artifact is the original release binary (SHA-256 `c8249c0ca2ad47d100bcb332ade83e6917b81486385e7e2a37ae88241238b2cc`).
The raw POC executable in this table was relinked with map output and is
1,428 bytes larger than the pre-map raw POC; its stripped artifact is unchanged.

### Compiler-side optimization

| Metric | -O1 | -Os | -Oz |
| --- | ---: | ---: | ---: |
| Raw size (bytes) | 110,393,917 | 102,413,835 | 123,389,192 |
| Stripped size (bytes) | 79,819,138 | 60,269,231 | 53,804,825 |
| gzip -9 (bytes) | 23,024,624 | 18,261,698 | 16,512,401 |
| Warm startup (ms) | 715.84 | 651.53 | 603.69 |
| Cold startup (s) | 35.53 | 30.76 | 33.35 |
| Warm peak RSS (KiB) | 459,152 | 388,452 | 382,084 |
| Cold peak RSS (KiB) | 1,953,672 | 1,915,712 | 2,306,220 |
| Guest compile/object (ms) | 7.22 | 7.50 | 13.14 |
| Guest LLD (ms) | 1.31 | 1.40 | 1.54 |
| Guest compile + link (ms) | 8.52 | 8.90 | 14.58 |

Compiler-side `-Oz` gives the smallest stripped and compressed component,
but its guest compile/link median is about 14.6 ms versus 8.9 ms at `-Os`.
Its repeated cold run again peaked above 2.3 million KiB, higher than `-Os`.
The raw `-Oz` file is larger because its name section alone is 65,682,506
bytes; that section is removed from the runtime component.

### Binaryen on the compiler-side `-O1` component

| Metric | wasm-opt -O1 | wasm-opt -Os | wasm-opt -Oz |
| --- | ---: | ---: | ---: |
| Stripped size (bytes) | 65,665,131 | 61,978,351 | 59,657,909 |
| gzip -9 (bytes) | 20,908,884 | 20,637,703 | 20,284,350 |
| Warm startup (ms) | 650.28 | 695.27 | 623.71 |
| Cold startup (s) | 30.36 | 27.96 | 28.29 |
| Warm peak RSS (KiB) | 406,376 | 391,368 | 382,420 |
| Cold peak RSS (KiB) | 1,695,568 | 1,964,840 | 1,950,968 |
| Guest compile/object (ms) | 7.08 | 7.25 | 7.79 |
| Guest LLD (ms) | 1.14 | 1.16 | 1.19 |
| Guest compile + link (ms) | 8.15 | 8.39 | 8.98 |

These Binaryen outputs start from the same 79,819,138-byte stripped `-O1`
component. Binaryen `-Oz` reaches 59,657,909 bytes and keeps guest
compile/link near 9 ms. Binaryen `-O1` has the lowest measured cold peak RSS
at 1,695,568 KiB; Binaryen `-Oz` reached 1,950,968 KiB in its cold run. The
compiler-side `-Oz` build is smaller at 53,804,825 bytes, but compiles the
sample more slowly. `wasm-opt -Os` had intermittent warm spikes: the first set had 1,849 and 1,977 ms launches,
while a repeat yielded a 695 ms median with one 1,930 ms spike. The tables
use the repeat for that output. The individual runs and the first result
are saved under `work/benchmarks/`.

Startup is time from launching Wasmtime to the guest's `ready` marker, so it
includes CLI and component instantiation. Guest compile/object time includes
Clang argument setup and object writing; for the original POC it also includes
the driver path. Warm rows use medians of three runs per metric after one
cache-populating run. Cold rows show one `-C cache=n` run. Compiler-side
`-Oz` and `wasm-opt -Os` were each repeated to check anomalous results; their
repeat runs appear in the tables. All runs compile `add.cpp` at user-code
`-O0`, then confirm `add(20, 22) == 42`. Host peak RSS comes from GNU `time`.
The original POC uses the wasi-sdk default 64 KiB stack; all driverless
components use 1 MiB because the compiler-side `-Oz` build trapped with the
default. The stack increase adds 3–4 bytes to each stripped driverless artifact
and may affect RSS slightly.
