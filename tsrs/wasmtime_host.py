"""Run upstream tsrs_wasm over a private, in-memory filesystem.

No compiler or package installation happens here. Supply Wasmtime Python 50.0.1
through WISP_WASMTIME_PYTHON or the Python import path. See host-contract.md.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import struct
import sys
import threading
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cwasm


MIB = 1024 * 1024
UINT32_MAX = 0xFFFFFFFF
SUCCESS, EBADF, ENOSYS = 0, 8, 52
WASI = "wasi_snapshot_preview1"
# Wasmtime 50's Python callback exception slot is process-global. Serialize
# our executions as well as changes to Python's process-wide thread stack size.
_RUNTIME_LOCK = threading.RLock()


class HostProtocolError(ValueError):
    """Invalid tsrs_wasm protocol or guest memory range."""


class HostRuntimeError(RuntimeError):
    """A failed run, with captured output and files in ``result``."""

    def __init__(self, message: str, result: dict[str, Any]):
        super().__init__(message)
        self.result = result


def _utf8(text: str) -> bytes:
    # Match JS TextEncoder even for lone UTF-16 surrogates in JSON input.
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace").encode("utf-8")


def _text(data: bytes, *, keep_bom: bool = False) -> str:
    return data.decode("utf-8" if keep_bom else "utf-8-sig", "replace")


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if "\0" in value:
        raise ValueError(f"{name} must not contain NUL bytes")
    return value


def _path(value: Any, name: str) -> str:
    value = _string(value, name)
    if not (value.startswith("/") or re.match(r"^[A-Za-z]:/", value)):
        raise ValueError(f"{name} must be an absolute path with / separators")
    return value


def _u32(value: int) -> int:
    """Interpret Wasmtime's signed i32 as unsigned, without arbitrary wrapping."""
    if type(value) is not int or not -(1 << 31) <= value <= UINT32_MAX:
        raise HostProtocolError("expected a wasm32 integer")
    return value & UINT32_MAX


def checked_range(size: int, pointer: int, length: int) -> tuple[int, int]:
    pointer, length = _u32(pointer), _u32(length)
    end = pointer + length
    if end > 1 << 32 or pointer > size or end > size:
        raise HostProtocolError(f"guest memory range {pointer}+{length} exceeds {size} bytes")
    return pointer, end


class MemoryFileSystem:
    """core.js memoryFileSystem semantics, extended with base64 input files.

    Directories are inferred from file keys, / always exists, and all mtimes
    stay zero. Lookup stays exact, as in upstream's Map, even when the compiler
    is told that names are case-insensitive.
    """

    def __init__(self, files: dict[str, Any]):
        if not isinstance(files, dict):
            raise ValueError("files must be an object mapping absolute paths to text or {base64: text}")
        self.files: dict[str, str | bytes] = {}
        for path, value in files.items():
            _path(path, "file path")
            if isinstance(value, str):
                self.files[path] = value
            elif isinstance(value, dict) and set(value) == {"base64"} and isinstance(value["base64"], str):
                try:
                    self.files[path] = base64.b64decode(value["base64"], validate=True)
                except (ValueError, binascii.Error) as error:
                    raise ValueError(f"invalid base64 file: {path}") from error
            else:
                raise ValueError(f"invalid file contents: {path}")

    @staticmethod
    def _prefix(path: str) -> str:
        return path if path.endswith("/") else path + "/"

    def is_directory(self, path: str) -> bool:
        return path == "/" or any(key.startswith(self._prefix(path)) for key in self.files)

    def snapshot(self) -> dict[str, Any]:
        return {path: {"base64": base64.b64encode(value).decode("ascii")} if isinstance(value, bytes)
                else value for path, value in self.files.items()}

    def call(self, op: int, request: bytes) -> bytes | str | None:
        """Bytes mean success; str is staged error text; None is bare failure."""
        path = _text(request)
        if op == 0:
            value = self.files.get(path)
            return _utf8(value) if isinstance(value, str) else value
        if op == 1:
            if path in self.files:
                value = self.files[path]
                size = len(_utf8(value) if isinstance(value, str) else value)
                return f"f {size} 0".encode()
            return b"d 0 0" if self.is_directory(path) else None
        if op == 2:
            prefix = self._prefix(path)
            entries: dict[str, str] = {}
            for key in self.files:
                if key.startswith(prefix):
                    rest = key[len(prefix):]
                    name, slash, _ = rest.partition("/")
                    entries.setdefault(name, "d" if slash else "f")
            if not entries and not self.is_directory(path):
                return None
            return _utf8("".join(kind + name + "\0" for name, kind in entries.items()))
        if op == 3:
            return _utf8(path) if path in self.files or self.is_directory(path) else None
        if op in (4, 5):
            raw_path, nul, data = request.partition(b"\0")
            if not nul:
                raise HostProtocolError("write request is missing its path separator")
            path = _text(raw_path)
            if self.is_directory(path):
                return f"open {path}: is a directory"
            for slash in (i for i, c in enumerate(path) if c == "/"):
                parent = path[:slash]
                if parent in self.files:
                    return f"mkdir {parent}: not a directory"
            previous = self.files.get(path, "") if op == 5 else ""
            # Preserve the original bytes when appending to an explicit binary
            # file. Ordinary emitted files follow upstream fileDecoder exactly.
            self.files[path] = previous + data if isinstance(previous, bytes) else previous + _text(data, keep_bom=True)
            return b""
        if op == 6:
            prefix = self._prefix(path)
            for key in list(self.files):
                if key == path or key.startswith(prefix):
                    del self.files[key]
            return b""
        if op == 7:
            # Upstream's memory filesystem has no chtimes method, so core.js
            # acknowledges this even for a missing path and keeps zero mtimes.
            return b""
        return None


