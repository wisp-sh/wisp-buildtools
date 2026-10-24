"""Wisp-owned execution host and exact-image external Swift backends."""
from __future__ import annotations

import hashlib
import json
import ctypes
import mmap
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import resource
import time

ROOT = Path(__file__).resolve().parent


def profile(stage, **details):
    """Optional per-process measurements without changing compiler stdout."""
    target = os.environ.get("WISP_SWIFT_PROFILE_LOG")
    if not target:
        return
    path = Path(target.replace("%p", str(os.getpid())))
    path.parent.mkdir(parents=True, exist_ok=True)
    resident = None
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            resident = int(line.split()[1])
    value = {"stage": stage, "pid": os.getpid(), "seconds": time.monotonic(),
             "maxRssKiB": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
             "residentKiB": resident, **details}
    with path.open("a") as stream:
        stream.write(json.dumps(value) + "\n")


def load_module(wasmtime, engine, path):
    """Borrow mapped compiler bytes during compilation instead of copying twice."""
    path = Path(path)
    profile("module-load-start", path=str(path), bytes=path.stat().st_size)
    with path.open("rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_COPY) as mapped:
        binary = (ctypes.c_uint8 * len(mapped)).from_buffer(mapped)
        pointer = ctypes.POINTER(wasmtime._ffi.wasmtime_module_t)()
        try:
            error = wasmtime._ffi.wasmtime_module_new(engine.ptr(), binary, len(mapped), ctypes.byref(pointer))
        finally:
            del binary
        if error:
            raise wasmtime.WasmtimeError._from_ptr(error)
        module = wasmtime.Module._from_ptr(pointer)
    profile("module-load-finished", path=str(path))
    return module


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def host():
    supplied = os.environ.get("WISP_WASMTIME", "wasmtime")
    found = shutil.which(supplied)
    if not found:
        raise ValueError("set WISP_WASMTIME to Wisp's execution host")
    path = Path(found).resolve()
    if path.is_relative_to(ROOT):
        raise ValueError("Wasmtime must be provided outside the Swift compiler package")
    return path


def provider():
    return Path(os.environ.get("WISP_LLVM_SWIFT", ROOT.parent / "llvm/dist/swift-shared")).resolve()


def verify_inventory(root, inventory, label):
    if root.is_symlink():
        raise ValueError(f"{label} distribution cannot be a symlink")
    entries = list(root.rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise ValueError(f"{label} distribution contains a symlink")
    actual = {str(path.relative_to(root)) for path in entries if path.is_file() and path != root / "manifest.json"}
    if not inventory or set(inventory) != actual:
        raise ValueError(f"{label} file inventory differs from its manifest")
    for relative, expected in inventory.items():
        if (not isinstance(relative, str) or "\\" in relative or PurePosixPath(relative).is_absolute()
                or ".." in PurePosixPath(relative).parts or str(PurePosixPath(relative)) != relative):
            raise ValueError(f"invalid {label} manifest path")
        path = root / relative
        if (not path.resolve().is_relative_to(root) or path.stat().st_size != expected["bytes"]
                or sha(path) != expected["sha256"]):
            raise ValueError(f"{label} artifact changed or missing: {relative}")


def integrity():
    lock = json.loads((ROOT / "toolchain.lock.json").read_text())
    manifest = json.loads((ROOT / "dist/manifest.json").read_text())
    if manifest.get("bootstrap") != lock or not manifest.get("fullyWasmHostedBuild"):
        raise ValueError("Swift artifacts differ from their locked recipe; run bootstrap.py")
    verify_inventory(ROOT / "dist", manifest.get("files"), "Swift")
    shared = json.loads((provider() / "manifest.json").read_text())
    if sha(provider() / "manifest.json") != manifest["llvmProviderManifestSha256"]:
        raise ValueError("Swift backend provider does not match this compiler package")
    verify_inventory(provider(), shared.get("files"), "Swift backend")
    for name, image in shared["images"].items():
        if (image["sourceSha256"] != lock["artifacts"][name]["moduleSha256"]
                or image["primary"] != manifest["images"][name]["primary"]):
            raise ValueError(f"Swift backend image mismatch: {name}")
    return manifest


def command(name, arguments, mounts=()):
    if name not in {"swift-frontend", "wasm-ld"}:
        raise ValueError("unknown Swift compiler guest")
    argv = [sys.executable, str(ROOT / "runtime.py"), "--guest", name]
    for physical, virtual in mounts:
        argv += ["--mount", f"{Path(physical).resolve()}::{virtual}"]
    argv += ["--", *map(str, arguments)]
    return argv


def binding():
    supplied = os.environ.get("WISP_WASMTIME_PYTHON")
    if supplied:
        selected = Path(supplied).resolve()
        if not (selected / "wasmtime/__init__.py").is_file():
            raise ValueError("WISP_WASMTIME_PYTHON must contain Wisp's wasmtime Python package")
        sys.path.insert(0, str(selected))
    try:
        import wasmtime
    except ImportError as error:
        raise ValueError("Wisp must provide the Wasmtime Python host; set WISP_WASMTIME_PYTHON") from error
    location = Path(wasmtime.__file__).resolve()
    if supplied and not location.is_relative_to(selected):
        raise ValueError("the loaded Wasmtime Python host differs from WISP_WASMTIME_PYTHON")
    if location.is_relative_to(ROOT):
        raise ValueError("the Wasmtime Python host must be outside the Swift package")
    return wasmtime


def host_report():
    wasmtime = binding()
    native = Path(wasmtime._ffi.dll._name).resolve()
    report = {"provider": "wisp", "compilerHost": "wasmtime-python",
            "loaderSha256": sha(__file__),
            "pythonBinding": str(Path(wasmtime.__file__).resolve()),
            "nativeLibrary": str(native), "nativeLibrarySha256": sha(native),
            "wasmToolsHost": str(host()), "compilerHostBackend": os.environ.get("WISP_SWIFT_HOST_BACKEND", "cranelift")}
    if report["compilerHostBackend"] == "streamed":
        record = prepared_shards("swift-frontend")
        report["compilerShards"] = {
            "manifestSha256": sha(Path(os.environ["WISP_SWIFT_SHARDS"]) / "manifest.json"),
            "sourceSha256": record["sourceSha256"], "toolSha256": record["toolSha256"],
            "shards": len(record["shards"]), "totalModuleBytes": record["totalModuleBytes"],
        }
    return report


def prepared_shards(name):
    """Read explicit compiler-bootstrap output; never build during a source run."""
    directory = os.environ.get("WISP_SWIFT_SHARDS")
    if not directory:
        raise ValueError("streamed requires WISP_SWIFT_SHARDS pointing to a prepared shard.py output")
    from shard import verify
    return verify(provider() / (name + "-llvm.so.wasm"), Path(directory),
                  int(os.environ.get("WISP_SWIFT_SHARD_COUNT", "8")), name,
                  tool_sha256=os.environ.get("WISP_SWIFT_SHARD_TOOL_SHA256"))


def compilation_engine(wasmtime):
    config = wasmtime.Config()
    config.cache = os.environ.get("WISP_SWIFT_CACHE_CONFIG", True)
    config.parallel_compilation = False
    selected_backend = os.environ.get("WISP_SWIFT_HOST_BACKEND", "cranelift")
    if selected_backend in {"winch", "winch-original", "winch-compatible", "winch-sharded"}:
        # The binding's public setter omits Winch, although its C API exposes it.
        wasmtime._ffi.wasmtime_config_strategy_set(config.ptr(), 2)
    elif selected_backend not in {"cranelift", "streamed"}:
        raise ValueError("unknown WISP_SWIFT_HOST_BACKEND")
    # Swift optimizes generated applications; skip re-optimizing the compiler guest.
    config.cranelift_opt_level = "none"
    # The Python host also includes async support, whose stack must cover the
    # configured Wasm stack even though these compiler guests are synchronous.
    wasmtime._ffi.wasmtime_config_async_stack_size_set(config.ptr(), 8 * 1024 * 1024)
    config.max_wasm_stack = 4 * 1024 * 1024
    engine = wasmtime.Engine(config)
    return engine, selected_backend


def execute(name, arguments, mounts):
    integrity()
    os.environ.setdefault("RAYON_NUM_THREADS", "2")
    wasmtime = binding()
    engine, selected_backend = compilation_engine(wasmtime)
    store = wasmtime.Store(engine)
    wasi = wasmtime.WasiConfig()
    wasi.argv = [name, *arguments]
    wasi.inherit_stdout()
    wasi.inherit_stderr()
    for physical, virtual in mounts:
        wasi.preopen_dir(physical, virtual)
    store.set_wasi(wasi)
    linker = wasmtime.Linker(engine)
    linker.define_wasi()
    def instantiate(path):
        instance = linker.instantiate(store, load_module(wasmtime, engine, path))
        profile("module-instantiated", path=str(path))
        return instance
    if selected_backend == "winch-original":
        original = ROOT / ".cache/originals" / (name + ".wasm")
        lock = json.loads((ROOT / "toolchain.lock.json").read_text())
        if not original.is_file() or sha(original) != lock["artifacts"][name]["moduleSha256"]:
            raise ValueError("winch-original requires the checksum-pinned original compiler image from bootstrap")
        entry = instantiate(original)
    else:
        def selected_image(path):
            if selected_backend in {"winch-compatible", "winch-sharded"}:
                from compat import image
                return image(path)
            return path
        primary = instantiate(selected_image(ROOT / "dist/bin" / (name + "-primary.wasm")))
        linker.define_instance(store, name + "-image", primary)
        if selected_backend == "streamed" and name == "swift-frontend":
            record = prepared_shards(name)
            dispatch = record["dispatchTable"]
            if dispatch["element"] != "funcref" or dispatch["table64"] or dispatch["shared"]:
                raise ValueError("unsupported compiler dispatch table")
            table = wasmtime.Table(store, wasmtime.TableType(wasmtime.ValType.funcref(),
                                   wasmtime.Limits(dispatch["minimum"], dispatch["maximum"])), None)
            linker.define(store, dispatch["module"], dispatch["name"], table)
            state = instantiate(Path(record["state"]["path"]))
            linker.define_instance(store, record["stateNamespace"], state)
            shards = [instantiate(Path(part["path"])) for part in record["shards"]]
        elif selected_backend == "winch-sharded" and name == "swift-frontend":
            from multisplit import ensure
            parts = ensure()['files']
            backend = instantiate(selected_image(Path(parts[0]['path'])))
            linker.define_instance(store, "swift-frontend-backend-image", backend)
            shards = [instantiate(selected_image(Path(part['path']))) for part in parts[1:]]
        else:
            backend = instantiate(selected_image(provider() / (name + "-llvm.so.wasm")))
        entry = instantiate(selected_image(ROOT / "dist/bin" / (name + ".wasm")))
    try:
        profile("guest-start", name=name)
        entry.exports(store)["_start"](store)
    except wasmtime.ExitTrap as error:
        if error.code != 0:
            raise SystemExit(error.code) from error
    profile("guest-finished", name=name)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guest", required=True, choices=["swift-frontend", "wasm-ld"])
    parser.add_argument("--mount", action="append", default=[])
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    guest_args = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    try:
        execute(args.guest, guest_args, [value.split("::", 1) for value in args.mount])
    except (OSError, ValueError) as error:
        raise SystemExit(f"Swift host: {error}") from error
