#!/usr/bin/env python3
"""Download and verify the upstream WASI build of wasm-tools."""
from __future__ import annotations

import hashlib
import argparse
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def adapter(args, dist):
    """Install the common Preview 1 reactor adapter alongside the Wasm CLI."""
    lock = json.loads((ROOT / "adapter.lock.json").read_text())
    manifest_path = dist / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    pins = [lock["module"], lock["license"]]
    if (manifest.get("preview1Adapter") == lock and all(
            (dist / spec["file"]).is_file() and (dist / spec["file"]).stat().st_size == spec["size"]
            and digest(dist / spec["file"]) == spec["sha256"] for spec in pins)):
        return
    if args.check:
        raise SystemExit("shared Preview 1 adapter is missing or changed; rerun bootstrap")
    downloads = ROOT / ".downloads"
    downloads.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".adapter-", dir=ROOT) as directory:
        stage = Path(directory)
        for spec in pins:
            cached = downloads / (spec["sha256"] + "-" + Path(spec["file"]).name)
            if not cached.is_file() or digest(cached) != spec["sha256"]:
                if args.offline:
                    raise ValueError("offline bootstrap needs the verified cached Preview 1 adapter and license")
                with urllib.request.urlopen(spec["url"], timeout=60) as source:
                    data = source.read(spec["size"] + 1)
                if len(data) != spec["size"] or hashlib.sha256(data).hexdigest() != spec["sha256"]:
                    raise ValueError("shared Preview 1 adapter download checksum mismatch")
                cached.write_bytes(data)
            destination = stage / spec["file"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cached, destination)
        if (stage / lock["module"]["file"]).read_bytes()[:8] != b"\0asm\x01\0\0\0":
            raise ValueError("Preview 1 adapter must be a Core Wasm module")
        for spec in pins:
            destination = dist / spec["file"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            (stage / spec["file"]).replace(destination)
        manifest["preview1Adapter"] = lock
        pending = stage / "manifest.json"
        pending.write_text(json.dumps(manifest, indent=2) + "\n")
        pending.replace(manifest_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check the installed package without writing or downloading")
    parser.add_argument("--offline", action="store_true", help="use only an installed package or a verified cached archive")
    parser.add_argument("--json", action="store_true", help="print machine-readable status")
    args = parser.parse_args()
    lock = json.loads((ROOT / "toolchain.lock.json").read_text())
    dist = ROOT / "dist"
    def report():
        adapter(args, dist)
        if args.json:
            print(json.dumps({"ready": True, "version": lock["version"],
                              "module": str(dist / "wasm-tools.wasm"),
                              "sha256": lock["module_sha256"]}, sort_keys=True))
        else:
            print(f"WASI wasm-tools is ready: {dist}")
    ready = False
    try:
        manifest = json.loads((dist / "manifest.json").read_text())
        ready = (manifest["bootstrap"] == lock and manifest["sha256"] == lock["module_sha256"]
                 and digest(dist / "wasm-tools.wasm") == lock["module_sha256"])
    except (OSError, ValueError, KeyError):
        pass
    if ready:
        report()
        return
    if args.check:
        raise SystemExit("WASI wasm-tools package is missing or changed; rerun bootstrap")
    downloads = ROOT / ".downloads"
    downloads.mkdir(exist_ok=True)
    archive = downloads / Path(lock["url"]).name
    if not archive.exists() or digest(archive) != lock["sha256"]:
        if args.offline:
            raise SystemExit("offline bootstrap needs the verified cached wasm-tools archive")
        with tempfile.NamedTemporaryFile(dir=downloads, delete=False) as output:
            pending = Path(output.name)
            try:
                with urllib.request.urlopen(lock["url"], timeout=60) as response:
                    shutil.copyfileobj(response, output)
                output.close()
                if digest(pending) != lock["sha256"]:
                    raise ValueError("WASI wasm-tools download SHA-256 mismatch")
                pending.replace(archive)
            finally:
                pending.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bootstrap-", dir=ROOT) as directory:
        staging = Path(directory)
        with tarfile.open(archive) as source:
            matches = [member for member in source.getmembers()
                       if member.isfile() and Path(member.name).name in ("wasm-tools", "wasm-tools.wasm")]
            if len(matches) != 1:
                raise ValueError("expected exactly one upstream wasm-tools module")
            with source.extractfile(matches[0]) as incoming, (staging / "wasm-tools.wasm").open("wb") as output:
                shutil.copyfileobj(incoming, output)
        if (staging / "wasm-tools.wasm").read_bytes()[:8] != b"\0asm\x01\0\0\0":
            raise ValueError("upstream wasm-tools artifact is not Core Wasm")
        if digest(staging / "wasm-tools.wasm") != lock["module_sha256"]:
            raise ValueError("extracted WASI wasm-tools module SHA-256 mismatch")
        manifest = {"schema": 1, "bootstrap": lock, "compiler_host": "wasm32-wasip1",
                    "sha256": digest(staging / "wasm-tools.wasm")}
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        dist.mkdir(exist_ok=True)
        for path in staging.iterdir():
            path.replace(dist / path.name)
    report()


if __name__ == "__main__":
    main()
