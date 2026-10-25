#!/usr/bin/env python3
"""Extract the authenticated WASI SDK application runtime, without native tools.

This deliberately reads the pinned archive, not the mutable bootstrap SDK tree.
It neither downloads artifacts nor executes a compiler.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parent
SYSROOT = "share/wasi-sysroot"
RESOURCE = "lib/clang/23"
TARGET = "wasm32-wasip1"
BUILTINS = f"{RESOURCE}/lib/wasm32-unknown-wasip1/libclang_rt.builtins.a"
LICENSE_NAMES = {"libcxx-LICENSE.TXT", "libcxxabi-LICENSE.TXT", "compiler-rt-LICENSE.TXT",
                 "wasi-libc/LICENSE", "wasi-libc/LICENSE-APACHE-LLVM", "wasi-libc/LICENSE-APACHE",
                 "wasi-libc/LICENSE-MIT", "wasi-libc/libc-bottom-half/cloudlibc/LICENSE",
                 "wasi-libc/libc-top-half/musl/COPYRIGHT", "wasi-libc/fts/musl-fts/COPYING"}
REQUIRED = {f"{SYSROOT}/include/{TARGET}/stdlib.h",
            f"{SYSROOT}/include/{TARGET}/noeh/c++/v1/vector",
            f"{SYSROOT}/include/{TARGET}/noeh/c++/v1/string",
            f"{RESOURCE}/include/stddef.h", f"{RESOURCE}/include/stdarg.h",
            f"{SYSROOT}/lib/{TARGET}/crt1-reactor.o",
            f"{SYSROOT}/lib/{TARGET}/libc.a",
            f"{SYSROOT}/lib/{TARGET}/noeh/libc++.a",
            f"{SYSROOT}/lib/{TARGET}/noeh/libc++abi.a", BUILTINS}


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def selected(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != name:
        raise ValueError(f"unsafe SDK archive path: {name}")
    headers = f"{SYSROOT}/include/{TARGET}/"
    if name.startswith(headers):
        relative = name[len(headers):]
        return not relative.startswith("eh/")
    if name.startswith(f"{RESOURCE}/include/") or name == BUILTINS:
        return True
    lib = f"{SYSROOT}/lib/{TARGET}/"
    if name.startswith(lib):
        relative = name[len(lib):]
        return (relative == "crt1-reactor.o" or
                ("/" not in relative and relative.endswith(".a")) or
                relative in {"noeh/libc++.a", "noeh/libc++abi.a"})
    return False


def inventory(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symlink in target SDK: {path}")
        if path.is_file() and path != root / "manifest.json":
            result[path.relative_to(root).as_posix()] = {
                "sha256": digest(path), "sizeBytes": path.stat().st_size}
    return result


def verify(root: Path, pin: dict) -> dict:
    if root.is_symlink():
        raise ValueError("target SDK directory must not be a symlink")
    manifest = json.loads((root / "manifest.json").read_text())
    if (manifest.get("schema") != 1 or manifest.get("wasiSdk") != pin or
            manifest.get("applicationTarget") != TARGET or
            manifest.get("sysroot") != SYSROOT or manifest.get("resourceDir") != RESOURCE or
            manifest.get("builtins") != BUILTINS or manifest.get("exceptions") is not False or
            manifest.get("compilerExecution") != "wasm-only"):
        raise ValueError("target SDK manifest differs from the pinned contract")
    files = inventory(root)
    if not REQUIRED <= files.keys() or manifest.get("files") != files:
        raise ValueError("target SDK inventory is incomplete or modified")
    receipts = json.loads((root / "licenses/receipts.json").read_text())
    if set(receipts) != LICENSE_NAMES:
        raise ValueError("target SDK license receipts are incomplete")
    for name, receipt in receipts.items():
        if files.get("licenses/" + name, {}).get("sha256") != receipt["sha256"]:
            raise ValueError(f"target SDK license checksum mismatch: {name}")
    for name in files:
        if not (selected(name) or name.startswith("licenses/")):
            raise ValueError(f"unexpected file in target SDK: {name}")
    return manifest


def package(archive: Path, output: Path, pin: dict, notices: Path) -> dict:
    if output.exists() or output.is_symlink():
        return verify(output, pin)
    if digest(archive) != pin["sha256"]:
        raise ValueError("WASI SDK archive checksum differs from llvm/toolchain.toml")
    receipts = json.loads((notices / "receipts.json").read_text())
    for name, receipt in receipts.items():
        if digest(notices / name) != receipt["sha256"]:
            raise ValueError(f"WASI SDK license checksum mismatch: {name}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".target-sdk-", dir=output.parent) as temporary:
        stage = Path(temporary) / "sdk"
        stage.mkdir()
        prefix = pin["archive"].removesuffix(".tar.gz") + "/"
        seen = set()
        # Streaming extraction keeps memory bounded and never extracts native bin/.
        with tarfile.open(archive, mode="r|gz") as tar:
            for member in tar:
                if not member.name.startswith(prefix):
                    if member.isdir() and member.name.rstrip("/") == prefix.rstrip("/"):
                        continue
                    raise ValueError(f"unexpected SDK archive root: {member.name}")
                name = member.name[len(prefix):].rstrip("/")
                if not name or not selected(name):
                    continue
                if member.isdir():
                    continue
                if not member.isfile() or name in seen:
                    raise ValueError(f"nonregular or duplicate SDK input: {name}")
                seen.add(name)
                path = stage / name
                path.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, path.open("xb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                path.chmod(0o644)
        if not REQUIRED <= seen:
            raise ValueError(f"missing target SDK inputs: {sorted(REQUIRED - seen)}")
        shutil.copytree(notices, stage / "licenses")
        manifest = {"schema": 1, "wasiSdk": pin, "applicationTarget": TARGET,
                    "sysroot": SYSROOT, "resourceDir": RESOURCE, "builtins": BUILTINS,
                    "exceptions": False, "compilerExecution": "wasm-only",
                    "files": inventory(stage)}
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        verify(stage, pin)
        stage.rename(output)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["package", "verify"])
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--out", type=Path, default=ROOT / "wasi-sdk")
    args = parser.parse_args()
    pin = tomllib.loads((ROOT.parent / "llvm/toolchain.toml").read_text())["inputs"]["wasi_sdk"]
    if args.command == "verify":
        manifest = verify(args.out, pin)
    else:
        archive = args.archive or ROOT / ".toolchains" / pin["archive"]
        manifest = package(archive, args.out, pin, ROOT.parent / "llvm/licenses/sdk")
    print(json.dumps({"target": TARGET, "files": len(manifest["files"]),
                      "bytes": sum(item["sizeBytes"] for item in manifest["files"].values())}))


if __name__ == "__main__":
    main()
