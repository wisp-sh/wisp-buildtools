"""Verify the shared JVM guest and its immutable class-library data."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil

ROOT = Path(__file__).resolve().parent


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def host():
    supplied = os.environ.get("WISP_WASMTIME") or os.environ.get("WASMTIME") or "wasmtime"
    found = shutil.which(supplied)
    if not found:
        raise ValueError("set WISP_WASMTIME to Wisp's execution host")
    path = Path(found).resolve()
    if path.is_relative_to(ROOT):
        raise ValueError("Wasmtime must be provided outside the JVM package")
    return path


def integrity():
    dist = ROOT / "dist"
    if dist.is_symlink():
        raise ValueError("JVM distribution cannot be a symlink")
    manifest = json.loads((dist / "manifest.json").read_text())
    lock = json.loads((ROOT / "toolchain.lock.json").read_text())
    if manifest.get("upstream") != lock or manifest.get("target") != "wasm32-wasip1":
        raise ValueError("JVM distribution differs from its pinned inputs; run jvm/bootstrap.py")
    entries = list(dist.rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise ValueError("JVM distribution contains a symlink")
    actual = {str(path.relative_to(dist)) for path in entries if path.is_file() and path != dist / "manifest.json"}
    inventory = manifest.get("files")
    if not inventory or set(inventory) != actual:
        raise ValueError("JVM distribution file inventory differs from its manifest")
    for relative, expected in inventory.items():
        if (not isinstance(relative, str) or "\\" in relative or PurePosixPath(relative).is_absolute()
                or ".." in PurePosixPath(relative).parts or str(PurePosixPath(relative)) != relative):
            raise ValueError("invalid JVM distribution manifest path")
        path = dist / relative
        if (path.is_symlink() or not path.resolve().is_relative_to(dist) or not path.is_file()
                or path.stat().st_size != expected["bytes"] or digest(path) != expected["sha256"]):
            raise ValueError(f"JVM artifact missing or changed: {relative}")
    if (dist / "jvm.wasm").read_bytes()[:8] != b"\0asm\x01\0\0\0":
        raise ValueError("JVM interpreter must be a Core Wasm guest")
    return manifest