def _prepare(request: dict[str, Any]) -> tuple[dict[str, Any], bytes, MemoryFileSystem]:
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object")
    cwd = _path(request.get("cwd", "/"), "cwd")
    args = request.get("args", [])
    if not isinstance(args, list):
        raise ValueError("args must be a list of strings")
    args = [_string(arg, "argument") for arg in args]
    diagnostics = request.get("diagnostics", "text")
    if diagnostics not in ("text", "json"):
        raise ValueError("diagnostics must be 'text' or 'json'")
    case_sensitive = request.get("caseSensitive", True)
    if type(case_sensitive) is not bool:
        raise ValueError("caseSensitive must be boolean")
    env = request.get("env", {})
    if not isinstance(env, dict):
        raise ValueError("env must be an object")
    env_bytes = []
    for key, value in env.items():
        _string(key, "environment name")
        if value is not None:
            env_bytes.append(_utf8(f"{key}={_string(value, 'environment value')}\0"))
    fs = MemoryFileSystem(request.get("files", {}))
    flags = (1 if diagnostics == "json" else 0) | (0 if case_sensitive else 2)
    payload = _utf8("\0".join([cwd, str(flags), *args]))
    if len(payload) > 0x7FFFFFFF:
        raise ValueError("request is too large for tsrs_wasm")
    return {"diagnostics": diagnostics, "env": env_bytes}, payload, fs


