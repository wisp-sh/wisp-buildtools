"""Host I/O and upstream AssemblyScript/Binaryen ABI glue for Wasmtime."""

import hashlib
import json
import os
import pathlib
import struct
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
if binding_root := os.environ.get("WISP_WASMTIME_PYTHON"):
    host_modules = pathlib.Path(binding_root).resolve(strict=True)
    if host_modules.is_relative_to(ROOT):
        raise SystemExit("WISP_WASMTIME_PYTHON must name Wisp's host installation outside the compiler package.")
    sys.path.insert(0, str(host_modules))
try:
    import wasmtime  # noqa: E402 - supplied by the Wisp host
except ModuleNotFoundError as error:
    raise SystemExit("Wisp must provide the Wasmtime Python API. Set WISP_WASMTIME_PYTHON to its host module directory.") from error
if pathlib.Path(wasmtime.__file__).resolve().is_relative_to(ROOT):
    raise SystemExit("Wasmtime must be supplied by Wisp outside the AssemblyScript compiler package.")


def validate_artifacts():
    """Check the installed compiler, backend and standard library before loading."""
    manifest_path = ROOT / "dist/manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(
            "Compiler bundle is missing. Run python3 bootstrap.py first."
        )
    manifest = json.loads(manifest_path.read_text())
    required = {"assemblyscript.wasm", "binaryen.wasm", "binaryen-aliases.json",
                "abi-shims/__i32_store8.wasm", "abi-shims/__i32_store.wasm",
                "abi-shims/_BinaryenAddTable.wasm"}
    if not required.issubset(manifest["artifacts"]):
        raise RuntimeError("Prebuilt compiler ABI shims are missing; rerun bootstrap.py")
    for name, info in manifest["artifacts"].items():
        path = ROOT / "dist" / name
        if not path.is_file():
            raise RuntimeError(
                f"Compiler artifact is missing: {name}. Run bootstrap.py."
            )
        data = path.read_bytes()
        if (
            len(data) != info["bytes"]
            or hashlib.sha256(data).hexdigest() != info["sha256"]
        ):
            raise RuntimeError(
                f"Compiler artifact checksum mismatch: {name}. Run bootstrap.py."
            )
    return manifest


class CppException(Exception):
    def __init__(self, pointer):
        self.pointer = pointer


