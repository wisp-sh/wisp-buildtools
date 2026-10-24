#!/usr/bin/env python3
"""Install or rebuild the JS/TS Wasm compiler guests. No Node or npm is used."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

from toolchain import ROOT, check_integrity, digest, load_manifest, tool_path, wasmtime_path


def run(command, **kwargs):
    subprocess.run([str(value) for value in command], check=True, **kwargs)


def runtimes(destination, offline=False):
    spec = load_manifest()["componentizeQjs"]
    cache = ROOT / ".toolchains/source-cache"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"componentize-qjs-{spec['version']}.crate"
    if not archive.is_file() or digest(archive) != spec["sha256"]:
        if offline:
            raise ValueError("pinned QuickJS runtime archive is not cached for offline bootstrap")
        with urllib.request.urlopen(spec["url"], timeout=90) as response:
            contents = response.read(64 * 1024 * 1024 + 1)
        if hashlib.sha256(contents).hexdigest() != spec["sha256"]:
            raise ValueError("QuickJS runtime archive checksum mismatch")
        archive.write_bytes(contents)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as bundle:
        for filename, expected in spec["runtimes"].items():
            member = f"componentize-qjs-{spec['version']}/prebuilt/{filename}"
            path = destination / filename
            path.write_bytes(bundle.extractfile(member).read())
            if path.stat().st_size != expected["bytes"] or digest(path) != expected["sha256"]:
                raise ValueError(f"QuickJS runtime checksum mismatch: {filename}")


def rebuild(args):
    if not shutil.which("cargo"):
        raise ValueError("Rust/Cargo is needed only to bootstrap the Wasm componentizer")
    rustc_details = subprocess.check_output(["rustc", "--version", "--verbose"], text=True).splitlines()
    rustc = rustc_details[0]
    commit = next(line.removeprefix("commit-hash: ") for line in rustc_details
                  if line.startswith("commit-hash: "))
    sysroot = Path(subprocess.check_output(["rustc", "--print", "sysroot"], text=True).strip()).resolve()
    standard_sources = sysroot / "lib/rustlib/src/rust/library"
    expected_rustc = load_manifest().get("bootstrapRustc")
    if expected_rustc and rustc != expected_rustc:
        raise ValueError(f"compiler guest bootstrap requires {expected_rustc}; found {rustc}")
    (ROOT / ".toolchains").mkdir(exist_ok=True)
    guest = ROOT / "tools/componentize-wasi"
    target = ROOT / ".toolchains/cargo-target"
    cargo_home = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo")).resolve()
    # Rust resolves standard-library source paths locally when rust-src is
    # installed. Normalize those paths so either installation emits the same guest.
    env = {**os.environ, "CARGO_TARGET_DIR": str(target), "CARGO_BUILD_JOBS": "2",
           "RUSTFLAGS": f"--remap-path-prefix={ROOT}=/wisp/javascript "
                        f"--remap-path-prefix={cargo_home / 'registry'}=/cargo/registry "
                        f"--remap-path-prefix={standard_sources}=/rustc/{commit}/library"}
    env.pop("CARGO_ENCODED_RUSTFLAGS", None)
    command = ["cargo", "build", "--manifest-path", guest / "Cargo.toml", "--release",
               "--locked", "--target", "wasm32-wasip1"]
    if args.offline:
        command.append("--offline")
    run(command, env=env)
    with tempfile.TemporaryDirectory(prefix=".bootstrap-", dir=ROOT / ".toolchains") as directory:
        staging = Path(directory)
        runtimes(staging / ".toolchains/runtimes", args.offline)
        componentizer = staging / ".toolchains/wasm/componentize.wasm"
        componentizer.parent.mkdir(parents=True)
        shutil.copyfile(target / "wasm32-wasip1/release/wisp-javascript-componentize.wasm", componentizer)
        command = [sys.executable, ROOT / "tools/build-esbuild-wasi.py", "--output",
                   staging / ".toolchains/wasm/esbuild.wasm", "--receipt",
                   staging / ".toolchains/esbuild-build.json", "--go", args.go]
        if args.offline:
            command.append("--offline")
        run(command, stdout=subprocess.DEVNULL if args.json else None)
        # A failed compiler build or checksum check never changes the installed
        # guests. Replacements are complete, pinned files before publication.
        check_integrity(artifact_root=staging)
        for relative in [*load_manifest()["files"], ".toolchains/esbuild-build.json"]:
            source, destination = staging / relative, ROOT / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify installed inputs without rebuilding")
    parser.add_argument("--rebuild", action="store_true", dest="rebuild",
                        help="bootstrap compiler Wasm with native Rust and Go, then verify pinned artifacts")
    parser.add_argument("--offline", action="store_true", help="use cached upstream sources and Cargo dependencies")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--go", default=os.environ.get("WISP_JAVASCRIPT_BOOTSTRAP_GO", "go"))
    args = parser.parse_args()
    if args.check and args.rebuild:
        parser.error("--check and --rebuild cannot be combined")
    try:
        if args.rebuild:
            rebuild(args)
        else:
            try:
                check_integrity()
            except ValueError:
                if args.check:
                    raise
                rebuild(args)
        shared_command = [sys.executable, ROOT.parent / "wasm-tools/bootstrap.py"]
        if args.check:
            shared_command.append("--check")
        if args.offline:
            shared_command.append("--offline")
        run(shared_command, stdout=subprocess.DEVNULL if args.json else None)
        report = check_integrity()
        report["wasmtime"] = str(wasmtime_path())
        report["fullyWasmHostedBuild"] = True
        report["nativeBootstrapOnly"] = "Rust and Go rebuild the compiler guests; source builds use only Wasmtime"
        if not args.check:
            receipt = ROOT / ".toolchains/bootstrap-receipt.json"
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2) if args.json else "Verified JS/TS Wasm compiler guests; Node is not required.")
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as error:
        raise SystemExit(f"bootstrap: {error}") from error


if __name__ == "__main__":
    main()
