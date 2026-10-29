#!/usr/bin/env python3
"""Bootstrap the pinned StarlingMonkey runtime and WASIp1 binding splicer."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

from toolchain import ROOT, CORE_HEADER, digest

PINS = ROOT / "checksums/starlingmonkey.json"
DEST = ROOT / ".toolchains/starlingmonkey"
PATCHES = [ROOT / "tools/starlingmonkey-spike/wasip1-lock.patch",
           ROOT / "tools/starlingmonkey-splicer-metadata.patch"]


def check():
    pins = json.loads(PINS.read_text())
    manifest = json.loads((DEST / "manifest.json").read_text())
    if set(manifest["files"]) != {"splicer.wasm", "runtime.wasm", "LICENSE"}:
        raise ValueError("incomplete StarlingMonkey compiler package")
    if manifest["pinsSha256"] != digest(PINS):
        raise ValueError("StarlingMonkey source pins changed; rebuild the tool package")
    if manifest["files"]["runtime.wasm"] != pins["runtime"]["sha256"]:
        raise ValueError("StarlingMonkey runtime pin mismatch")
    for name, expected in manifest["files"].items():
        path = DEST / name
        if path.parent != DEST or digest(path) != expected:
            raise ValueError("StarlingMonkey package integrity failure: " + name)
        if name.endswith(".wasm") and path.read_bytes()[:8] != CORE_HEADER:
            raise ValueError("StarlingMonkey compiler/runtime must be Core Wasm")
    return manifest


def download(spec, name, offline):
    cache = ROOT / ".toolchains/source-cache"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / name
    if not path.is_file() or digest(path) != spec["sha256"]:
        if offline:
            raise ValueError("pinned StarlingMonkey archive is unavailable offline: " + name)
        with urllib.request.urlopen(spec["url"], timeout=120) as response:
            data = response.read(spec["bytes"] + 1)
        if len(data) != spec["bytes"] or hashlib.sha256(data).hexdigest() != spec["sha256"]:
            raise ValueError("StarlingMonkey archive checksum mismatch: " + name)
        path.write_bytes(data)
    return path


def rebuild(offline=False):
    pins = json.loads(PINS.read_text())
    for path, key in zip(PATCHES, ("lockPatchSha256", "metadataPatchSha256")):
        if digest(path) != pins["splicer"][key]:
            raise ValueError("StarlingMonkey patch checksum mismatch")
    rustc = shutil.which("rustc")
    cargo = shutil.which("cargo")
    if not rustc or not cargo:
        raise ValueError("Rust/Cargo are required only to bootstrap the splicer package")
    identities = {}
    for name, path in (("rustc", rustc), ("cargo", cargo)):
        version = subprocess.check_output([path, "--version"], text=True).strip()
        if version != pins["splicer"]["nativeBootstrap"][name]["version"]:
            raise ValueError("StarlingMonkey bootstrap requires pinned " + name)
        identities[name] = {"version": version, "sha256": digest(Path(path).resolve())}
    source_archive = download(pins["source"], "componentize-js-source.tar.gz", offline)
    runtime_archive = download(pins["release"], "componentize-js-runtime.tgz", offline)
    scratch = ROOT / ".toolchains/starlingmonkey-build"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as temporary:
        work = Path(temporary)
        source = work / "source"
        source.mkdir()
        with tarfile.open(source_archive) as archive:
            archive.extractall(source, filter="data")
        source = next(source.iterdir())
        if digest(source / "Cargo.lock") != pins["source"]["cargoLockSha256"]:
            raise ValueError("upstream splicer Cargo.lock mismatch")
        for patch in PATCHES:
            subprocess.run(["patch", "--batch", "-p1", "-i", str(patch)], cwd=source, check=True)
        if digest(source / "Cargo.lock") != pins["splicer"]["cargoLockSha256"]:
            raise ValueError("patched splicer Cargo.lock mismatch")
        cargo_home = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo")).resolve()
        flags = [f"--remap-path-prefix={source}=/wisp/starlingmonkey",
                 f"--remap-path-prefix={cargo_home}=/cargo"]
        env = {**os.environ, "RUSTUP_TOOLCHAIN": "1.99.0", "RUSTC": rustc,
               "CARGO_BUILD_JOBS": "1", "CARGO_INCREMENTAL": "0",
               "CARGO_PROFILE_RELEASE_OPT_LEVEL": "1", "CARGO_PROFILE_RELEASE_DEBUG": "0",
               "CARGO_PROFILE_RELEASE_CODEGEN_UNITS": "1", "CARGO_PROFILE_RELEASE_LTO": "false",
               "CARGO_TARGET_DIR": str(scratch / "target"), "TMPDIR": str(work),
               "CARGO_ENCODED_RUSTFLAGS": "\x1f".join(flags)}
        env.pop("RUSTFLAGS", None)
        command = [cargo, "build", "--locked", "--release", "--target", "wasm32-wasip1",
                   "-p", "spidermonkey-embedding-splicer", "--bin", "splicer", "-j1"]
        if offline:
            command.append("--offline")
        subprocess.run(command, cwd=source, env=env, check=True)
        staged = work / "package"
        staged.mkdir()
        shutil.copyfile(scratch / "target/wasm32-wasip1/release/splicer.wasm", staged / "splicer.wasm")
        with tarfile.open(runtime_archive) as archive:
            (staged / "runtime.wasm").write_bytes(archive.extractfile(pins["runtime"]["archivePath"]).read())
            (staged / "LICENSE").write_bytes(archive.extractfile("package/LICENSE").read())
        if digest(staged / "runtime.wasm") != pins["runtime"]["sha256"]:
            raise ValueError("StarlingMonkey runtime checksum mismatch")
        manifest = {"schemaVersion": 1, "pinsSha256": digest(PINS),
                    "sourceCommit": pins["source"]["commit"], "bootstrap": identities,
                    "files": {p.name: digest(p) for p in sorted(staged.iterdir())},
                    "applicationCompiler": "WASIp1 splicer; native Rust only during tool bootstrap"}
        (staged / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        DEST.mkdir(parents=True, exist_ok=True)
        for path in staged.iterdir():
            os.replace(path, DEST / path.name)
    return check()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    if args.rebuild:
        result = rebuild(args.offline)
    else:
        try:
            result = check()
        except (OSError, ValueError):
            if args.check:
                raise
            result = rebuild(args.offline)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
