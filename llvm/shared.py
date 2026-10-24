"""Locate and verify Wisp's external LLVM shared Wasm libraries."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

PROFILE = "llvm24-wasip3"
MODULE_NAMES = {"wisp-llvm24", "wisp-llvm24-libc", "wisp-llvm24-libcxx", "wisp-llvm24-libcxxabi"}
# Module import names are part of the binary ABI. Existing Vise bundles keep
# their original names and hashes; new builds use the Wisp names above.
LEGACY_MODULE_NAMES = {name.replace("wisp-", "vise-", 1) for name in MODULE_NAMES}


def root(directory: Path | None = None) -> Path:
    if directory is not None:
        return directory.resolve()
    explicit = os.environ.get("WISP_LLVM")
    return Path(explicit).resolve() if explicit else Path(__file__).resolve().parent / "dist/shared"


def manifest(directory: Path | None = None) -> dict:
    directory = root(directory)
    try:
        result = json.loads((directory / "manifest.json").read_text())
        entries = result["coreModules"]
        if result["schema"] != 1 or result["profile"] != PROFILE or not isinstance(entries, list):
            raise ValueError("unsupported shared LLVM manifest")
        if len(entries) != len(MODULE_NAMES) or {item["name"] for item in entries} not in (MODULE_NAMES, LEGACY_MODULE_NAMES):
            raise ValueError("shared LLVM module inventory is incomplete")
        for item in entries:
            path = directory / item["path"]
            if not path.resolve().is_relative_to(directory) or path.is_symlink() or not path.is_file():
                raise ValueError(f"unsafe or missing shared LLVM module: {item['path']}")
            if path.stat().st_size != item["sizeBytes"]:
                raise ValueError(f"shared LLVM size mismatch: {item['path']}")
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != item["sha256"]:
                raise ValueError(f"shared LLVM checksum mismatch: {item['path']}")
        return result
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"shared LLVM is not ready at {directory}: {error}") from error


def preload_args(directory: Path | None = None) -> list[str]:
    directory = root(directory)
    # LLVM optimizes the application inside Wasm. Avoid an expensive second
    # optimization pass while the host JITs the compiler itself.
    return ["-C", "cache=y", "-O", "opt-level=0", *[argument for item in manifest(directory)["coreModules"]
            for argument in ("--preload", f"{item['name']}={directory / item['path']}")]]