class _RunState:
    def __init__(self, options: dict[str, Any], fs: MemoryFileSystem):
        self.options, self.fs = options, fs
        self.staged: bytes | None = None
        self.stdout, self.stderr = bytearray(), bytearray()

    @staticmethod
    def memory(caller: Any) -> Any:
        memory = caller.get("memory")
        if memory is None:
            raise HostProtocolError("guest does not export memory")
        return memory

    @staticmethod
    def read(memory: Any, store: Any, ptr: int, length: int) -> bytes:
        start, end = checked_range(memory.data_len(store), ptr, length)
        return bytes(memory.read(store, start, end))

    @staticmethod
    def write(memory: Any, store: Any, ptr: int, data: bytes) -> None:
        start, _ = checked_range(memory.data_len(store), ptr, len(data))
        if data:
            memory.write(store, data, start)

    def fs_call(self, caller: Any, op: int, ptr: int, length: int) -> int:
        self.staged = None
        result = self.fs.call(_u32(op), self.read(self.memory(caller), caller, ptr, length))
        if result is None:
            return -1
        self.staged = _utf8(result) if isinstance(result, str) else result
        if len(self.staged) > 0x7FFFFFFD:
            raise HostProtocolError("filesystem response exceeds the signed ABI length")
        return -2 - len(self.staged) if isinstance(result, str) else len(self.staged)

    def fs_take(self, caller: Any, ptr: int) -> None:
        if self.staged is None:
            raise HostProtocolError("fs_take has no staged response")
        self.write(self.memory(caller), caller, ptr, self.staged)
        self.staged = None

    def wasi(self, name: str, caller: Any, *args: int) -> int:
        if name == "fd_prestat_get":
            return EBADF
        if name == "sched_yield":
            return SUCCESS
        if name not in WASI_SIGNATURES:
            return ENOSYS
        memory = self.memory(caller)
        write = lambda ptr, data: self.write(memory, caller, ptr, data)
        if name in ("args_sizes_get", "environ_sizes_get"):
            strings = [] if name == "args_sizes_get" else self.options["env"]
            write(args[0], struct.pack("<I", len(strings)))
            write(args[1], struct.pack("<I", sum(map(len, strings))))
        elif name == "environ_get":
            pointers, buf = map(_u32, args)
            strings = self.options["env"]
            checked_range(memory.data_len(caller), pointers, 4 * len(strings))
            checked_range(memory.data_len(caller), buf, sum(map(len, strings)))
            for index, value in enumerate(strings):
                write(pointers + 4 * index, struct.pack("<I", buf))
                write(buf, value)
                buf += len(value)
        elif name == "clock_time_get":
            ns = time.time_ns() if args[0] == 0 else time.monotonic_ns()
            write(args[2], struct.pack("<Q", ns))
        elif name == "clock_res_get":
            write(args[1], struct.pack("<Q", 1000))
        elif name == "random_get":
            ptr, end = checked_range(memory.data_len(caller), *args)
            for at in range(ptr, end, 65536):
                write(at, os.urandom(min(65536, end - at)))
        elif name == "fd_write":
            fd, iovs, count, nwritten = map(_u32, args)
            if fd not in (1, 2):
                return EBADF
            table = self.read(memory, caller, iovs, count * 8)
            checked_range(memory.data_len(caller), nwritten, 4)
            chunks = [self.read(memory, caller, *struct.unpack_from("<II", table, offset))
                      for offset in range(0, len(table), 8)]
            size = sum(map(len, chunks))
            if size > UINT32_MAX:
                raise HostProtocolError("fd_write byte count exceeds wasm32")
            output = self.stdout if fd == 1 else self.stderr
            for chunk in chunks:
                output.extend(chunk)
            write(nwritten, struct.pack("<I", size))
        elif name == "fd_fdstat_get":
            if _u32(args[0]) > 2:
                return EBADF
            write(args[1], bytes(24))
        elif name == "poll_oneoff":
            write(args[3], struct.pack("<I", 0))
        # args_get has no arguments to copy, as in core.js.
        return SUCCESS

    def result(self, code: int, diagnostics: Any = None) -> dict[str, Any]:
        return {"exitCode": code, "stdout": _text(bytes(self.stdout), keep_bom=True),
                "stderr": _text(bytes(self.stderr), keep_bom=True), "diagnostics": diagnostics,
                "files": self.fs.snapshot()}


WASI_SIGNATURES = {
    "args_sizes_get": (["i32", "i32"], ["i32"]),
    "args_get": (["i32", "i32"], ["i32"]),
    "environ_sizes_get": (["i32", "i32"], ["i32"]),
    "environ_get": (["i32", "i32"], ["i32"]),
    "clock_time_get": (["i32", "i64", "i32"], ["i32"]),
    "clock_res_get": (["i32", "i32"], ["i32"]),
    "random_get": (["i32", "i32"], ["i32"]),
    "fd_write": (["i32", "i32", "i32", "i32"], ["i32"]),
    "fd_fdstat_get": (["i32", "i32"], ["i32"]),
    "fd_prestat_get": (["i32", "i32"], ["i32"]),
    "proc_exit": (["i32"], []),
    "sched_yield": ([], ["i32"]),
    "poll_oneoff": (["i32", "i32", "i32", "i32"], ["i32"]),
}
EXPORT_SIGNATURES = {"tsrs_input": (["i32"], ["i32"]), "tsrs_run": ([], ["i32"]),
                     "tsrs_output": ([], ["i32"]), "tsrs_output_len": ([], ["i32"])}
HOST_SIGNATURES = {"fs": (["i32", "i32", "i32"], ["i32"]), "fs_take": (["i32"], [])}


def _wasmtime() -> Any:
    location = os.environ.get("WISP_WASMTIME_PYTHON")
    if location and str(Path(location).resolve()) not in sys.path:
        sys.path.insert(0, str(Path(location).resolve()))
    try:
        wt = importlib.import_module("wasmtime")
        version = importlib.metadata.version("wasmtime")
    except (ImportError, importlib.metadata.PackageNotFoundError) as error:
        raise RuntimeError("supply Wasmtime Python 50.0.1 via WISP_WASMTIME_PYTHON or PYTHONPATH") from error
    if version != "50.0.1":
        raise RuntimeError(f"Wasmtime Python 50.0.1 required, found {version}")
    if location and not Path(wt.__file__).resolve().is_relative_to(Path(location).resolve()):
        raise RuntimeError("wasmtime was already imported from outside WISP_WASMTIME_PYTHON")
    return wt


