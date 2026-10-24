#!/usr/bin/env python3
"""Compile Grain source inside the Wisp-provided Wasmtime host."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def runtime():
    candidate = os.environ.get("WISP_WASMTIME", "wasmtime")
    executable = shutil.which(candidate)
    if not executable:
        raise ValueError("Wisp must provide Wasmtime; set WISP_WASMTIME")
    return Path(executable).resolve()


def integrity():
    lock = json.loads((ROOT / "toolchain.lock.json").read_text())
    manifest = json.loads((ROOT / "dist/manifest.json").read_text())
    if manifest.get("bootstrap") != lock or not manifest.get("fullyWasmHostedBuild"):
        raise ValueError("Grain distribution does not match toolchain.lock.json; run bootstrap.py")
    files = manifest.get("sha256", {})
    required = {"grainc.wasm", "grainc.js", "runtime.js", "stdlib/pervasives.gr"}
    if not required.issubset(files):
        raise ValueError("Grain distribution is incomplete")
    for name, expected in files.items():
        path = ROOT / "dist" / name
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT / "dist") or digest(path) != expected:
            raise ValueError(f"Grain artifact is missing or changed: {name}")
    if files["grainc.js"] != lock["compiler"]["javascript"]["sha256"]:
        raise ValueError("Grain compiler does not match its upstream checksum")
    return manifest


def copy_sources(source, target):
    # Snapshot before writing. An output directory may be inside the source tree.
    paths = sorted(path for path in source.rglob("*.gr")
                   if not path.is_relative_to(target.parent)
                   and not any(part.startswith(".grain-") for part in path.relative_to(source).parts[:-1]))
    target.mkdir(parents=True, exist_ok=True)
    captured = {}
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError(f"Grain source symlinks are unsupported: {path}")
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        captured[path.relative_to(source).as_posix()] = digest(destination)
    return captured


def build(source, output, *, project_root=None, component=False, wit=None, world=None,
          adapter=None, release=False, use_start_section=False, cache_dir=None,
          timeout=600, grain_args=None):
    manifest = integrity()
    source, output = Path(source).resolve(strict=True), Path(output).absolute()
    project = Path(project_root).resolve(strict=True) if project_root else source.parent
    if source.suffix != ".gr" or not source.is_file() or not source.is_relative_to(project):
        raise ValueError("source must be a .gr file inside --project-root")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("--timeout must be positive")
    if component and wit is None:
        raise ValueError("--component requires --wit")
    if not component and (wit is not None or world is not None or adapter is not None):
        raise ValueError("--wit, --world and --preview1-adapter require --component")
    if component and use_start_section:
        raise ValueError("Component initialization uses _initialize; omit --use-start-section")
    receipt_path = Path(str(output) + ".build.json")
    targets = [output, receipt_path, *([Path(str(output) + ".wit")] if component else [])]
    if any(path.exists() or path.is_symlink() for path in targets):
        raise ValueError("output already exists; choose a new output path")
    output.parent.mkdir(parents=True, exist_ok=True)
    host = runtime()
    dist = ROOT / "dist"
    guest = dist / "grainc.wasm"
    base = [str(host), "run", "-C", "cache=y", "-O", "opt-level=0", "-W", "max-wasm-stack=16777216"]
    compiler = dist / "grainc.js"
    cache = None
    if cache_dir is not None:
        key = hashlib.sha256((manifest["sha256"]["grainc.wasm"] + manifest["sha256"]["grainc.js"]).encode()).hexdigest()
        cache = Path(cache_dir).absolute() / key
        cache.mkdir(parents=True, exist_ok=True)
        bytecode = cache / "grainc.qbc"
        record = cache / "bytecode.json"
        ready = False
        try:
            cached = json.loads(record.read_text())
            ready = (cached["compiler"] == manifest["sha256"]["grainc.js"]
                     and cached["guest"] == manifest["sha256"]["grainc.wasm"]
                     and cached["bytes"] == bytecode.stat().st_size
                     and cached["sha256"] == digest(bytecode))
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if not ready:
            with tempfile.TemporaryDirectory(prefix=".bytecode-", dir=cache) as directory:
                work = Path(directory)
                command = [*base, "--dir", f"{dist}::/toolchain", "--dir", f"{work}::/cache", str(guest),
                           "--compile-bytecode", "/toolchain/grainc.js", "/cache/grainc.qbc"]
                subprocess.run(command, check=True, timeout=timeout)
                generated = work / "grainc.qbc"
                metadata = {"sha256": digest(generated), "bytes": generated.stat().st_size,
                            "compiler": manifest["sha256"]["grainc.js"], "guest": manifest["sha256"]["grainc.wasm"]}
                (work / "bytecode.json").write_text(json.dumps(metadata, indent=2) + "\n")
                os.replace(generated, bytecode)
                os.replace(work / "bytecode.json", record)
        compiler = bytecode
    with tempfile.TemporaryDirectory(prefix=".grain-", dir=output.parent) as directory:
        work = Path(directory)
        source_files = copy_sources(project, work / "project")
        copy_sources(dist / "stdlib", work / "stdlib")
        stdlib_cache = None
        if cache:
            cache_inputs = {"runtime": manifest["sha256"]["runtime.js"], "release": release,
                            "useStartSection": use_start_section,
                            "componentMemoryBase": 196608 if component else None,
                            "arguments": grain_args or [],
                            "stdlib": {name: value for name, value in manifest["sha256"].items() if name.startswith("stdlib/")}}
            cache_key = hashlib.sha256(json.dumps(cache_inputs, sort_keys=True).encode()).hexdigest()
            stdlib_cache = cache / "stdlib" / f"{cache_key}.json"
            try:
                cached_record = json.loads(stdlib_cache.read_text())
                if cached_record["inputs"] != cache_inputs:
                    raise ValueError("cached Grain library inputs changed")
                cached_files = cached_record["files"]
                if not isinstance(cached_files, dict):
                    raise ValueError("invalid cached Grain library inventory")
                verified = []
                for name, expected in cached_files.items():
                    relative = Path(name)
                    if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".gro":
                        raise ValueError("invalid cached Grain library path")
                    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
                        raise ValueError("invalid cached Grain library hash")
                    blob = cache / "objects" / expected
                    contents = blob.read_bytes()
                    if hashlib.sha256(contents).hexdigest() != expected:
                        raise ValueError("cached Grain library checksum mismatch")
                    verified.append((relative, contents))
                for relative, contents in verified:
                    destination = work / "stdlib" / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(contents)
            except (OSError, ValueError, KeyError, TypeError):
                # An interrupted or stale cache entry falls back to source compilation.
                pass
        mounts = ["--dir", f"{dist}::/toolchain", "--dir", f"{work}::/work"]
        selected = "/toolchain/grainc.js"
        if cache:
            mounts += ["--dir", f"{cache}::/compiler-cache"]
            selected = "/compiler-cache/grainc.qbc"
        arguments = ["/work/project/" + source.relative_to(project).as_posix(),
                     "--stdlib", "/work/stdlib", "-o", "/work/output.wasm"]
        if release:
            arguments += ["--profile", "release"]
        if component:
            # Keep the reactor adapter's two pages outside Grain's independent heap.
            arguments += ["--memory-base", "196608"]
        if use_start_section:
            arguments.append("--use-start-section")
        command = [*base, *mounts, str(guest), "/toolchain/runtime.js", selected, *arguments, *(grain_args or [])]
        subprocess.run(command, check=True, timeout=timeout)
        candidate = work / "output.wasm"
        if not candidate.is_file() or candidate.read_bytes()[:8] != b"\0asm\x01\0\0\0":
            raise ValueError("Grain did not emit a Core Wasm module")
        if stdlib_cache:
            objects = cache / "objects"
            objects.mkdir(parents=True, exist_ok=True)
            inventory = {}
            for path in (work / "stdlib").rglob("*.gro"):
                contents = path.read_bytes()
                checksum = hashlib.sha256(contents).hexdigest()
                target = objects / checksum
                with tempfile.NamedTemporaryFile(prefix=".object-", dir=objects, delete=False) as staged:
                    temporary = Path(staged.name)
                    staged.write(contents)
                try:
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
                inventory[path.relative_to(work / "stdlib").as_posix()] = checksum
            stdlib_cache.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix=".inventory-", dir=stdlib_cache.parent, mode="w", delete=False) as staged:
                temporary = Path(staged.name)
                json.dump({"inputs": cache_inputs, "files": inventory}, staged, sort_keys=True)
                staged.write("\n")
            try:
                os.replace(temporary, stdlib_cache)
            finally:
                temporary.unlink(missing_ok=True)
        metadata = {"schemaVersion": 1, "toolchain": "grain", "version": manifest["version"],
                    "source": {"path": str(source), "sha256": source_files[source.relative_to(project).as_posix()],
                               "projectRoot": str(project), "files": source_files},
                    "compiler": {"guestSha256": manifest["sha256"]["grainc.wasm"],
                                 "javascriptSha256": manifest["sha256"]["grainc.js"]},
                    "fullyWasmHostedBuild": True, "nodeRequired": False,
                    "host": {"provider": "wisp", "wasmtime": str(host)},
                    "release": release, "component": component,
                    "output": {"path": str(output), "bytes": candidate.stat().st_size, "sha256": digest(candidate)}}
        if component:
            from component import build as assemble
            component_file = work / "component.wasm"
            assembled = assemble(candidate, Path(wit), component_file, world=world, adapter=adapter,
                                 initialize=True, reserve_adapter=True, timeout=timeout)
            assembled["output"]["path"] = str(output)
            metadata["output"] = assembled["output"]
            metadata["assembly"] = assembled
            (work / "receipt.json").write_text(json.dumps(metadata, indent=2) + "\n")
            published = []
            try:
                for staged, target in [(Path(str(component_file) + ".wit"), Path(str(output) + ".wit")),
                                       (work / "receipt.json", receipt_path), (component_file, output)]:
                    os.link(staged, target)
                    published.append(target)
            except OSError:
                for path in published:
                    path.unlink(missing_ok=True)
                raise
        else:
            (work / "receipt.json").write_text(json.dumps(metadata, indent=2) + "\n")
            os.link(work / "receipt.json", receipt_path)
            try:
                os.link(candidate, output)
            except OSError:
                receipt_path.unlink(missing_ok=True)
                raise
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--component", action="store_true")
    parser.add_argument("--wit", type=Path)
    parser.add_argument("--world")
    parser.add_argument("--preview1-adapter", type=Path)
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--use-start-section", action="store_true")
    parser.add_argument("--cache-dir", type=Path, default=os.environ.get("WISP_GRAIN_CACHE"),
                        help="optional external cache for QuickJS bytecode and compiled Grain libraries")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--grain-arg", action="append", default=[])
    args = parser.parse_args()
    try:
        report = build(args.source, args.output, project_root=args.project_root, component=args.component,
                       wit=args.wit, world=args.world, adapter=args.preview1_adapter, release=args.release,
                       use_start_section=args.use_start_section, cache_dir=args.cache_dir,
                       timeout=args.timeout, grain_args=args.grain_arg)
        print(f"{args.output} ({report['output']['bytes']} bytes)")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Grain compile: {error}") from error


if __name__ == "__main__":
    main()
