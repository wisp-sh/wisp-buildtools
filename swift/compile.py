#!/usr/bin/env python3
"""Compile ordinary Swift with Wasm frontend, external backend and linker guests."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT.parent))
if os.environ.get("WISP_WASM_TOOLS_TOOLCHAIN"):
    sys.path.insert(0, str(Path(os.environ["WISP_WASM_TOOLS_TOOLCHAIN"]).resolve().parent))
if __package__:
    from .runtime import command, host_report, integrity, provider, sha
else:
    sys.path.insert(0, str(ROOT))
    from runtime import command, host_report, integrity, provider, sha
import wasm_tools


def run(argv, timeout):
    result = subprocess.run(list(map(str, argv)), capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip() or f"Swift compiler guest exited {result.returncode}: {argv[2:4]}")
    return result.stdout


def capture_wit(source, destination):
    base = source.parent if source.is_file() else source
    entries = [source] if source.is_file() else list(base.glob("*.wit"))
    if (base / "deps").is_dir():
        entries += list((base / "deps").rglob("*"))
    files = []
    for path in sorted(entries):
        if not path.is_file() or path.suffix not in {".wit", ".wasm"}:
            continue
        if not path.resolve().is_relative_to(base):
            raise ValueError("WIT dependency escapes its directory")
        target = destination / path.relative_to(base)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        files.append({"path": str(path), "sha256": sha(target)})
    if not files:
        raise ValueError("WIT input contains no contracts")
    return files


def build(sources, output, *, component=False, wit=None, world=None, adapter=None, timeout=600, whole_module=False, defines=(), module_paths=(), objects=(), minimal_imports=False):
    manifest = integrity()
    sources = [Path(path).resolve(strict=True) for path in sources]
    if not sources or len(set(sources)) != len(sources) or any(path.suffix != ".swift" for path in sources):
        raise ValueError("provide distinct Swift source files")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("--timeout must be positive")
    if component and wit is None:
        raise ValueError("--component requires --wit")
    if not component and (wit or world or adapter):
        raise ValueError("WIT and adapter options require --component")
    output = Path(output).absolute()
    targets = [output, Path(str(output) + ".build.json")]
    if component:
        targets.append(Path(str(output) + ".wit"))
    if any(path.exists() or path.is_symlink() for path in targets):
        raise ValueError("output already exists; choose a new output path")
    selected_host = host_report()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".swift-", dir=output.parent) as directory:
        work = Path(directory)
        (work / "src").mkdir()
        (work / "objects").mkdir()
        (work / "mcache").mkdir()
        source_records, guests = [], []
        for i, source in enumerate(sources):
            target = work / "src" / str(i) / source.name
            target.parent.mkdir()
            shutil.copyfile(source, target)
            source_records.append({"path": str(source), "sha256": sha(target)})
            guests.append(f"/work/src/{i}/" + target.name)
        if component:
            shutil.copyfile(ROOT / "src/adapter.swift", work / "src/adapter.swift")
            guests.append("/work/src/adapter.swift")
        mounts = [(work, "/work"), (ROOT / "dist/sysroot", "/sysroot")]
        dependencies = []
        for index, directory in enumerate(module_paths):
            directory = Path(directory).resolve(strict=True)
            target = work / "modules" / str(index)
            shutil.copytree(directory, target)
            dependencies.extend({"path": str(p), "sha256": sha(p)} for p in sorted(directory.rglob("*")) if p.is_file())
        linked_objects = []
        for index, path in enumerate(objects):
            path = Path(path).resolve(strict=True)
            target = work / "objects" / f"dependency-{index}.o"
            shutil.copyfile(path, target)
            dependencies.append({"path": str(path), "sha256": sha(path)})
            linked_objects.append(f"/work/objects/dependency-{index}.o")
        commands = []
        for i, primary in enumerate(guests[:1] if whole_module else guests):
            arguments = ["-frontend", "-c",
                         *(["-whole-module-optimization", *guests] if whole_module else
                           ["-primary-file", primary, *(path for path in guests if path != primary)]),
                         *(item for define in defines for item in ["-D", define]),
                         *(item for i in range(len(module_paths)) for item in ["-I", f"/work/modules/{i}"]),
                         *(["-disable-implicit-concurrency-module-import", "-disable-implicit-string-processing-module-import"] if minimal_imports else []),
                         "-target", "wasm32-unknown-wasip1", "-disable-objc-interop",
                         "-sdk", "/sysroot/wasi-sysroot", "-resource-dir", "/sysroot/swift/lib/swift_static",
                         "-use-static-resource-dir", "-module-cache-path", "/work/mcache",
                         "-no-color-diagnostics", "-empty-abi-descriptor", "-module-name", "main",
                         "-o", f"/work/objects/{i}.o"]
            if component:
                arguments.append("-parse-as-library")
            argv = command("swift-frontend", arguments, mounts)
            run(argv, timeout)
            commands.append(argv)
        startup = "crt1-reactor.o" if component else "crt1-command.o"
        arguments = ["-m", "wasm32", "-L/sysroot/swift/lib/swift_static/wasi",
                     "-L/sysroot/wasi-sysroot/lib/wasm32-wasip1",
                     "/sysroot/wasi-sysroot/lib/wasm32-wasip1/" + startup,
                     "/sysroot/swift/lib/swift_static/wasi/wasm32/swiftrt.o",
                     *(f"/work/objects/{i}.o" for i in range(1 if whole_module else len(guests))),
                     *linked_objects,
                     "-lswiftSwiftOnoneSupport", "-lswiftCore", "-lswift_Concurrency",
                     "-lswift_StringProcessing", "-lswift_RegexParser", "-ldl", "-lc++", "-lc++abi", "-lm",
                     "-lwasi-emulated-mman", "-lwasi-emulated-signal", "-lwasi-emulated-process-clocks",
                     "--error-limit=0", "--threads=1", "--global-base=4096", "--table-base=4096",
                     "-z", "stack-size=131072", "-lc",
                     "/sysroot/swift/lib/swift_static/clang/lib/wasip1/libclang_rt.builtins-wasm32.a",
                     "-o", "/work/core.wasm"]
        if component:
            arguments += ["--no-entry", "--export=_initialize", "--export=cabi_realloc_adapter"]
        argv = command("wasm-ld", arguments, mounts)
        run(argv, timeout)
        commands.append(argv)
        candidate = work / "core.wasm"
        assembly = None
        if component:
            wit = Path(wit).resolve(strict=True)
            wit_records = capture_wit(wit, work / "wit")
            selected = work / "wit" / wit.name if wit.is_file() else work / "wit"
            arguments = ["component", "embed", selected, candidate, "-o", work / "embedded.wasm"]
            if world:
                arguments += ["--world", world]
            run(wasm_tools.command(*arguments), timeout)
            adapter = wasm_tools.preview1_adapter(adapter)
            run(wasm_tools.command("component", "new", work / "embedded.wasm", "--adapt",
                                   f"wasi_snapshot_preview1={adapter}", "-o", work / "component.wasm"), timeout)
            candidate = work / "component.wasm"
            (work / "component.wit").write_text(run(wasm_tools.command("component", "wit", candidate), timeout))
            assembly = {"wit": wit_records, "world": world, "adapterSha256": sha(adapter),
                        "initialization": "WASI reactor CRT _initialize",
                        "adapterShimSha256": sha(work / "src/adapter.swift"),
                        "adapterAllocator": "cabi_realloc_adapter using Swift's shared libc heap"}
        run(wasm_tools.command("validate", candidate), timeout)
        record = {"schemaVersion": 1, "toolchain": "swift", "version": manifest["version"],
                  "sources": source_records, "fullyWasmHostedBuild": True, "nodeRequired": False,
                  "compilerManifestSha256": sha(ROOT / "dist/manifest.json"),
                  "llvmProviderManifestSha256": sha(provider() / "manifest.json"),
                  "host": selected_host,
                  "commands": commands, "component": component, "assembly": assembly,
                  "wholeModule": whole_module, "defines": list(defines), "dependencies": dependencies, "minimalImports": minimal_imports,
                  "output": {"path": str(output), "bytes": candidate.stat().st_size, "sha256": sha(candidate)}}
        (work / "receipt.json").write_text(json.dumps(record, indent=2) + "\n")
        staged = [(work / "receipt.json", targets[1])]
        if component:
            staged.append((work / "component.wit", targets[2]))
        staged.append((candidate, targets[0]))
        published = []
        try:
            for source, target in staged:
                os.link(source, target)
                published.append(target)
        except OSError:
            for path in published:
                path.unlink(missing_ok=True)
            raise
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, nargs="+")
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--component", action="store_true")
    parser.add_argument("--wit", type=Path)
    parser.add_argument("--world")
    parser.add_argument("--preview1-adapter", type=Path)
    parser.add_argument("--minimal-imports", action="store_true", help="omit implicit concurrency and regex modules")
    parser.add_argument("--module-search-path", action="append", default=[], type=Path, help="directory of precompiled Swift modules")
    parser.add_argument("--link-object", action="append", default=[], type=Path, help="precompiled target object to link")
    parser.add_argument("--whole-module", action="store_true", help="compile all files in one frontend invocation")
    parser.add_argument("--define", action="append", default=[], help="Swift conditional compilation flag")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()
    report = build(args.source, args.output, component=args.component, wit=args.wit,
                   world=args.world, adapter=args.preview1_adapter, timeout=args.timeout, whole_module=args.whole_module, defines=args.define, module_paths=args.module_search_path, objects=args.link_object, minimal_imports=args.minimal_imports)
    print(f"{args.output} ({report['output']['bytes']} bytes)")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Swift compile: {error}") from error