def _signature(wt: Any, ty: Any) -> tuple[list[str], list[str]] | None:
    return ([str(t) for t in ty.params], [str(t) for t in ty.results]) if isinstance(ty, wt.FuncType) else None


def compilation_engine(wt=None, *, wasm_stack_bytes=128 * MIB, python_stack_bytes=256 * MIB):
    wt = wt or _wasmtime()
    config = wt.Config()
    config.strategy = "cranelift"
    config.target = cwasm.host_target()
    config.cranelift_opt_level = "none"
    config.parallel_compilation = False
    config.max_wasm_stack = wasm_stack_bytes
    from wasmtime import _bindings
    _bindings.wasmtime_config_async_stack_size_set(config.ptr(), python_stack_bytes)
    engine = wt.Engine(config)
    config.close()
    return engine


def load_module(wt, engine, source, *, artifact_only=False,
                wasm_stack_bytes=128 * MIB, python_stack_bytes=256 * MIB):
    return cwasm.load_module(wt, engine, source, artifact_only=artifact_only,
        configuration={"host": "tsrs", "target": cwasm.host_target(),
                       "loaderSha256": cwasm.digest(__file__), "optLevel": "none",
                       "parallelCompilation": False, "wasmStackBytes": wasm_stack_bytes,
                       "pythonStackBytes": python_stack_bytes})


class CompiledEngine:
    """One compiled module, checked native cache and no retained request instances."""

    def __init__(self, module: Path | str | bytes, *, wasm_stack_bytes: int,
                 python_stack_bytes: int, max_memory_bytes: int, precompiled: bool):
        if not 64 * 1024 <= wasm_stack_bytes <= 256 * MIB:
            raise ValueError("wasm_stack_bytes must be between 64 KiB and 256 MiB")
        if not wasm_stack_bytes + 2 * MIB <= python_stack_bytes <= 512 * MIB:
            raise ValueError("python_stack_bytes must exceed wasm_stack_bytes by 2 MiB and be at most 512 MiB")
        if not 65536 <= max_memory_bytes <= 1 << 32:
            raise ValueError("max_memory_bytes must be between 64 KiB and 4 GiB")
        self.wt = wt = _wasmtime()
        self.python_stack_bytes, self.max_memory_bytes = python_stack_bytes, max_memory_bytes
        self.closed = False
        self.engine = compilation_engine(wt, wasm_stack_bytes=wasm_stack_bytes,
                                         python_stack_bytes=python_stack_bytes)
        self.module = None
        try:
            if isinstance(module, bytes):
                self.module = wt.Module(self.engine, module)
            elif precompiled:
                self.module = load_module(wt, self.engine, module, wasm_stack_bytes=wasm_stack_bytes,
                                          python_stack_bytes=python_stack_bytes)
            else:
                self.module = wt.Module.from_file(self.engine, module)
            exports = {item.name: item.type for item in self.module.exports}
            memory = exports.get("memory")
            if not isinstance(memory, wt.MemoryType) or memory.is_64 or memory.is_shared:
                raise HostProtocolError("module must export unshared wasm32 memory")
            for name, signature in EXPORT_SIGNATURES.items():
                if _signature(wt, exports.get(name)) != signature:
                    raise HostProtocolError(f"wrong or missing export: {name}")
            if "_initialize" in exports and _signature(wt, exports["_initialize"]) != ([], []):
                raise HostProtocolError("wrong reactor initializer signature")
            for item in self.module.imports:
                signature = _signature(wt, item.type)
                if item.module == "tsrs_host":
                    if item.name not in HOST_SIGNATURES or signature != HOST_SIGNATURES[item.name]:
                        raise HostProtocolError(f"unsupported tsrs_host import: {item.name}")
                elif item.module == WASI:
                    if signature is None or (item.name in WASI_SIGNATURES and signature != WASI_SIGNATURES[item.name]):
                        raise HostProtocolError(f"wrong WASI function signature: {item.name}")
                    if item.name not in WASI_SIGNATURES and signature[1] != ["i32"]:
                        raise HostProtocolError(f"unsupported WASI result type: {item.name}")
                else:
                    raise HostProtocolError(f"unsupported import: {item.module}.{item.name}")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with _RUNTIME_LOCK:
            if not self.closed:
                self.closed = True
                if self.module is not None:
                    self.module.close()
                self.engine.close()

    def __enter__(self) -> CompiledEngine:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _run(self, state: _RunState, payload: bytes) -> dict[str, Any]:
        wt = self.wt
        with wt.Store(self.engine) as store, wt.Linker(self.engine) as linker:
            store.set_limits(memory_size=self.max_memory_bytes, memories=1, instances=1)
            wasi = wt.WasiConfig()
            wasi.argv, wasi.env = [], []
            store.set_wasi(wasi)
            # Retain real WASI proc_exit so Python receives an ExitTrap. Every
            # other imported WASI function is shadowed by core.js semantics.
            linker.define_wasi()
            linker.allow_shadowing = True
            defined = set()
            for item in self.module.imports:
                key = (item.module, item.name)
                if key in defined:
                    continue
                defined.add(key)
                if item.module == "tsrs_host":
                    callback = state.fs_call if item.name == "fs" else state.fs_take
                elif item.name == "proc_exit":
                    continue
                else:
                    callback = lambda caller, *args, name=item.name: state.wasi(name, caller, *args)
                linker.define_func(item.module, item.name, item.type, callback, access_caller=True)
            instance = linker.instantiate(store, self.module)
            exports = instance.exports(store)
            memory = exports["memory"]
            # Upstream tsrs initializes the reactor before writing its request.
            # Never call the command _start export.
            if "_initialize" in exports:
                exports["_initialize"](store)
            pointer = exports["tsrs_input"](store, len(payload))
            state.write(memory, store, pointer, payload)
            try:
                code = exports["tsrs_run"](store)
            except wt.ExitTrap as error:
                code = error.code & UINT32_MAX
            diagnostics = None
            if state.options["diagnostics"] == "json":
                ptr = exports["tsrs_output"](store)
                length = exports["tsrs_output_len"](store)
                data = state.read(memory, store, ptr, length)
                diagnostics = json.loads(_text(data)) if data else []
                if not isinstance(diagnostics, list):
                    raise HostProtocolError("diagnostic response must be a JSON array")
            return state.result(code, diagnostics)


