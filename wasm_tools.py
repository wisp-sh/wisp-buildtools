"""Run the pinned upstream wasm-tools CLI inside WASI, never a native fallback."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT / "wasm-tools"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def runtime() -> Path:
    candidates = [os.environ.get("WISP_WASMTIME"), os.environ.get("WASMTIME"),
                  shutil.which("wasmtime")]
    for value in candidates:
        if value:
            path = Path(shutil.which(str(value)) or value)
            if path.is_file():
                return path.resolve()
    raise ValueError("set WISP_WASMTIME to a Wasmtime executable")


def module(configured=None) -> Path:
    supplied = configured or os.environ.get("WISP_WASM_TOOLS")
    path = Path(supplied).resolve() if supplied else PACKAGE / "dist/wasm-tools.wasm"
    if not path.is_file():
        raise ValueError("run python3 wasm-tools/bootstrap.py before building")
    with path.open("rb") as stream:
        if stream.read(8) != b"\0asm\x01\0\0\0":
            raise ValueError("wasm-tools must be a WASI Core Wasm module; native binaries are not supported")
    if not supplied:
        manifest = json.loads((PACKAGE / "dist/manifest.json").read_text())
        lock = json.loads((PACKAGE / "toolchain.lock.json").read_text())
        if (manifest.get("bootstrap") != lock or manifest.get("sha256") != lock["module_sha256"]
                or digest(path) != lock["module_sha256"]):
            raise ValueError("WASI wasm-tools package changed; rerun its bootstrap")
    return path


def preview1_adapter(configured=None) -> Path:
    supplied = configured or os.environ.get("WISP_PREVIEW1_ADAPTER")
    path = Path(supplied).resolve() if supplied else PACKAGE / "dist/wasi_snapshot_preview1.reactor.wasm"
    if not path.is_file():
        raise ValueError("Preview 1 reactor adapter is missing; run python3 wasm-tools/bootstrap.py")
    with path.open("rb") as stream:
        if stream.read(8) != b"\0asm\x01\0\0\0":
            raise ValueError("Preview 1 reactor adapter must be a Core Wasm module")
    if not supplied:
        lock = json.loads((PACKAGE / "adapter.lock.json").read_text())
        manifest = json.loads((PACKAGE / "dist/manifest.json").read_text())
        if (manifest.get("preview1Adapter") != lock or path.stat().st_size != lock["module"]["size"]
                or digest(path) != lock["module"]["sha256"]):
            raise ValueError("shared Preview 1 reactor adapter changed; rerun its bootstrap")
    return path


def guest_command(guest, *args, directories=()) -> list[str]:
    """Mount only argument paths and supplied directories, preserving absolute paths."""
    mounts = {Path(path).resolve() for path in directories}
    for argument in args:
        value = str(argument)
        candidate = value if Path(value).is_absolute() else value.split("=", 1)[-1]
        path = Path(candidate)
        if path.is_absolute():
            mounts.add(path if path.is_dir() else path.parent)
    ordered = []
    for path in sorted(mounts, key=lambda item: (len(item.parts), str(item))):
        if not path.is_dir():
            raise ValueError(f"guest mount directory does not exist: {path}")
        if not any(path.is_relative_to(parent) for parent in ordered):
            ordered.append(path)
    command = [str(runtime()), "run", "-C", "cache=y"]
    for path in ordered:
        command += ["--dir", f"{path}::{path}"]
    return [*command, str(guest), *(str(value) for value in args)]


def command(*args, configured=None, directories=()) -> list[str]:
    return guest_command(module(configured), *args, directories=directories)
