#!/usr/bin/env python3
"""Build Wisp's legacy WASI threads host with postloaded split modules."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(argv, **kwargs):
    subprocess.run([str(item) for item in argv], check=True, **kwargs)


def smoke(host, work):
    primary = work / "primary.wat"
    primary.write_text('''(module
      (type $answer (func (result i32)))
      (import "env" "memory" (memory 1 1 shared))
      (import "wasi" "thread-spawn" (func $spawn (param i32) (result i32)))
      (table (export "table") 1 funcref)
      (export "memory" (memory 0))
      (func (export "probe") (result i32)
        i32.const 0 call_indirect (type $answer))
      (func (export "wasi_thread_start") (param i32 i32)
        i32.const 0 i32.const 0 call_indirect (type $answer) i32.atomic.store
        i32.const 0 i32.const 1 memory.atomic.notify drop)
      (func (export "thread_probe") (result i32)
        i32.const 0 call $spawn drop
        i32.const 0 i32.const 0 i64.const 10000000000 memory.atomic.wait32 drop
        i32.const 0 i32.atomic.load))
''')
    provider = work / "provider.wat"
    provider.write_text('''(module
      (import "wisp-postload-test" "table" (table 1 funcref))
      (func $answer (result i32) i32.const 42)
      (elem (i32.const 0) $answer))
''')
    command = [str(host), "run", "-Sthreads=y"]
    missing = subprocess.run(command + ["--invoke", "probe", str(primary)],
                             text=True, capture_output=True, timeout=30)
    if missing.returncode == 0:
        raise RuntimeError("a missing split provider must reject execution")
    results = {}
    for function in ("probe", "thread_probe"):
        process = subprocess.run(command + ["--postload", f"wisp-postload-test={provider}",
                                           "--invoke", function, str(primary)],
                                 text=True, capture_output=True, timeout=30)
        if process.returncode or process.stdout.strip() != "42":
            raise RuntimeError(f"postload {function} failed: {process.stdout}{process.stderr}")
        results[function] = 42
    return {"missingProviderRejected": True, "mainTable": results["probe"],
            "spawnedThreadTable": results["thread_probe"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=ROOT / ".tools/shared-hosts/wisp-wasmtime-38.0.4")
    parser.add_argument("--cache", type=Path, default=ROOT / ".tools/shared-hosts/rust-host-build")
    parser.add_argument("--jobs", type=int, choices=range(1, 9), default=2)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    lock = json.loads((HERE / "rust-wasmtime.lock.json").read_text())
    patch = HERE / "rust-postload.patch"
    prefix, cache = args.prefix.resolve(), args.cache.resolve()
    prefix.mkdir(parents=True, exist_ok=True)
    host = prefix / "wasmtime"
    if args.check:
        receipt = json.loads((prefix / "receipt.json").read_text())
        if (receipt["source"] != lock or receipt["patchSha256"] != sha(patch)
                or receipt["binarySha256"] != sha(host)):
            raise RuntimeError("Wisp Rust execution host receipt or binary mismatch")
        with tempfile.TemporaryDirectory(prefix=".smoke-", dir=prefix) as temporary:
            proof = smoke(host, Path(temporary))
        print(json.dumps({"host": str(host), "smoke": proof}, indent=2))
        return
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".extract-", dir=cache) as staging:
        for item in lock["sources"]:
            name = f'{item["crate"]}-{lock["version"]}'
            archive = cache / f"{name}.crate"
            if not archive.exists():
                temporary = archive.with_suffix(".partial")
                try:
                    with urllib.request.urlopen(item["url"], timeout=120) as source, temporary.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    if sha(temporary) != item["sha256"]:
                        raise RuntimeError(f"source checksum mismatch: {name}")
                    temporary.replace(archive)
                finally:
                    temporary.unlink(missing_ok=True)
            if sha(archive) != item["sha256"]:
                raise RuntimeError(f"source cache checksum mismatch: {name}")
            with tarfile.open(archive) as stream:
                members = stream.getmembers()
                if any(not member.isfile() and not member.isdir() for member in members):
                    raise RuntimeError(f"unexpected source archive member: {name}")
                stream.extractall(staging, filter="data")
            destination = cache / name
            if destination.exists():
                shutil.rmtree(destination)
            (Path(staging) / name).replace(destination)
    run(["patch", "--batch", "--fuzz=0", "-p1", "-i", patch], cwd=cache)
    source = cache / f'wasmtime-cli-{lock["version"]}'
    # Only replace the registry entry with the checksum-verified patched crate.
    # Every other upstream lockfile version stays pinned.
    original_lock = tomllib.loads((source / "Cargo.lock").read_text())
    run(["cargo", f'+{lock["rustVersion"]}', "update", "-p", "wasmtime-wasi-threads"], cwd=source)
    patched_lock = tomllib.loads((source / "Cargo.lock").read_text())
    for value in (original_lock, patched_lock):
        value["package"] = [item for item in value["package"] if item["name"] != "wasmtime-wasi-threads"]
    if original_lock != patched_lock:
        raise RuntimeError("patching the threads crate changed unrelated dependency versions")
    environment = os.environ.copy()
    environment.update({"CARGO_TARGET_DIR": str(cache / "target"),
                        "CARGO_PROFILE_RELEASE_DEBUG": "0", "CARGO_PROFILE_RELEASE_OPT_LEVEL": "1",
                        "CARGO_PROFILE_RELEASE_STRIP": "symbols", "CARGO_PROFILE_RELEASE_INCREMENTAL": "false"})
    command = ["cargo", f'+{lock["rustVersion"]}', "build", "--locked", "--release", "--no-default-features",
               "--features", ",".join(lock["features"]), "--bin", "wasmtime", "--jobs", str(args.jobs)]
    run(command, cwd=source, env=environment)
    shutil.copy2(cache / "target/release/wasmtime", host)
    shutil.copy2(source / "LICENSE", prefix / "LICENSE")
    with tempfile.TemporaryDirectory(prefix=".smoke-", dir=prefix) as temporary:
        proof = smoke(host, Path(temporary))
    receipt = {"schema": 1, "owner": "wisp", "version": lock["version"], "source": lock,
               "patchSha256": sha(patch), "cargoLockSha256": sha(source / "Cargo.lock"),
               "binarySha256": sha(host), "command": command, "smoke": proof}
    (prefix / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"host": str(host), "smoke": proof}, indent=2))


if __name__ == "__main__":
    main()
