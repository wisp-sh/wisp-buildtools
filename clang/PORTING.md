# Source changes for the WASIp3 compiler host

The WASI adaptations are in
[`patches/0001-clang-wasip3-in-process.patch`](patches/0001-clang-wasip3-in-process.patch).
[`patches/0002-driverless-clang-wasm.patch`](patches/0002-driverless-clang-wasm.patch)
adds the driverless build target and linker maps. Both apply to the pinned LLVM
commit in the README. `build-clang-wasi.sh` applies them and copies
[`src/clang_wasm_main.cpp`](src/clang_wasm_main.cpp) into the LLVM tree before
configuring either build. It refreshes that copy when the repository source
changes. See [DRIVERLESS.md](DRIVERLESS.md) for the current compiler path.

| Source | Failure under WASI | Normal Clang path | Why this adaptation fits the Wasm-only host |
| --- | --- | --- | --- |
| `llvm/cmake/modules/HandleLLVMOptions.cmake` | LLVM rejects CMake's `WASI` system name. | Build setup, required. | Keeps Unix file status types and disables shared-library version scripts. |
| `llvm/cmake/config-ix.cmake` | Static-only CMake link probes claim `pthreads` exists; the final link then requests absent `-lpthreads`. | Threads are disabled for this component. | Skips pthread discovery on WASI, avoiding a false link dependency. |
| `llvm/include/llvm/ADT/bit.h` | The fallback header `machine/endian.h` is absent. | Required for APInt and most compiler code. | wasi-libc provides `endian.h`, as on Linux. |
| `llvm/lib/Support/CrashRecoveryContext.cpp` | WASI lacks POSIX signals; `setjmp` requires WebAssembly exception handling. | Crash diagnostics, optional for a single compile. | An unrecoverable trap or explicit exit ends this component invocation. No exception proposal or signal emulation is enabled. |
| `llvm/lib/Support/LockFileManager.cpp` | `getsid`, process checks, and hostname lookup are unavailable. | Module cache coordination; unused by the basic test. | A lock from another owner is conservatively treated as active. |
| `llvm/lib/Support/Unix/Unix.h` | `sys/wait.h` is absent. | Only subprocess support needs it. | The compiler never waits for a child. |
| `llvm/lib/Support/RandomNumberGenerator.cpp` | `/dev/urandom` is outside the workspace. | Random bytes are useful for generated names. | `getentropy` uses WASI random and needs no file access. |
| `llvm/lib/Support/CMakeLists.txt` | Unix socket address fields are absent. | Socket streams are irrelevant to this command-line compiler. | The unsupported source is omitted from this target. |
| `llvm/lib/Support/Program.cpp` and `WASI/Program.inc` | `fork`, `exec`, `posix_spawn`, and wait do not exist. | Required only when Clang launches external tools. | Every attempted subprocess returns an explicit error. The wrapper calls integrated cc1 and the wasm LLD library. |
| `llvm/lib/Support/Process.cpp` and `WASI/Process.inc` | PID, process clocks, signals, terminal ioctl, and `/dev/null` assumptions fail. | Environment lookup and stdio setup are used; PID and terminal behavior are not needed here. | Environment comes only from Wasmtime's explicit grants. Stdio remains WASI streams. Diagnostics use plain text. Random values use `getentropy`. |
| `llvm/lib/Support/Memory.cpp` and `WASI/Memory.inc` | `mmap` and `mprotect` are unavailable. | JIT mappings are unused; data buffers may request mapped memory. | Nonexecuting allocations use aligned linear memory. Executable mappings fail, since this compiler does no JIT. |
| `llvm/lib/Support/ProgramStack.cpp` | `getrlimit` for stack size is unavailable. | Clang can use the size to choose a new stack. | The existing 8 MiB default is used; the simple compile has no stack switch. |
| `llvm/lib/Support/Unix/Watchdog.inc` | `alarm` is unavailable. | Optional timeout handling. | The host can time out Wasmtime; the compiler does not set an alarm. |
| `llvm/lib/Support/Signals.cpp` and `WASI/Signals.inc` | No signal handlers can be installed. | Interrupt and crash cleanup, optional here. | A trap terminates the instance. This experiment does not rely on signal-triggered file cleanup. |
| `llvm/lib/Support/Unix/Path.inc` | User database, `umask`, `fchown`, `mmap`, and executable discovery assumptions fail. | Ordinary paths and file status are required. | Files stay inside preopened directories. `argv[0]` identifies the compiler; `HOME` is used only if granted; mmap operations report unsupported. |
| `llvm/lib/Support/MemoryBuffer.cpp` | Large files default to mmap. | Input and object reads are required. | The existing read-and-copy path works with WASI descriptors; it costs extra memory. |
| `clang/tools/driver` | The stock driver invokes `wasm-ld` as a separate program. | Codegen and linking are required. | `clang-p3` forces integrated cc1, writes an object in `/work`, then calls `lld::lldMain` with only the wasm driver. |
| `clang/tools/driver/driver.cpp` | Unix error handling includes `signal.h` and re-raises signals, which WASI does not provide. | Only needed when preserving POSIX signal exit codes. | An error returns its status directly; no signal emulation is linked. |
| `clang/lib/Driver/Driver.cpp` | The macOS crash-report lookup calls `getpid`, which WASI does not provide. | Crash report lookup, unused in normal compilation. | The WASI build skips that lookup. |

LLVM's target list contains only WebAssembly. The P3 component does not build
an X86, ARM, AArch64, RISC-V, PowerPC, or MIPS backend. Clang and LLD still
compile target-independent LLVM support and analysis libraries.
