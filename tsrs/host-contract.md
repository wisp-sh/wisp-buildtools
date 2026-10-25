The Python host executes the upstream `tsrs_wasm` core module. It does not
compile Rust, install dependencies, or change compiler diagnostics. Wasmtime
Python 50.0.1 must already be available through `WISP_WASMTIME_PYTHON` or
`PYTHONPATH`.

```python
from wasmtime_host import compileEngine, runRequest

with compileEngine("tsrs/dist/tsrs.wasm") as compiled:
    result = runRequest(compiled, {
        "cwd": "/p",
        "args": ["-p", "."],
        "files": {
            "/p/tsconfig.json": '{"compilerOptions":{"strict":true,"outDir":"out"},"files":["a.ts"]}',
            "/p/a.ts": "export const value: number = 'text';\n",
        },
        "diagnostics": "json",
        "caseSensitive": True,
        "env": {},
    })
```

`compileEngine` retains one compiled module and its Engine. The shared checked
`.cwasm` disk cache retains native code, keyed by the source and host identity.
It retains no request instances. Each `runRequest` copies the input file map and
creates a new thread, Store, instance, filesystem staging buffer, and output
buffers. A compiled handle can serve repeated requests, which run serially
through this host. Close it after use. The host does not acquire the workspace
compiler lock; the build or test caller owns that queue.

The request defaults are `cwd: "/"`, `args: []`, `files: {}`,
`diagnostics: "text"`, `caseSensitive: true`, and `env: {}`. Paths must be
absolute and use `/` separators. NUL bytes in cwd, arguments, environment
names/values, or file paths are rejected. Environment values are strings;
`null` unsets a name. No process environment, stdio descriptors, arguments,
network access, or WASI filesystem preopens are inherited.

File values are strings or `{ "base64": "..." }` for raw bytes, including
UTF-16 input files. Base64 is validated. The result always contains
`exitCode`, `stdout`, `stderr`, `diagnostics`, and `files`. Text mode returns
`diagnostics: null`; JSON mode returns the upstream diagnostic array without
rewriting messages or positions. All files, including unchanged input files
and emitted files, appear in a fresh result map. Byte-valued files use the
same base64 representation. Writes to ordinary files decode UTF-8 with
replacement and preserve a leading BOM, as upstream's in-memory host does.
Appending to an explicit binary file preserves its bytes.

The filesystem preserves the previous Wisp checker host's isolated map contract: directories
come from file paths, `/` exists even in an empty map, missing parent
directories appear when a file is written, removal is recursive, and mtimes
stay zero. `chtimes` succeeds without changing them. Write errors retain the
upstream `open PATH: is a directory` and `mkdir PATH: not a directory` text.
Lookup uses exact map keys. `caseSensitive: false` sets the upstream compiler
flag; it does not add case folding to the map. This host has no symlink or
real-filesystem backend. Its realpath operation returns the existing map
path, as the previous Wisp checker host does.

The host implements `tsrs_host.fs` and `tsrs_host.fs_take`, and supplies a captured
preview1 shim for environment, clocks, random bytes, and captured stdout and
stderr. Other preview1 calls return ENOSYS; preopen discovery returns EBADF.
Only `proc_exit` uses the real WASI implementation, so Wasmtime's `ExitTrap`
retains the exit status. Initialization follows tsrs's instantiate, optional `_initialize`, `tsrs_input`,
`tsrs_run` sequence. The command `_start` is never called. A reactor initializer
must have type `() -> ()` and runs once in each fresh instance.
Guest pointers and lengths are checked as unsigned wasm32 ranges before
reading or writing. Filesystem callbacks never invoke guest allocators.

Cranelift uses opt0 and serial compilation. Defaults are a 128 MiB Wasm
execution stack, a 256 MiB Python worker stack, and a 2 GiB linear-memory
limit. The Wasmtime 50 C API sets the corresponding async-stack configuration
limit because the Python Config wrapper lacks that setter; execution stays
synchronous. Callers can supply `wasm_stack_bytes`, `python_stack_bytes`, and
`max_memory_bytes` to `compileEngine`. `precompiled=False` bypasses native
companions and compiles the Wasm directly for comparisons. These limits are separate from the
module's linked shadow stack. They do not establish support for every project
depth or memory requirement, and there is no wall-time or fuel limit.

Invalid input raises `ValueError`. Ordinary traps, bad guest ranges, invalid
diagnostic JSON, and other runtime failures raise `HostRuntimeError`. Its
`result` retains partial stdout, stderr, and files with `exitCode: 70` and
`diagnostics: null`. Compiler return statuses and `proc_exit` statuses are
returned unchanged. A trap is never reported as a successful compilation.

```sh
python3 -B tsrs/wasmtime_host.py \
  --module tsrs/dist/tsrs.wasm \
  --request request.json --output result.json
```

Without `--output`, the CLI writes one JSON object to stdout. It exits with
the compiler's status when that status fits a process exit byte; larger
statuses remain in JSON and cause process exit 1. Host errors include an
`error` string and exit 70. Output directories must already exist.

`python3 -B tsrs/test_wasmtime_host.py` tests the protocol and
small synthetic Wasm modules. The synthetic-JIT test group takes
`build/tsrs/compiler-memory.lock` nonblocking and skips if the parent owns it.
These tests never load the compiler artifact and do not establish actual
TypeScript checking, feature compatibility, or upstream acceptance results.

This is a custom tsrs filesystem host. It keeps the prior Wisp host's strict
directory-write errors, exact map lookup and full-file result snapshots. The
upstream JS `memoryFileSystem` additionally normalizes paths and supports case
folding; those behaviors are not claimed by this compatibility host. Default
application checks use case-sensitive absolute project paths.