class AssemblyScript:
    def __init__(self):
        validate_artifacts()
        config = wasmtime.Config()
        config.cache = True
        config.cranelift_opt_level = "none"
        config.parallel_compilation = False
        self.engine = wasmtime.Engine(config)
        self.store = wasmtime.Store(self.engine)
        self.bexports = None
        self.exception = None
        self.caught = []
        self.uncaught = 0
        self.aexports = None
        self.aliases = json.loads((ROOT / "dist/binaryen-aliases.json").read_text())
        self.bmodule = wasmtime.Module.from_file(
            self.engine, str(ROOT / "dist/binaryen.wasm")
        )
        blink = wasmtime.Linker(self.engine)
        self.import_results = {
            item.name: list(item.type.results) for item in self.bmodule.imports
        }
        for item in self.bmodule.imports:
            blink.define_func(
                item.module,
                item.name,
                item.type,
                lambda *args, name=item.name: self.binaryen_import(name, args),
            )
        self.binstance = blink.instantiate(self.store, self.bmodule)
        self.bexports = self.binstance.exports(self.store)
        self.bmemory = self.bexports["ya"]
        self.btable = self.bexports["QB"]
        self.bexports["za"](self.store)
        self.amodule = wasmtime.Module.from_file(
            self.engine, str(ROOT / "dist/assemblyscript.wasm")
        )
        alink = wasmtime.Linker(self.engine)
        for item in self.amodule.imports:
            if item.module == "binaryen":
                if item.name not in self.aliases:
                    raise RuntimeError(f"Unmapped Binaryen export: {item.name}")
                target = self.bexports[self.aliases[item.name]]
                actual = target.type(self.store)
                if [str(t) for t in actual.params] != [
                    str(t) for t in item.type.params
                ]:
                    path = ROOT / "dist/abi-shims" / f"{item.name}.wasm"
                    if not path.is_file():
                        raise RuntimeError(f"Missing prebuilt Binaryen ABI shim: {item.name}; rerun bootstrap.py")
                    shim = wasmtime.Instance(
                        self.store, wasmtime.Module.from_file(self.engine, str(path)), [target]
                    )
                    alink.define(
                        self.store,
                        item.module,
                        item.name,
                        shim.exports(self.store)["adapt"],
                    )
                else:
                    alink.define(self.store, item.module, item.name, target)
            elif item.module == "env" and item.name == "abort":
                alink.define_func(item.module, item.name, item.type, self.abort)
            else:
                raise RuntimeError(
                    f"Unsupported compiler import: {item.module}.{item.name}"
                )
        self.ainstance = alink.instantiate(self.store, self.amodule)
        self.aexports = self.ainstance.exports(self.store)
        self.amemory = self.aexports["memory"]
        self.call("_initialize")

    def bcall(self, name, *args):
        return self.bexports[self.aliases[name]](self.store, *args)

    def call(self, name, *args):
        self.aexports["__setArgumentsLength"](self.store, len(args))
        return self.aexports[name](self.store, *args)

    def string(self, value):
        if value is None:
            return 0
        data = value.encode("utf-16-le", errors="surrogatepass")
        ptr = self.call("__new", len(data), 2)
        self.call("__pin", ptr)
        self.amemory.write(self.store, data, ptr)
        return ptr

    def read_string(self, ptr):
        if not ptr:
            return None
        size = struct.unpack("<I", self.amemory.read(self.store, ptr - 4, ptr))[0]
        return self.amemory.read(self.store, ptr, ptr + size).decode(
            "utf-16-le", errors="replace"
        )

    def abort(self, message, filename, line, column):
        raise RuntimeError(
            f"{self.read_string(filename)}:{line}:{column}: {self.read_string(message)}"
        )

    def binaryen_import(self, name, args):
        # Emscripten's invoke_* callbacks dispatch C++ function pointers back into Wasm.
        invokes = {
            "c",
            "d",
            "e",
            "f",
            "g",
            "i",
            "j",
            "l",
            "m",
            "n",
            "o",
            "p",
            "q",
            "r",
            "s",
            "u",
            "v",
            "y",
            "z",
            "A",
            "B",
            "D",
            "E",
            "F",
            "G",
            "H",
            "I",
            "J",
            "K",
            "L",
            "M",
            "N",
            "O",
            "P",
            "R",
            "S",
            "T",
            "U",
            "V",
            "W",
            "X",
            "Y",
            "Z",
            "_",
            "ca",
            "da",
            "ea",
            "fa",
            "ga",
            "ha",
            "ja",
            "ka",
        }
        if name in invokes:
            stack = self.bexports["KB"](self.store)
            try:
                return self.btable.get(self.store, args[0])(self.store, *args[1:])
            except CppException:
                self.bexports["IB"](self.store, stack)
                self.bexports["GB"](self.store, 1, 0)
                return 0 if self.import_results[name] else None
        if name == "w":
            ptr, kind, destructor = args
            self.bmemory.write(self.store, struct.pack("<I", 0), ptr - 8)
            self.bmemory.write(
                self.store, struct.pack("<II", kind, destructor), ptr - 20
            )
            self.bexports["LB"](self.store, ptr)
            self.exception = CppException(ptr)
            self.uncaught += 1
            raise self.exception
        if name == "h":
            if self.exception is None:
                self.exception = CppException(args[0])
            raise self.exception
        if name == "Q":
            if not self.caught:
                raise RuntimeError("Binaryen rethrew without a caught exception")
            ptr = self.caught[-1]
            self.bmemory.write(self.store, b"\x00\x01", ptr - 12)
            self.uncaught += 1
            self.bexports["LB"](self.store, ptr)
            self.exception = CppException(ptr)
            raise self.exception
        if name == "t":
            ptr = args[0]
            if self.bmemory.read(self.store, ptr - 12, ptr - 11) == b"\x00":
                self.bmemory.write(self.store, b"\x01", ptr - 12)
                self.uncaught -= 1
            self.bmemory.write(self.store, b"\x00", ptr - 11)
            self.caught.append(ptr)
            return self.bexports["PB"](self.store, ptr)
        if name == "x":
            self.bexports["GB"](self.store, 0, 0)
            self.bexports["MB"](self.store, self.caught.pop())
            self.exception = None
            return None
        if name in ("a", "k"):
            if self.exception is None:
                self.bexports["HB"](self.store, 0)
                return 0
            ptr = self.exception.pointer
            self.bmemory.write(self.store, struct.pack("<I", ptr), ptr - 8)
            kind = struct.unpack(
                "<I", self.bmemory.read(self.store, ptr - 20, ptr - 16)
            )[0]
            matched = kind
            for candidate in args:
                if candidate == 0 or candidate == kind:
                    break
                if self.bexports["OB"](self.store, candidate, kind, ptr - 8):
                    matched = candidate
                    break
            self.bexports["HB"](self.store, matched)
            return ptr
        if name == "oa":
            required = args[0] & 0xFFFFFFFF
            current = self.bmemory.data_len(self.store)
            if required > current:
                self.bmemory.grow(self.store, (required - current + 65535) // 65536)
            return 1
        if name in ("wa", "va"):
            if name == "wa":
                for ptr in args:
                    self.bmemory.write(self.store, struct.pack("<I", 0), ptr)
            return 0
        if name == "na":
            self.bmemory.write(self.store, struct.pack("<Q", time.time_ns()), args[2])
            return 0
        if name == "aa":
            fd, iov, count, out = args
            total = 0
            for i in range(count):
                ptr, length = struct.unpack(
                    "<II", self.bmemory.read(self.store, iov + i * 8, iov + i * 8 + 8)
                )
                data = bytes(self.bmemory.read(self.store, ptr, ptr + length))
                (sys.stdout.buffer if fd == 1 else sys.stderr.buffer).write(data)
                total += length
            self.bmemory.write(self.store, struct.pack("<I", total), out)
            return 0
        if name == "C":
            return args[0]
        if name == "ta":
            return self.uncaught
        if name in ("la",):
            return None
        if name == "ma" and args[1] == 0:
            return 0
        if name in ("$", "pa", "qa", "ba", "ra", "sa"):
            return 8  # WASI EBADF: no filesystem access for the backend.
        if name == "b":

            def cstr(ptr):
                data = self.bmemory.read(
                    self.store, ptr, min(ptr + 2048, self.bmemory.data_len(self.store))
                )
                return bytes(data).split(b"\0", 1)[0].decode(errors="replace")

            raise RuntimeError(
                f"Binaryen assertion: {cstr(args[0])}, {cstr(args[1])}:{args[2]}"
            )
        raise RuntimeError(f"Unsupported Binaryen runtime callback {name} {args}")

    def diagnostics(self, program):
        errors = 0
        while True:
            diagnostic = self.call("nextDiagnostic", program)
            if not diagnostic:
                break
            self.call("__pin", diagnostic)
            try:
                textptr = self.call("formatDiagnostic", diagnostic, 0, 1)
                print(self.read_string(textptr), file=sys.stderr)
                errors += bool(self.call("isError", diagnostic))
            finally:
                self.call("__unpin", diagnostic)
        if errors:
            raise ValueError(
                f"AssemblyScript compilation failed with {errors} error(s)"
            )

    def compile(self, source, output, optimize=0, runtime="incremental"):
        source = pathlib.Path(source).resolve()
        options = self.call("newOptions")
        self.call("__pin", options)
        self.call("setTarget", options, 0)
        self.call(
            "setRuntime", options, {"stub": 0, "minimal": 1, "incremental": 2}[runtime]
        )
        # Match upstream asc: incremental GC requires a shadow stack for live roots.
        self.call("setStackSize", options, 32768 if runtime == "incremental" else 0)
        self.call("setExportMemory", options, 1)
        self.call("setNoAssert", options, 0)
        self.call("setOptimizeLevelHints", options, optimize, 0)
        self.call(
            "addGlobalAlias", options, self.string("abort"), self.string("__wisp_abort")
        )
        program = self.call("newProgram", options)
        self.call("__pin", program)
        std = ROOT / "dist/std"

        def parse(text, path, entry=False):
            textptr = self.string(text)
            pathptr = self.string(path)
            try:
                self.call("parse", program, textptr, pathptr, int(entry))
            finally:
                if textptr:
                    self.call("__unpin", textptr)
                self.call("__unpin", pathptr)

        parse(
            "@global function __wisp_abort(message: string | null = null, file: string | null = null, line: u32 = 0, column: u32 = 0): void { unreachable(); }",
            "~wisp/abort.ts",
        )
        for file in sorted(std.glob("*.ts")):
            if not file.name.endswith(".d.ts"):
                parse(file.read_text(), "~lib/" + file.name)
        file = std / f"rt/index-{runtime}.ts"
        parse(file.read_text(), f"~lib/rt/index-{runtime}.ts", True)
        parse(source.read_text(), source.as_posix(), True)
        while True:
            ptr = self.call("nextFile", program)
            if not ptr:
                break
            internal = self.read_string(ptr)
            if internal.startswith("~lib/"):
                base = std / internal.removeprefix("~lib/")
            else:
                base = pathlib.Path(internal)
            found = next(
                (
                    p
                    for p in [
                        pathlib.Path(str(base) + ".ts"),
                        base / "index.ts",
                        pathlib.Path(str(base) + ".d.ts"),
                    ]
                    if p.is_file()
                ),
                None,
            )
            if found:
                # Upstream treats portable .d.ts files as logical .ts modules.
                path = (
                    internal + "/index.ts"
                    if found.name == "index.ts"
                    else internal + ".ts"
                )
                parse(found.read_text(), path)
            else:
                parse(None, internal + ".ts")
        self.diagnostics(program)
        if os.environ.get("WISP_AS_TRACE"):
            print("compile", file=sys.stderr)
        module = self.call("compile", program)
        self.call("__pin", module)
        self.diagnostics(program)
        if optimize:
            if os.environ.get("WISP_AS_TRACE"):
                print("optimize", file=sys.stderr)
            self.call("optimize", module, optimize, 0, 0, 0)
        ref = self.call("getBinaryenModuleRef", module)
        result = self.bcall("_malloc", 12)
        self.bcall("_BinaryenModuleAllocateAndWrite", result, ref, 0)
        ptr, length, _ = struct.unpack(
            "<III", self.bmemory.read(self.store, result, result + 12)
        )
        data = bytes(self.bmemory.read(self.store, ptr, ptr + length))
        wasmtime.Module.validate(self.engine, data)
        # Keep an earlier successful build intact if writing the new file fails.
        output = pathlib.Path(output)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{output.name}.",
                suffix=".tmp",
                dir=output.parent,
                delete=False,
            ) as file:
                temporary = pathlib.Path(file.name)
                file.write(data)
            os.replace(temporary, output)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return data
