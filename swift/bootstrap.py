#!/usr/bin/env python3
"""Install pinned WASI Swift tools and separate their LLVM/runtime backends."""
from __future__ import annotations

import argparse
import gzip
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

from runtime import ROOT, integrity, sha
import split

sys.path.insert(0, str(ROOT.parent))
import wasm_tools


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def fetch(name, item, offline):
    path = ROOT / ".cache" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and sha(path) == item["sha256"]:
        return path
    if offline:
        raise ValueError(f"verified bootstrap input missing offline: {path}")
    with urllib.request.urlopen(item["url"]) as source, path.with_suffix(path.suffix + ".pending").open("wb") as output:
        shutil.copyfileobj(source, output)
    pending = path.with_suffix(path.suffix + ".pending")
    if sha(pending) != item["sha256"]:
        pending.unlink()
        raise ValueError(f"bootstrap checksum mismatch: {name}")
    os.replace(pending, path)
    return path


def inventory(directory):
    return {str(path.relative_to(directory)): {"sha256": sha(path), "bytes": path.stat().st_size}
            for path in sorted(directory.rglob("*")) if path.is_file() and path != directory / "manifest.json"}


def recipe():
    return {name: sha(ROOT / name) for name in ["bootstrap.py", "split.py", "src/adapter.swift"]}


def publish(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.with_name("." + target.name + "-previous")
    if backup.exists():
        shutil.rmtree(backup)
    if target.exists():
        os.replace(target, backup)
    try:
        os.replace(source, target)
    except OSError:
        if backup.exists():
            os.replace(backup, target)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    if args.check:
        manifest = integrity()
        if manifest.get("recipe") != recipe():
            raise ValueError("Swift bootstrap source inputs changed; rebuild the distribution")
        print(json.dumps({"passed": True, "version": manifest["version"], "files": len(manifest["files"])}))
        return
    if not args.rebuild:
        try:
            installed = integrity()
            if installed.get("recipe") == recipe():
                print("Swift compiler and external backends already match their pinned inputs")
                return
        except (OSError, ValueError, KeyError):
            pass
    lock = json.loads((ROOT / "toolchain.lock.json").read_text())
    cache = ROOT / ".cache"
    cache.mkdir(exist_ok=True)
    originals = cache / "originals"
    originals.mkdir(exist_ok=True)
    for name in ["swift-frontend", "wasm-ld"]:
        item = lock["artifacts"][name]
        path = originals / (name + ".wasm")
        if not path.is_file() or sha(path) != item["moduleSha256"]:
            current = ROOT / "dist" / (name + ".wasm")
            if current.is_file() and sha(current) == item["moduleSha256"]:
                os.replace(current, path)
            else:
                archive = fetch(name + ".wasm.gz", item, args.offline)
                with gzip.open(archive, "rb") as source, path.open("wb") as output:
                    shutil.copyfileobj(source, output)
            if sha(path) != item["moduleSha256"]:
                raise ValueError("decompressed Swift compiler checksum mismatch")
    sdk = fetch("swift-sysroot-core.tar.gz", lock["artifacts"]["sysroot"], args.offline)
    for name, item in lock["licenses"].items():
        cached = cache / "licenses" / name
        current = ROOT / "dist/licenses" / name
        if not cached.is_file() and current.is_file() and sha(current) == item["sha256"]:
            cached.parent.mkdir(exist_ok=True)
            shutil.copyfile(current, cached)
    with tempfile.TemporaryDirectory(prefix=".bootstrap-", dir=ROOT) as directory:
        work = Path(directory)
        distribution, shared = work / "dist", work / "shared"
        (distribution / "bin").mkdir(parents=True)
        (distribution / "sysroot").mkdir()
        (distribution / "licenses").mkdir()
        shared.mkdir()
        with tarfile.open(sdk, "r:gz") as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("Swift SDK archive escapes its directory")
                target = distribution / "sysroot" / Path(*path.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
                else:
                    raise ValueError("Swift SDK archive contains an unsupported member")
        for name, item in lock["licenses"].items():
            source = fetch("licenses/" + name, item, args.offline)
            shutil.copyfile(source, distribution / "licenses" / name)
        images = {}
        for name in ["swift-frontend", "wasm-ld"]:
            print("Separating " + name + " backend", flush=True)
            images[name] = split.create(ROOT, name, originals / (name + ".wasm"), work, lock)
            shutil.copyfile(work / (name + "-primary.wasm"), distribution / "bin" / (name + "-primary.wasm"))
            shutil.copyfile(work / (name + "-llvm.so.wasm"), shared / (name + "-llvm.so.wasm"))
            images[name]["selectionFile"] = name + "-selection.txt.gz"
            with (shared / images[name]["selectionFile"]).open("wb") as output:
                with gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as compressed:
                    with (work / (name + "-selection.txt")).open("rb") as source:
                        shutil.copyfileobj(source, compressed)
            entry = work / (name + ".wat")
            entry.write_text('(module (import "' + name + '-image" "_start" (func $entry)) (func (export "_start") call $entry))')
            subprocess.run(wasm_tools.command("parse", entry, "-o", distribution / "bin" / (name + ".wasm")), check=True)
        shutil.copytree(distribution / "licenses", shared / "licenses")
        provider = {"schema": 1, "kind": "shared-wasm-module", "profile": "swift-6.3.3-private-backend",
                    "abi": "exact-image-shared-function-tables", "images": images, "files": inventory(shared),
                    "source": lock["source"], "binaryen": lock["binaryen"], "recipeSha256": sha(ROOT / "split.py"),
                    "scope": "Private Swift compiler/linker backend modules derived from the pinned published monoliths. Includes LLVM, Clang, LLD, C/C++ runtime and unattributed support helpers. Primarys own their memory, data and tables. No generic LLVM C++ ABI or full source-build archive ownership inventory is claimed."}
        write_json(shared / "manifest.json", provider)
        manifest = {"schemaVersion": 1, "toolchain": "swift", "version": lock["version"], "bootstrap": lock,
                    "fullyWasmHostedBuild": True, "nodeRequired": False, "images": images,
                    "llvmProviderManifestSha256": sha(shared / "manifest.json"), "files": inventory(distribution),
                    "recipe": recipe()}
        write_json(distribution / "manifest.json", manifest)
        publish(shared, ROOT.parent / "llvm/dist/swift-shared")
        publish(distribution, ROOT / "dist")
    integrity()
    print("Swift compiler and external backends installed")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Swift bootstrap: {error}") from error
