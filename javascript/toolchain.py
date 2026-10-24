"""Integrity checks and the Wasmtime host for the Wasm compiler guests."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "checksums/wasm-tools.json"
COMPONENT_HEADER = b"\0asm\x0d\0\x01\0"
CORE_HEADER = b"\0asm\x01\0\0\0"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_manifest() -> dict:
    document = json.loads(MANIFEST.read_text())
    if document.get("schemaVersion") != 1:
        raise ValueError("unsupported compiler manifest schema")
    return document


def tool_path(name: str) -> Path:
    filenames = {"componentize": "componentize.wasm", "esbuild": "esbuild.wasm"}
    if name not in filenames:
        raise ValueError(f"unknown compiler guest: {name}")
    return ROOT / ".toolchains/wasm" / filenames[name]


def wasmtime_path() -> Path:
    configured = os.environ.get("WISP_WASMTIME")
    candidates = [configured] if configured else [
        shutil.which("wasmtime"),
    ]
    for value in candidates:
        if value:
            path = Path(shutil.which(str(value)) or value).resolve()
            if path.is_file() and os.access(path, os.X_OK):
                return path
    raise ValueError("Wasmtime host is missing; set WISP_WASMTIME")


def check_integrity(*, artifact_root=ROOT) -> dict:
    document = load_manifest()
    files = document["files"]
    if not files:
        raise ValueError("compiler artifacts are not pinned; run bootstrap.py --rebuild")
    total = 0
    for relative, spec in files.items():
        path = artifact_root / relative
        if not path.resolve().is_relative_to(artifact_root.resolve()):
            raise ValueError(f"compiler input escapes toolchain: {relative}")
        if not path.is_file():
            raise ValueError(f"compiler input missing: {relative}; run python3 bootstrap.py")
        if path.stat().st_size != spec["bytes"] or digest(path) != spec["sha256"]:
            raise ValueError(f"compiler input checksum mismatch: {relative}; run python3 bootstrap.py --rebuild")
        if relative.endswith(".wasm") and path.read_bytes()[:8] != CORE_HEADER:
            raise ValueError(f"expected a Core Wasm compiler or runtime: {relative}")
        total += spec["bytes"]
    for name in ("componentize", "esbuild"):
        if str(tool_path(name).relative_to(ROOT)) not in files:
            raise ValueError(f"compiler manifest does not pin {name}")
    return {"manifestSha256": digest(MANIFEST), "fileCount": len(files), "bytes": total,
            "compilerExecution": "WASI guests hosted by Wasmtime", "nodeRequired": False}


def run_guest(arguments, *, mounts=(), timeout=120, capture=True):
    command = [str(wasmtime_path()), "run", "-C", "cache=y", "-O", "opt-level=0", "-C", "parallel-compilation=n"]
    for host, guest in mounts:
        command += ["--dir", f"{Path(host).resolve()}::{guest}"]
    command += [str(value) for value in arguments]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, timeout=timeout,
                            env={**os.environ, "RAYON_NUM_THREADS": "1", "TOKIO_WORKER_THREADS": "1"})
    if result.returncode:
        diagnostic = (result.stderr or result.stdout or "").strip()
        raise ValueError(f"Wasm compiler failed ({result.returncode}): {diagnostic}")
    return result
