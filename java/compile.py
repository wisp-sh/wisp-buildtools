#!/usr/bin/env python3
"""Compile Java21 sources entirely in Wasm, through ECJ and the shared TeaVM backend."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT.parent))
for variable in ("WISP_JVM_TOOLCHAIN", "WISP_TEAVM_TOOLCHAIN", "WISP_LLVM_TOOLCHAIN"):
    if os.environ.get(variable):
        sys.path.insert(0, str(Path(os.environ[variable]).resolve().parent))
from teavm import compile as backend


def verified_dist():
    dist = ROOT / "dist"
    if dist.is_symlink():
        raise ValueError("Java distribution cannot be a symlink")
    manifest = json.loads((dist / "manifest.json").read_text())
    if manifest.get("toolchain") != "java" or not manifest.get("sha256"):
        raise ValueError("invalid Java distribution manifest; run java/bootstrap.py")
    if manifest.get("upstream") != json.loads((ROOT / "toolchain.lock.json").read_text()):
        raise ValueError("Java distribution uses a different toolchain lock; rebuild it")
    entries = list(dist.rglob("*"))
    if any(p.is_symlink() for p in entries):
        raise ValueError("Java distribution contains a symlink")
    actual = {str(p.relative_to(dist)) for p in entries if p.is_file() and p != dist / "manifest.json"}
    if set(manifest["sha256"]) != actual:
        raise ValueError("Java distribution file inventory differs from its manifest")
    for name, checksum in manifest["sha256"].items():
        if not isinstance(name, str) or "\\" in name or str(PurePosixPath(name)) != name or ".." in PurePosixPath(name).parts or PurePosixPath(name).is_absolute():
            raise ValueError("invalid Java distribution manifest path")
        path = dist / name
        if not path.resolve().is_relative_to(dist.resolve()) or backend.digest(path) != checksum:
            raise ValueError(f"Java distribution checksum mismatch: {name}")
    return dist, manifest


def build(sources, output, *, main, classpath=(), component=False, wit=None, world=None,
          adapter=None, timeout=1200):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive")
    if component and wit is None:
        raise ValueError("--component requires --wit")
    if not component and any(value is not None for value in [wit, world, adapter]):
        raise ValueError("WIT and adapter options require --component")
    deadline = time.monotonic() + timeout
    dist, manifest = verified_dist()
    backend_dist, backend_manifest = backend.verified_dist()
    files = []
    for source in sources:
        source = Path(source).resolve(strict=True)
        if source.is_dir():
            candidates = sorted(source.rglob("*.java"))
            if any(not p.resolve().is_relative_to(source) for p in candidates):
                raise ValueError("Java source directory contains a file outside its root")
            files.extend(candidates)
        elif source.suffix == ".java":
            files.append(source)
        else:
            raise ValueError(f"expected a Java source file or directory: {source}")
    files = sorted(set(files))
    if not files:
        raise ValueError("no Java source files found")
    source_root = Path(os.path.commonpath([p.parent for p in files]))
    dependencies = []
    for value in classpath:
        path = Path(value).resolve(strict=True)
        dependencies.append((path, backend.snapshot(path)[1] if path.is_dir() else None))
    output = Path(output).absolute()
    targets = [output, Path(str(output) + ".build.json")]
    if component:
        targets.append(Path(str(output) + ".wit"))
    if any(p.exists() or p.is_symlink() for p in targets):
        raise ValueError("output already exists; choose a new output path")
    output.parent.mkdir(parents=True, exist_ok=True)
    from jvm.runner import command, ROOT as JVM_ROOT
    with tempfile.TemporaryDirectory(prefix=".java-", dir=output.parent) as directory:
        work = Path(directory)
        records = backend.copy_snapshot(source_root, files, work / "sources")
        extra, extra_records = [], []
        staged_dependencies = []
        for index, (path, entries) in enumerate(dependencies):
            destination = work / "dependencies" / str(index)
            if entries is None:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
                record = {"path": str(path), "sha256": backend.digest(destination)}
            else:
                record = {"path": str(path), "files": backend.copy_snapshot(path, entries, destination)}
            extra.append(f"/project/dependencies/{index}")
            extra_records.append(record)
            staged_dependencies.append(destination)
        compile_classpath = ["/backend/java-sdk.jar", f"/backend/jars/teavm-interop-{backend_manifest['version']}.jar", *extra]
        arguments = ["/project/classes", ":".join(compile_classpath), "21", "/frontend/messages.properties",
                     *["/project/sources/" + str(p.relative_to(source_root)) for p in files]]
        invocation = command("wisp/java/EcjMain", [dist / "launcher", dist / "ecj.jar"],
                             mounts={dist: "/frontend", backend_dist: "/backend", work: "/project"},
                             arguments=arguments)
        jvm_record = {"manifestSha256": backend.digest(JVM_ROOT / "dist/manifest.json"),
                      "guestSha256": backend.digest(JVM_ROOT / "dist/jvm.wasm")}
        result = subprocess.run(invocation, capture_output=True, text=True,
                                timeout=max(0.01, deadline - time.monotonic()))
        if result.returncode:
            raise ValueError((result.stderr or result.stdout).strip() or "Java compiler failed")
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        candidate = work / "app.wasm"
        compiled = backend.build(work / "classes", candidate, main=main, classpath=staged_dependencies,
                                 component=component, wit=wit, world=world, adapter=adapter,
                                 timeout=max(0.01, deadline - time.monotonic()))
        compiled["output"]["path"] = str(output)
        if compiled.get("assembly"):
            compiled["assembly"]["output"]["path"] = str(output)
        receipt = {"schemaVersion": 1, "toolchain": "java", "version": manifest["version"],
                   "javaSourceLevel": "21", "main": main,
                   "sources": {"root": str(source_root), "files": records}, "classpath": extra_records,
                   "compiler": {"manifestSha256": backend.digest(dist / "manifest.json"), "command": invocation},
                   "jvm": jvm_record,
                   "sourceCompilationWasmHosted": True, "fullyWasmHostedBuild": True, "nodeRequired": False,
                   "backend": compiled, "output": compiled["output"]}
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
    parser.add_argument("sources", type=Path, nargs="+")
    parser.add_argument("--main", required=True)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--classpath", type=Path, action="append", default=[])
    parser.add_argument("--component", action="store_true")
    parser.add_argument("--wit", type=Path)
    parser.add_argument("--world")
    parser.add_argument("--preview1-adapter", type=Path)
    parser.add_argument("--timeout", type=float, default=1200)
    args = parser.parse_args()
    receipt = build(args.sources, args.output, main=args.main, classpath=args.classpath,
                    component=args.component, wit=args.wit, world=args.world,
                    adapter=args.preview1_adapter, timeout=args.timeout)
    print(f"{args.output} ({receipt['output']['bytes']} bytes)")


if __name__ == "__main__":
    try:
        main_cli()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"java: {error}")
