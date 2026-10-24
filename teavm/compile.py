#!/usr/bin/env python3
"""Compile JVM class files to standalone WASI Wasm using TeaVM in the Wasm JVM."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT.parent))
for variable in ("WISP_JVM_TOOLCHAIN", "WISP_TEAVM_TOOLCHAIN", "WISP_LLVM_TOOLCHAIN", "WISP_WASM_TOOLS_TOOLCHAIN"):
    if os.environ.get(variable):
        sys.path.insert(0, str(Path(os.environ[variable]).resolve().parent))


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verified_dist():
    dist = ROOT / "dist"
    if dist.is_symlink():
        raise ValueError("TeaVM distribution cannot be a symlink")
    manifest = json.loads((dist / "manifest.json").read_text())
    if manifest.get("toolchain") != "teavm" or not manifest.get("sha256"):
        raise ValueError("invalid TeaVM distribution manifest; run teavm/bootstrap.py")
    if manifest.get("upstream") != json.loads((ROOT / "toolchain.lock.json").read_text()):
        raise ValueError("TeaVM distribution uses a different toolchain lock; rebuild it")
    entries = list(dist.rglob("*"))
    if any(p.is_symlink() for p in entries):
        raise ValueError("TeaVM distribution contains a symlink")
    actual = {str(p.relative_to(dist)) for p in entries if p.is_file() and p != dist / "manifest.json"}
    if set(manifest["sha256"]) != actual:
        raise ValueError("TeaVM distribution file inventory differs from its manifest")
    for name, checksum in manifest["sha256"].items():
        if not isinstance(name, str) or "\\" in name or str(PurePosixPath(name)) != name or ".." in PurePosixPath(name).parts or PurePosixPath(name).is_absolute():
            raise ValueError("invalid TeaVM distribution manifest path")
        path = dist / name
        if not path.resolve().is_relative_to(dist.resolve()) or digest(path) != checksum:
            raise ValueError(f"TeaVM distribution checksum mismatch: {name}")
    return dist, manifest


def snapshot(directory):
    directory = directory.resolve(strict=True)
    if not directory.is_dir():
        raise ValueError(f"expected a class directory: {directory}")
    files = sorted(p for p in directory.rglob("*") if p.is_file())
    if any(not p.resolve().is_relative_to(directory) for p in files):
        raise ValueError("class directory contains a file outside its root")
    return directory, files


def copy_snapshot(source, files, target):
    records = []
    for path in files:
        relative = path.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        records.append({"path": str(relative), "sha256": digest(destination)})
    return records


def build(classes: Path, output: Path, *, main: str, classpath=(), component=False,
          wit=None, world=None, adapter=None, timeout=1200):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive")
    if not main or any(not part for part in main.split(".")) or re.search(r"[\s/\\;\[\]\0]", main):
        raise ValueError("--main must be a fully qualified JVM class name")
    if component and wit is None:
        raise ValueError("--component requires --wit")
    if not component and any(value is not None for value in [wit, world, adapter]):
        raise ValueError("WIT and adapter options require --component")
    deadline = time.monotonic() + timeout
    dist, manifest = verified_dist()
    from teavm.c_backend import verify_provider
    verify_provider(dist)
    source, files = snapshot(classes)
    if not any(path.suffix == ".class" for path in files):
        raise ValueError("class directory contains no JVM class files")
    dependencies = []
    for path in classpath:
        path = Path(path).resolve(strict=True)
        dependencies.append((path, snapshot(path)[1] if path.is_dir() else None))
    output = Path(output).absolute()
    targets = [output, Path(str(output) + ".build.json")]
    if component:
        targets.append(Path(str(output) + ".wit"))
    if any(p.exists() or p.is_symlink() for p in targets):
        raise ValueError("output already exists; choose a new output path")
    output.parent.mkdir(parents=True, exist_ok=True)
    from jvm.runner import command, ROOT as JVM_ROOT
    with tempfile.TemporaryDirectory(prefix=".teavm-", dir=output.parent) as directory:
        work = Path(directory)
        inputs = copy_snapshot(source, files, work / "classes")
        extra, extra_records = [], []
        for index, (path, entries) in enumerate(dependencies):
            target = work / "dependencies" / str(index)
            if entries is None:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                record = {"path": str(path), "sha256": digest(target)}
            else:
                record = {"path": str(path), "files": copy_snapshot(path, entries, target)}
            extra.append(f"/project/dependencies/{index}")
            extra_records.append(record)
        jars = sorted((dist / "jars").glob("*.jar"))
        from teavm.resources import prepare as prepare_resources
        resource_record = prepare_resources(dist, work / "resources")
        arguments = [main, "/project/output", "app.wasm", "/project/classes", "/project/resources",
                     *extra, *["/backend/jars/" + p.name for p in jars]]
        invocation = command("wisp/java/TeaVMMain", [dist / "launcher", work / "resources", *jars],
                             mounts={dist: "/backend", work: "/project"}, arguments=arguments)
        jvm_record = {"manifestSha256": digest(JVM_ROOT / "dist/manifest.json"),
                      "guestSha256": digest(JVM_ROOT / "dist/jvm.wasm")}
        from teavm import codegen_cache
        cache_inputs={"main":main,"classes":inputs,"dependencies":extra_records,
                      "compilerManifestSha256":digest(dist/"manifest.json"),"resources":resource_record,
                      "launcherSources":{name:digest(ROOT/name) for name in ["compile.py","resources.py"]}}
        cache=codegen_cache.target(os.environ.get("WISP_TEAVM_CODEGEN_CACHE"),cache_inputs)
        cached=codegen_cache.restore(cache,cache_inputs,work/"output")
        if cached is None:
            result = subprocess.run(invocation, capture_output=True, text=True, timeout=max(0.01, deadline - time.monotonic()))
            if result.returncode:
                raise ValueError((result.stderr or result.stdout).strip() or "TeaVM compiler failed")
            cached=codegen_cache.save(cache,cache_inputs,work/"output",invocation)
        cache_record={"hit":cached is not None and cached.get("command")!=invocation,
                      "path":str(cache) if cache else None,"record":cached}
        from teavm.c_backend import build as compile_c
        c_backend = compile_c(work, dist, component=component, timeout=max(0.01, deadline - time.monotonic()))
        core = work / "output/app.wasm"
        if core.read_bytes()[:8] != b"\0asm\x01\0\0\0":
            raise ValueError("TeaVM did not emit Core Wasm")
        candidate = work / "result.wasm"
        assembly = None
        if component:
            import importlib.util
            spec = importlib.util.spec_from_file_location("teavm_component", ROOT / "component.py")
            helper = importlib.util.module_from_spec(spec)
            sys.path.insert(0, str(ROOT))
            try:
                spec.loader.exec_module(helper)
                assembly = helper.build(core, Path(wit), candidate, world=world, adapter=adapter, timeout=max(0.01, deadline - time.monotonic()))
            finally:
                sys.path.pop(0)
            assembly["output"]["path"] = str(output)
        else:
            shutil.copyfile(core, candidate)
        receipt = {"schemaVersion": 1, "toolchain": "teavm", "version": manifest["version"],
                   "main": main, "inputs": {"classes": str(source), "files": inputs, "classpath": extra_records},
                   "compiler": {"manifestSha256": digest(dist / "manifest.json"), "command": invocation},
                   "jvm": jvm_record,
                   "cBackend": c_backend, "runtimeResources": resource_record, "codeGenerationCache":cache_record,
                   "sourceCompilationWasmHosted": True, "fullyWasmHostedBuild": True, "nodeRequired": False,
                   "coreSha256": digest(core), "assembly": assembly,
                   "output": {"path": str(output), "sha256": digest(candidate), "bytes": candidate.stat().st_size}}
        (work / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        publications = [(work / "receipt.json", targets[1])]
        if component:
            publications.append((Path(str(candidate) + ".wit"), targets[2]))
        publications.append((candidate, output))
        published = []
        try:
            for staged, destination in publications:
                os.link(staged, destination)
                published.append(destination)
        except OSError:
            for path in published:
                path.unlink(missing_ok=True)
            raise
    return receipt


def main_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("classes", type=Path)
    parser.add_argument("--main", required=True)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--classpath", type=Path, action="append", default=[])
    parser.add_argument("--component", action="store_true")
    parser.add_argument("--wit", type=Path)
    parser.add_argument("--world")
    parser.add_argument("--preview1-adapter", type=Path)
    parser.add_argument("--timeout", type=float, default=1200)
    args = parser.parse_args()
    receipt = build(args.classes, args.output, main=args.main, classpath=args.classpath,
                    component=args.component, wit=args.wit, world=args.world,
                    adapter=args.preview1_adapter, timeout=args.timeout)
    print(f"{args.output} ({receipt['output']['bytes']} bytes)")


if __name__ == "__main__":
    try:
        main_cli()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"teavm: {error}")
