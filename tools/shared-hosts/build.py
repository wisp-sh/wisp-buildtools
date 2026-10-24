#!/usr/bin/env python3
"""Build Wisp's shared Wasmtime host with external core-module imports."""
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

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(argv, **kwargs):
    subprocess.run([str(arg) for arg in argv], check=True, **kwargs)


def smoke(host, work):
    provider = work / "provider.wat"
    provider.write_text('(module (func (export "answer") (result i32) i32.const 42))\n')
    consumer = work / "consumer.wat"
    consumer.write_text('''(component
      (core type $provider (module (export "answer" (func (result i32)))))
      (import "wisp-test-provider" (core module $provider (type $provider)))
      (core instance $instance (instantiate $provider))
      (alias core export $instance "answer" (core func $answer))
      (func (export "answer") (result s32) (canon lift (core func $answer)))
    )\n''')
    failed = subprocess.run([str(host), "run", "--invoke", "answer()", str(consumer)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if failed.returncode == 0 or "wisp-test-provider" not in failed.stderr:
        raise RuntimeError("missing shared provider must fail before guest execution")
    result = subprocess.check_output([str(host), "run", "--preload", f"wisp-test-provider={provider}",
                                      "--invoke", "answer()", str(consumer)], text=True)
    if result.strip() != "42":
        raise RuntimeError(f"shared provider returned {result!r}")
    return {"externalModuleImport": "wisp-test-provider", "value": 42,
            "missingProviderRejected": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=ROOT / ".tools/shared-hosts/wisp-wasmtime-49.0.1")
    parser.add_argument("--cache", type=Path, default=ROOT / ".tools/shared-hosts/host-build")
    parser.add_argument("--jobs", type=int, choices=range(1, 9), default=2)
    parser.add_argument("--check", action="store_true", help="verify the installed host and its import smoke test")
    args = parser.parse_args()
    lock = json.loads((HERE / "wasmtime.lock.json").read_text())
    prefix = args.prefix.resolve()
    cache = args.cache.resolve()
    prefix.mkdir(parents=True, exist_ok=True)
    if args.check:
        receipt = json.loads((prefix / "receipt.json").read_text())
        host = prefix / "wasmtime"
        if (receipt["source"] != lock or receipt["patchSha256"] != sha(HERE / "component-preload.patch")
                or receipt["binarySha256"] != sha(host)):
            raise RuntimeError("Wisp execution host receipt or binary mismatch")
        with tempfile.TemporaryDirectory(prefix=".smoke-", dir=prefix) as directory:
            proof = smoke(host, Path(directory))
        print(json.dumps({"host": str(host), "sha256": receipt["binarySha256"], "smoke": proof}, indent=2))
        return
    downloads = cache / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / f'wasmtime-cli-{lock["version"]}.crate'
    if not archive.exists():
        temporary = archive.with_suffix(".partial")
        try:
            with urllib.request.urlopen(lock["url"], timeout=120) as source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output)
            if sha(temporary) != lock["sha256"]:
                raise RuntimeError("Wasmtime source checksum mismatch")
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    if sha(archive) != lock["sha256"]:
        raise RuntimeError("Wasmtime source cache checksum mismatch")
    sources = cache / "source"
    sources.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".extract-", dir=sources) as staging:
        with tarfile.open(archive) as stream:
            members = stream.getmembers()
            if any(not member.isfile() and not member.isdir() for member in members):
                raise RuntimeError("unexpected Wasmtime source archive member")
            stream.extractall(staging, filter="data")
        source = sources / f'wasmtime-cli-{lock["version"]}'
        if source.exists():
            shutil.rmtree(source)
        (Path(staging) / source.name).replace(source)
    patch = HERE / "component-preload.patch"
    run(["patch", "--batch", "--fuzz=0", "-p1", "-i", patch], cwd=source)
    environment = os.environ.copy()
    environment.update({"CARGO_TARGET_DIR": str(cache / "target"),
                        "CARGO_PROFILE_RELEASE_DEBUG": "0", "CARGO_PROFILE_RELEASE_OPT_LEVEL": "1",
                        "CARGO_PROFILE_RELEASE_STRIP": "symbols", "CARGO_PROFILE_RELEASE_INCREMENTAL": "false"})
    argv = ["cargo", f'+{lock["rustVersion"]}', "build", "--locked", "--release", "--no-default-features",
            "--features", ",".join(lock["features"]), "--bin", "wasmtime", "--jobs", str(args.jobs)]
    run(argv, cwd=source, env=environment)
    host = prefix / "wasmtime"
    shutil.copy2(cache / "target/release/wasmtime", host)
    shutil.copy2(source / "LICENSE", prefix / "LICENSE")
    with tempfile.TemporaryDirectory(prefix=".smoke-", dir=prefix) as directory:
        proof = smoke(host, Path(directory))
    receipt = {"schema": 1, "owner": "wisp", "version": lock["version"],
               "source": lock, "patchSha256": sha(patch), "cargoLockSha256": sha(source / "Cargo.lock"),
               "binarySha256": sha(host), "command": argv, "smoke": proof}
    (prefix / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"host": str(host), "sha256": receipt["binarySha256"], "smoke": proof}, indent=2))


if __name__ == "__main__":
    main()