def compileEngine(module: Path | str | bytes, *, wasm_stack_bytes: int = 128 * MIB,
                  python_stack_bytes: int = 256 * MIB,
                  max_memory_bytes: int = 2 * 1024 * MIB, precompiled: bool = True) -> CompiledEngine:
    """Compile once with serial Cranelift opt0. The returned handle caches one module."""
    with _RUNTIME_LOCK:
        return CompiledEngine(module, wasm_stack_bytes=wasm_stack_bytes,
                              python_stack_bytes=python_stack_bytes, max_memory_bytes=max_memory_bytes,
                              precompiled=precompiled)


def runRequest(compiled: CompiledEngine, request: dict[str, Any]) -> dict[str, Any]:
    """Run on a new large-stack thread, Store, instance and file map.

    Validation raises ValueError. Runtime failures raise HostRuntimeError with
    partial output in error.result, always with exitCode 70. A compiler exit
    status, including WASI proc_exit, is returned unchanged.
    """
    options, payload, fs = _prepare(request)
    state = _RunState(options, fs)
    outcome: list[dict[str, Any] | HostRuntimeError] = []

    def worker() -> None:
        try:
            outcome.append(compiled._run(state, payload))
        except BaseException as error:
            outcome.append(HostRuntimeError(str(error), state.result(70)))

    with _RUNTIME_LOCK:
        if compiled.closed:
            raise ValueError("compiled engine is closed")
        previous_stack = threading.stack_size()
        try:
            threading.stack_size(compiled.python_stack_bytes)
            thread = threading.Thread(target=worker, name="ts-wasm-request")
            thread.start()
        finally:
            threading.stack_size(previous_stack)
        thread.join()
    result = outcome[0]
    if isinstance(result, HostRuntimeError):
        raise result
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        # Validate caller inputs before spending time compiling the module.
        _prepare(request)
        with compileEngine(args.module) as compiled:
            result = runRequest(compiled, request)
    except Exception as error:
        result = error.result if isinstance(error, HostRuntimeError) else {
            "exitCode": 70, "stdout": "", "stderr": "", "diagnostics": None, "files": {}}
        result["error"] = str(error)
    output = json.dumps(result, ensure_ascii=True) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    code = result["exitCode"]
    return code if 0 <= code <= 255 else 1


if __name__ == "__main__":
    raise SystemExit(main())
