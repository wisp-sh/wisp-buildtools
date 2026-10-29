"""Protocol tests and tiny WAT fixtures; never load a real compiler module."""
from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import wasmtime_host as host


class FakeMemory:
    def __init__(self, size=256):
        self.data = bytearray(size)

    def data_len(self, store):
        return len(self.data)

    def read(self, store, start, end):
        return self.data[start:end]

    def write(self, store, data, start):
        self.data[start:start + len(data)] = data


class FakeCaller:
    def __init__(self, size=256):
        self.mem = FakeMemory(size)

    def get(self, name):
        return self.mem if name == "memory" else None


class InputTests(unittest.TestCase):
    def test_payload_defaults_flags_and_empty_arguments(self):
        _, payload, _ = host._prepare({})
        self.assertEqual(payload, b"/\x000")
        request = {"cwd": "/p", "args": ["-p", ".", ""], "diagnostics": "json", "caseSensitive": False}
        _, payload, _ = host._prepare(request)
        self.assertEqual(payload, b"/p\x003\x00-p\x00.\x00")

    def test_nul_injection_rejected(self):
        for request in ({"cwd": "/p\0x"}, {"args": ["a.ts\0--outDir"]},
                        {"env": {"A\0B": None}}, {"env": {"A": "v\0B=x"}},
                        {"files": {"/a\0b": "x"}}):
            with self.subTest(request=request), self.assertRaisesRegex(ValueError, "NUL"):
                host._prepare(request)

    def test_invalid_shape_rejected(self):
        for request in ([], {"cwd": "relative"}, {"args": "-p"}, {"args": [4]},
                        {"env": []}, {"env": {"A": 4}}, {"caseSensitive": "false"},
                        {"diagnostics": "pretty"}, {"files": []}, {"files": {"a.ts": "x"}},
                        {"files": {"/a": {"base64": "!"}}}, {"files": {"/a": {"other": ""}}}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                host._prepare(request)

    def test_environment_is_explicit_and_null_unsets(self):
        with patch.dict(os.environ, {"PRIVATE_TEST_ENV": "secret"}):
            options, _, _ = host._prepare({"env": {"X": "", "Y": "a=b", "Z": None}})
        self.assertEqual(options["env"], [b"X=\0", b"Y=a=b\0"])

    def test_js_utf8_encoding(self):
        self.assertEqual(host._utf8("\ud800"), b"\xef\xbf\xbd")
        self.assertEqual(host._utf8("\ud83d\ude00"), "😀".encode())


class FileSystemTests(unittest.TestCase):
    def test_reads_utf8_and_binary_without_decoding(self):
        raw = b"\xff\xfeA\0\xa9\x03"
        fs = host.MemoryFileSystem({"/p/a": "é", "/p/b": {"base64": base64.b64encode(raw).decode()}})
        self.assertEqual(fs.call(0, b"/p/a"), b"\xc3\xa9")
        self.assertEqual(fs.call(0, b"/p/b"), raw)
        self.assertEqual(fs.call(1, b"/p/a"), b"f 2 0")
        self.assertEqual(fs.call(1, b"/p/b"), b"f 6 0")
        self.assertIsNone(fs.call(0, b"/missing"))

    def test_inferred_directories_listing_and_realpath(self):
        fs = host.MemoryFileSystem({"/p/z.ts": "", "/p/sub/b.ts": "", "/p/sub/a.ts": "", "/q.ts": ""})
        self.assertEqual(fs.call(2, b"/p"), b"fz.ts\0dsub\0")
        self.assertEqual(fs.call(2, b"/p/"), b"fz.ts\0dsub\0")
        self.assertEqual(fs.call(1, b"/p/sub"), b"d 0 0")
        self.assertEqual(fs.call(3, b"/p/sub"), b"/p/sub")
        self.assertIsNone(fs.call(2, b"/missing"))
        self.assertIsNone(fs.call(3, b"/missing"))
        empty = host.MemoryFileSystem({})
        self.assertEqual(empty.call(1, b"/"), b"d 0 0")
        self.assertEqual(empty.call(2, b"/"), b"")
        self.assertEqual(empty.call(3, b"/"), b"/")

    def test_write_append_mkdir_bom_and_replacement(self):
        fs = host.MemoryFileSystem({})
        self.assertEqual(fs.call(4, b"/new/deep/a\0\xef\xbb\xbfhello"), b"")
        self.assertEqual(fs.call(5, b"/new/deep/a\0!"), b"")
        self.assertEqual(fs.call(0, b"/new/deep/a"), b"\xef\xbb\xbfhello!")
        self.assertEqual(fs.call(1, b"/new/deep"), b"d 0 0")
        self.assertEqual(fs.call(4, b"/new/deep/a\0\xff"), b"")
        self.assertEqual(fs.snapshot()["/new/deep/a"], "�")
        fs.call(5, b"/first\0text")
        self.assertEqual(fs.snapshot()["/first"], "text")

    def test_write_errors_and_unknown_operation(self):
        fs = host.MemoryFileSystem({"/p/a": "", "/file": ""})
        for op in (4, 5):
            self.assertEqual(fs.call(op, b"/p\0x"), "open /p: is a directory")
            self.assertEqual(fs.call(op, b"/file/out/a\0x"), "mkdir /file: not a directory")
        self.assertEqual(fs.call(4, b"/\0x"), "open /: is a directory")
        self.assertIsNone(fs.call(99, b"/p"))
        with self.assertRaisesRegex(host.HostProtocolError, "separator"):
            fs.call(4, b"/missing-separator")

    def test_recursive_remove_prefix_boundary_and_empty_directories(self):
        fs = host.MemoryFileSystem({"/a/b": "x", "/a/c/d": "y", "/ab/e": "z"})
        self.assertEqual(fs.call(6, b"/a"), b"")
        self.assertEqual(fs.snapshot(), {"/ab/e": "z"})
        self.assertIsNone(fs.call(1, b"/a"))
        self.assertEqual(fs.call(6, b"/missing"), b"")
        fs.call(6, b"/")
        self.assertEqual(fs.snapshot(), {})
        self.assertEqual(fs.call(2, b"/"), b"")

    def test_chtimes_does_not_change_memory_filesystem_mtime(self):
        fs = host.MemoryFileSystem({"/a": "x"})
        self.assertEqual(fs.call(7, b"/a\000\000"), b"")
        self.assertEqual(fs.call(7, b"/missing\0\0"), b"")
        self.assertEqual(fs.call(1, b"/a"), b"f 1 0")

    def test_files_are_copied_and_binary_append_is_lossless(self):
        source = {"/a": {"base64": "//4="}}
        fs = host.MemoryFileSystem(source)
        fs.call(5, b"/a\0\0A")
        self.assertEqual(fs.snapshot(), {"/a": {"base64": "//4AQQ=="}})
        fs.snapshot()["/a"]["base64"] = ""
        self.assertEqual(fs.call(0, b"/a"), b"\xff\xfe\0A")
        self.assertEqual(source, {"/a": {"base64": "//4="}})

    def test_case_flag_does_not_invent_case_folding(self):
        _, _, fs = host._prepare({"caseSensitive": False, "files": {"/A.ts": ""}})
        self.assertIsNone(fs.call(0, b"/a.ts"))


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.caller = FakeCaller()
        self.state = host._RunState({"diagnostics": "text", "env": []}, host.MemoryFileSystem({"/a": "hé"}))

    def test_unsigned_bounds_include_empty_one_past_end(self):
        self.assertEqual(host.checked_range(256, 256, 0), (256, 256))
        self.assertEqual(host.checked_range(1 << 32, -1, 1), (0xFFFFFFFF, 1 << 32))
        for size, ptr, length in ((256, -1, 0), (256, 0, -1), (256, 255, 2),
                                  (256, 257, 0), (256, 1 << 32, 0), (1 << 32, -1, 2)):
            with self.subTest(ptr=ptr, length=length), self.assertRaises(host.HostProtocolError):
                host.checked_range(size, ptr, length)

    def test_read_and_staged_take(self):
        self.caller.mem.data[:2] = b"/a"
        self.assertEqual(self.state.fs_call(self.caller, 0, 0, 2), 3)
        self.state.fs_take(self.caller, 20)
        self.assertEqual(self.caller.mem.data[20:23], b"h\xc3\xa9")
        with self.assertRaises(host.HostProtocolError):
            self.state.fs_take(self.caller, 20)

    def test_empty_take_and_failure_clear_old_staging(self):
        self.state.staged = b""
        self.state.fs_take(self.caller, 256)
        self.state.staged = b"stale"
        self.assertEqual(self.state.fs_call(self.caller, 99, 0, 0), -1)
        self.assertIsNone(self.state.staged)

    def test_write_error_length_is_utf8_bytes(self):
        raw = "/a/é\0data".encode()
        self.caller.mem.data[:len(raw)] = raw
        result = self.state.fs_call(self.caller, 4, 0, len(raw))
        error = b"mkdir /a: not a directory"
        self.assertEqual(result, -2 - len(error))
        self.state.fs_take(self.caller, 64)
        self.assertEqual(self.caller.mem.data[64:64 + len(error)], error)

    def test_callbacks_reject_bad_ranges(self):
        with self.assertRaises(host.HostProtocolError):
            self.state.fs_call(self.caller, 0, -1, 2)
        self.state.staged = b"hi"
        with self.assertRaises(host.HostProtocolError):
            self.state.fs_take(self.caller, 255)

    def test_environment_and_no_inherited_arguments(self):
        self.state.options["env"] = [b"A=value\0", b"B=\0"]
        self.assertEqual(self.state.wasi("environ_sizes_get", self.caller, 0, 4), 0)
        self.assertEqual(struct.unpack_from("<II", self.caller.mem.data), (2, 11))
        self.state.wasi("environ_get", self.caller, 8, 32)
        self.assertEqual(struct.unpack_from("<II", self.caller.mem.data, 8), (32, 40))
        self.assertEqual(self.caller.mem.data[32:43], b"A=value\0B=\0")
        self.state.wasi("args_sizes_get", self.caller, 0, 4)
        self.assertEqual(self.caller.mem.data[:8], bytes(8))
        self.assertEqual(self.state.wasi("fd_prestat_get", self.caller, 3, 0), 8)
        self.assertEqual(self.state.wasi("path_open", self.caller), 52)

    def test_fd_write_iovecs_bytes_and_invalid_fd(self):
        self.caller.mem.data[40:44] = b"A\xc3\xa9!"
        struct.pack_into("<IIII", self.caller.mem.data, 0, 40, 1, 41, 3)
        self.assertEqual(self.state.wasi("fd_write", self.caller, 1, 0, 2, 24), 0)
        self.assertEqual(self.state.result(0)["stdout"], "Aé!")
        self.assertEqual(struct.unpack_from("<I", self.caller.mem.data, 24)[0], 4)
        self.assertEqual(self.state.wasi("fd_write", self.caller, -1, 0, 2, 24), 8)

    def test_bad_fd_write_has_no_partial_output(self):
        struct.pack_into("<IIII", self.caller.mem.data, 0, 32, 1, 255, 2)
        with self.assertRaises(host.HostProtocolError):
            self.state.wasi("fd_write", self.caller, 1, 0, 2, 24)
        self.assertEqual(self.state.stdout, b"")
        with self.assertRaises(host.HostProtocolError):
            self.state.wasi("fd_write", self.caller, 1, 0, -1, 24)

    def test_clocks_random_fdstat_poll_and_no_memory_growth(self):
        self.state.wasi("clock_time_get", self.caller, 0, 0, 0)
        self.assertGreater(struct.unpack_from("<Q", self.caller.mem.data)[0], 0)
        self.state.wasi("clock_res_get", self.caller, 0, 0)
        self.assertEqual(struct.unpack_from("<Q", self.caller.mem.data)[0], 1000)
        with patch.object(host.os, "urandom", return_value=b"test"):
            self.state.wasi("random_get", self.caller, 8, 4)
        self.assertEqual(self.caller.mem.data[8:12], b"test")
        self.state.wasi("fd_fdstat_get", self.caller, 1, 16)
        self.assertEqual(self.caller.mem.data[16:40], bytes(24))
        self.assertEqual(self.state.wasi("fd_fdstat_get", self.caller, 3, 0), 8)
        self.state.wasi("poll_oneoff", self.caller, 0, 0, 0, 0)
        self.assertEqual(self.caller.mem.data[:4], bytes(4))
        self.assertEqual(self.state.wasi("sched_yield", self.caller), 0)
        self.assertEqual(len(self.caller.mem.data), 256)

    def test_stdout_and_stderr_preserve_leading_bom_and_split_utf8(self):
        self.state.stdout.extend(b"\xef\xbb\xbf\xc3")
        self.state.stdout.extend(b"\xa9")
        self.state.stderr.extend(b"\xef\xbb\xbfoops")
        result = self.state.result(2)
        self.assertEqual(result["stdout"], "\ufeffé")
        self.assertEqual(result["stderr"], "\ufeffoops")


def fixture(body="i32.const 0", *, imports="", extra="", data="", input_pointer="i32.const 4096",
            output_pointer="i32.const 8192", output_length="i32.const 0"):
    return f'''(module
      {imports}
      (memory (export "memory") 1)
      (global $length (mut i32) (i32.const 0))
      {extra}
      {data}
      (func (export "tsrs_input") (param $n i32) (result i32)
        local.get $n global.set $length {input_pointer})
      (func (export "tsrs_run") (result i32) {body})
      (func (export "tsrs_output") (result i32) {output_pointer})
      (func (export "tsrs_output_len") (result i32) {output_length}))'''


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import fcntl
        # Parent owns real compilation. Never wait for or overlap its queue.
        lock_path = Path(__file__).resolve().parents[1] / "build/tsrs/compiler-memory.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        cls.lock = lock_path.open("a+")
        try:
            fcntl.flock(cls.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            cls.lock.close()
            raise unittest.SkipTest("compiler-memory.lock is busy; synthetic JIT tests not run")
        cls.addClassCleanup(cls.lock.close)
        cls.wt = host._wasmtime()

    def compile(self, wat):
        compiled = host.compileEngine(bytes(self.wt.wat2wasm(wat)), wasm_stack_bytes=2 * host.MIB,
                                      python_stack_bytes=8 * host.MIB, max_memory_bytes=2 * host.MIB)
        self.addCleanup(compiled.close)
        return compiled

    def test_input_is_exact_with_optional_reactor_initializer(self):
        imports = '(import "wasi_snapshot_preview1" "fd_write" (func $write (param i32 i32 i32 i32) (result i32)))'
        body = '''i32.const 0 i32.const 4096 i32.store
                  i32.const 4 global.get $length i32.store
                  i32.const 1 i32.const 0 i32.const 1 i32.const 8 call $write drop i32.const 0'''
        compiled = self.compile(fixture(body, imports=imports, extra='(func (export "_initialize"))'))
        request = {"cwd": "/p", "args": ["--noEmit", "a.ts"], "caseSensitive": False, "files": {"/p/a.ts": "é"}}
        result = host.runRequest(compiled, request)
        self.assertEqual(result["stdout"], "/p\x002\x00--noEmit\x00a.ts")
        self.assertEqual(result["exitCode"], 0)
        self.assertIsNone(result["diagnostics"])
        self.assertEqual(result["files"], request["files"])

    def test_reactor_initializer_runs_once_per_fresh_request(self):
        body = "global.get $initialized i32.const 1 i32.ne if unreachable end i32.const 0"
        init = '(global $initialized (mut i32) (i32.const 0)) (func (export "_initialize") global.get $initialized if unreachable end i32.const 1 global.set $initialized)'
        compiled = self.compile(fixture(body, extra=init))
        self.assertEqual(host.runRequest(compiled, {})["exitCode"], 0)
        self.assertEqual(host.runRequest(compiled, {})["exitCode"], 0)

    def test_host_files_diagnostics_and_fresh_instance(self):
        imports = '''(import "tsrs_host" "fs" (func $fs (param i32 i32 i32) (result i32)))
                     (import "tsrs_host" "fs_take" (func $take (param i32)))
                     (import "wasi_snapshot_preview1" "fd_write" (func $write (param i32 i32 i32 i32) (result i32)))'''
        data = '(data (i32.const 16) "/a") (data (i32.const 32) "/out\\00done") (data (i32.const 8192) "[{\\22code\\22:2322}]")'
        body = '''(local $n i32)
                  global.get $used if unreachable end i32.const 1 global.set $used
                  i32.const 0 i32.const 16 i32.const 2 call $fs local.set $n
                  i32.const 100 call $take
                  i32.const 0 i32.const 100 i32.store i32.const 4 local.get $n i32.store
                  i32.const 1 i32.const 0 i32.const 1 i32.const 8 call $write drop
                  i32.const 4 i32.const 32 i32.const 9 call $fs drop i32.const 200 call $take
                  i32.const 2'''
        compiled = self.compile(fixture(body, imports=imports, data=data,
                                       extra='(global $used (mut i32) (i32.const 0))', output_length="i32.const 15"))
        request = {"files": {"/a": "hé"}, "diagnostics": "json"}
        original = copy.deepcopy(request)
        first = host.runRequest(compiled, request)
        self.assertEqual(first, {"exitCode": 2, "stdout": "hé", "stderr": "",
                                 "diagnostics": [{"code": 2322}], "files": {"/a": "hé", "/out": "done"}})
        first["files"]["/a"] = "changed"
        self.assertEqual(host.runRequest(compiled, request)["stdout"], "hé")
        self.assertEqual(request, original)

    def test_binary_file_bytes_reach_guest(self):
        imports = '''(import "tsrs_host" "fs" (func $fs (param i32 i32 i32) (result i32)))
                     (import "tsrs_host" "fs_take" (func $take (param i32)))'''
        body = '''i32.const 0 i32.const 0 i32.const 2 call $fs drop
                  i32.const 100 call $take
                  i32.const 100 i32.load16_u i32.const 65279 i32.ne if unreachable end i32.const 0'''
        compiled = self.compile(fixture(body, imports=imports, data='(data (i32.const 0) "/a")'))
        result = host.runRequest(compiled, {"files": {"/a": {"base64": "//5BAA=="}}})
        self.assertEqual(result["exitCode"], 0)
        self.assertEqual(result["files"], {"/a": {"base64": "//5BAA=="}})

    def test_proc_exit_retains_nonzero_status(self):
        imports = '(import "wasi_snapshot_preview1" "proc_exit" (func $exit (param i32)))'
        compiled = self.compile(fixture("i32.const 70 call $exit unreachable", imports=imports))
        result = host.runRequest(compiled, {"diagnostics": "json"})
        self.assertEqual(result["exitCode"], 70)
        self.assertEqual(result["diagnostics"], [])

    def test_trap_captures_partial_output_and_never_succeeds(self):
        imports = '(import "wasi_snapshot_preview1" "fd_write" (func $write (param i32 i32 i32 i32) (result i32)))'
        body = '''i32.const 0 i32.const 100 i32.store i32.const 4 i32.const 4 i32.store
                  i32.const 2 i32.const 0 i32.const 1 i32.const 8 call $write drop unreachable'''
        compiled = self.compile(fixture(body, imports=imports, data='(data (i32.const 100) "oops")'))
        for _ in range(2):
            with self.assertRaises(host.HostRuntimeError) as caught:
                host.runRequest(compiled, {})
            self.assertEqual(caught.exception.result["exitCode"], 70)
            self.assertEqual(caught.exception.result["stderr"], "oops")
            self.assertIn("unreachable", str(caught.exception))

    def test_bad_input_and_output_pointers_are_errors(self):
        for wat, request in ((fixture(input_pointer="i32.const -1"), {}),
                             (fixture(output_pointer="i32.const 65535", output_length="i32.const 2"), {"diagnostics": "json"}),
                             (fixture(output_length="i32.const -1"), {"diagnostics": "json"})):
            compiled = self.compile(wat)
            with self.assertRaises(host.HostRuntimeError) as caught:
                host.runRequest(compiled, request)
            self.assertEqual(caught.exception.result["exitCode"], 70)
            self.assertIn("range", str(caught.exception))

    def test_callback_memory_error_and_staged_error_text(self):
        imports = '''(import "tsrs_host" "fs" (func $fs (param i32 i32 i32) (result i32)))
                     (import "tsrs_host" "fs_take" (func $take (param i32)))'''
        bad = self.compile(fixture("i32.const 0 i32.const -1 i32.const 2 call $fs", imports=imports))
        with self.assertRaises(host.HostRuntimeError) as caught:
            host.runRequest(bad, {})
        self.assertIn("range", str(caught.exception))
        body = '''i32.const 4 i32.const 0 i32.const 5 call $fs i32.const -25 i32.ne if unreachable end
                  i32.const 100 call $take
                  i32.const 100 i32.load8_u i32.const 111 i32.ne if unreachable end i32.const 0'''
        good = self.compile(fixture(body, imports=imports, data='(data (i32.const 0) "/p\\00xx")'))
        self.assertEqual(host.runRequest(good, {"files": {"/p/a": ""}})["exitCode"], 0)

    def test_unknown_wasi_call_is_enosys_and_preopens_absent(self):
        imports = '''(import "wasi_snapshot_preview1" "fd_prestat_get" (func $preopen (param i32 i32) (result i32)))
                     (import "wasi_snapshot_preview1" "fd_close" (func $close (param i32) (result i32)))'''
        body = '''i32.const 3 i32.const 0 call $preopen i32.const 8 i32.ne if unreachable end
                  i32.const 1 call $close i32.const 52 i32.ne if unreachable end i32.const 0'''
        self.assertEqual(host.runRequest(self.compile(fixture(body, imports=imports)), {})["exitCode"], 0)

    def test_invalid_diagnostic_json_is_error(self):
        for data, length in (('"{}"', 2), ('"oops"', 4)):
            compiled = self.compile(fixture(data=f"(data (i32.const 8192) {data})", output_length=f"i32.const {length}"))
            with self.assertRaises(host.HostRuntimeError):
                host.runRequest(compiled, {"diagnostics": "json"})

    def test_start_trap_and_wrong_abi_rejected(self):
        compiled = self.compile(fixture(extra="(func $start unreachable) (start $start)"))
        with self.assertRaises(host.HostRuntimeError):
            host.runRequest(compiled, {})
        with self.assertRaisesRegex(host.HostProtocolError, "export"):
            self.compile('(module (memory (export "memory") 1))')
        with self.assertRaisesRegex(host.HostProtocolError, "unsupported import"):
            self.compile(fixture(imports='(import "other" "escape" (func))'))

    def test_worker_stack_is_restored_and_closed_engine_rejected(self):
        previous = threading.stack_size()
        compiled = self.compile(fixture())
        self.assertEqual(host.runRequest(compiled, {})["exitCode"], 0)
        self.assertEqual(threading.stack_size(), previous)
        compiled.close()
        with self.assertRaisesRegex(ValueError, "closed"):
            host.runRequest(compiled, {})

    def test_memory_growth_is_bounded(self):
        body = "i32.const 33 memory.grow i32.const -1 i32.ne if unreachable end i32.const 0"
        self.assertEqual(host.runRequest(self.compile(fixture(body)), {})["exitCode"], 0)

    def test_cli_request_output_and_trap_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module, request, output = root / "tiny.wasm", root / "request.json", root / "result.json"
            module.write_bytes(self.wt.wat2wasm(fixture("i32.const 2")))
            request.write_text(json.dumps({"files": {"/a": "é"}}))
            command = [sys.executable, "-B", str(Path(host.__file__)), "--module", str(module), "--request", str(request)]
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run(command + ["--output", str(output)], capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(json.loads(output.read_text())["files"], {"/a": "é"})
            module.write_bytes(self.wt.wat2wasm(fixture("unreachable")))
            result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 70, result.stderr)
            self.assertEqual(json.loads(result.stdout)["exitCode"], 70)
            self.assertIn("error", json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
