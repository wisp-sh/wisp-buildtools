#!/usr/bin/env python3
"""Compile JS/TS and WIT using Wasm compiler, binding-generator and linker guests."""
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

from toolchain import ROOT, COMPONENT_HEADER, check_integrity, digest, run_guest, tool_path, wasmtime_path
from typescript import bundle

sys.path.insert(0, str(ROOT.parent))
if os.environ.get("WISP_WASM_TOOLS_TOOLCHAIN"):
    sys.path.insert(0, str(Path(os.environ["WISP_WASM_TOOLS_TOOLCHAIN"]).resolve().parent))
import wasm_tools


def wasm_tool(*arguments):
    result = subprocess.run(wasm_tools.command(*arguments), text=True, capture_output=True, timeout=120)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip())
    return result.stdout


def publish(staged, targets):
    """Publish complete files exclusively, with the Component as the commit marker."""
    pending_files, published = [], []
    try:
        for source, target in zip(staged, targets):
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix=f".{target.name}.", suffix=".tmp",
                                             dir=target.parent, delete=False) as destination:
                pending = Path(destination.name)
                pending_files.append((pending, target))
                with source.open("rb") as contents:
                    shutil.copyfileobj(contents, destination)
                destination.flush()
                os.fsync(destination.fileno())
        # Each hard link publishes an already complete file and fails if a
        # concurrent build has claimed the destination. Publish the module last.
        for pending, target in [*pending_files[1:], pending_files[0]]:
            os.link(pending, target)
            published.append(target)
    except OSError:
        for path in published:
            path.unlink(missing_ok=True)
        raise
    finally:
        for pending, _ in pending_files:
            pending.unlink(missing_ok=True)


def capture_wit(wit, destination):
    """Freeze WIT bytes so the guest and provenance read the same contract."""
    root = wit.parent if wit.is_file() else wit
    files = []
    entries = [wit] if wit.is_file() else list(root.glob("*.wit"))
    if (root / "deps").is_dir():
        entries += list((root / "deps").rglob("*"))
    for path in sorted(entries):
        if not path.is_file() or path.suffix not in (".wit", ".wasm"):
            continue
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"WIT input escapes its directory: {path}")
        relative = path.relative_to(root)
        payload = path.read_bytes()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        files.append({"path": str(path), "sha256": hashlib.sha256(payload).hexdigest()})
    if not files:
        raise ValueError("WIT input contains no contract files")
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--wit", type=Path, help="WIT file/directory, default adjacent source.wit")
    parser.add_argument("--world")
    parser.add_argument("--engine", choices=("quickjs", "starlingmonkey"), default="quickjs")
    parser.add_argument("-o", "--out", "--output", required=True, type=Path)
    parser.add_argument("--module-root", type=Path)
    parser.add_argument("--typecheck", action="store_true",
                        help="run the optional tsrs package over the module root before esbuild")
    parser.add_argument("--emit-js", type=Path)
    parser.add_argument("--stub-wasi", action="store_true",
                        help="remove WASI imports using zero clocks/seed for startup and traps for other operations")
    parser.add_argument("--opt-size", action="store_true", default=True)
    parser.add_argument("--no-opt-size", action="store_false", dest="opt_size")
    parser.add_argument("--keep-custom-sections", action="store_true")
    parser.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--timeout", type=float, default=120, help="seconds allowed per compiler/initialization stage")
    args = parser.parse_args()
    if args.max_bytes < 1:
        raise ValueError("--max-bytes must be a positive integer")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    integrity = check_integrity()
    source = args.source.resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in (".js", ".mjs", ".jsx", ".ts", ".mts", ".tsx"):
        raise ValueError("source must be a JavaScript or TypeScript file")
    source_bytes = source.read_bytes()
    source_bytes.decode("utf-8", errors="strict")
    wit = (args.wit or source.with_suffix(".wit")).resolve(strict=True)
    module_root = (args.module_root or source.parent).resolve(strict=True)
    if not module_root.is_dir() or not source.is_relative_to(module_root):
        raise ValueError("source must be inside --module-root")
    authored_wit = [wit] if wit.is_file() else sorted(wit.rglob("*.wit"))
    output = args.out.absolute()
    targets = [output, Path(str(output) + ".wit"), Path(str(output) + ".build.json")]
    if args.emit_js:
        targets.append(args.emit_js.absolute())
    resolved_targets = [path.resolve() for path in targets]
    if len(set(resolved_targets)) != len(targets) or any(path in [source, *authored_wit] for path in resolved_targets):
        raise ValueError("output files must be distinct from source and WIT inputs")
    if any(path.exists() or path.is_symlink() for path in targets):
        raise ValueError("output already exists; choose a new output path")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".javascript-compile-", dir=output.parent) as temporary:
        work = Path(temporary)
        captured_wit = capture_wit(wit, work / "wit-inputs")
        typecheck = {"enabled": False}
        if args.typecheck:
            checked = subprocess.run([sys.executable, str(Path(os.environ.get("WISP_TSRS_TOOLCHAIN", ROOT.parent / "tsrs")) / "typecheck.py"),
                                      "--module-root", str(module_root), "--source", str(source),
                                      "--output", str(work / "typecheck.json")],
                                     text=True, capture_output=True, timeout=args.timeout,
                                     env={**os.environ, "RAYON_NUM_THREADS": "1"})
            if checked.returncode:
                raise ValueError((checked.stderr or checked.stdout).strip())
            typecheck = json.loads((work / "typecheck.json").read_text())
        bundled = bundle(source, module_root, work, esbuild_wasm=tool_path("esbuild"),
                         wasmtime=wasmtime_path(), timeout=args.timeout)
        if args.typecheck:
            checked_inputs = {item["path"]: item["sha256"] for item in typecheck["inputs"]}
            if any(checked_inputs.get(item["path"]) != item["sha256"] for item in bundled["inputs"]):
                raise ValueError("source changed between type checking and bundling; rerun the build")
        input_paths = {Path(item["path"]) for item in bundled["inputs"]}
        if any(path in input_paths for path in resolved_targets):
            raise ValueError("output files must be distinct from bundled module inputs")
        js = bundled["js"]
        (work / "source.mjs").write_text(js, encoding="utf-8")
        candidate, validation = work / "candidate.wasm", work / "validate.wasm"
        engine_receipt = None
        if args.engine == "starlingmonkey":
            from starlingmonkey import compile_component
            engine_receipt = compile_component(work, wit, args.world, stub_wasi=args.stub_wasi,
                                               timeout=args.timeout, wasm_tool=wasm_tool,
                                               adapter=wasm_tools.preview1_adapter())
        else:
            runtime_name = "runtime-opt-size-sync.wasm" if args.opt_size else "runtime-sync.wasm"
            candidate, validation = work / "candidate.wasm", work / "validate.wasm"
            arguments = [tool_path("componentize"), "--wit", "/wit/" + wit.name if wit.is_file() else "/wit",
                         "--source", "/work/source.mjs", "--runtime", "/runtime/" + runtime_name,
                         "--output", "/work/candidate.wasm", "--validation-output", "/work/validate.wasm",
                         "--keep-custom-sections"]
            if args.world:
                arguments += ["--world", args.world]
            if args.stub_wasi:
                arguments.append("--stub-wasi")
            run_guest(arguments, mounts=[(work / "wit-inputs", "/wit"), (work, "/work"),
                      (ROOT / ".toolchains/runtimes", "/runtime")], timeout=args.timeout)
        unstripped_bytes = candidate.stat().st_size
        if not args.keep_custom_sections:
            stripped = work / "stripped.wasm"
            wasm_tool("strip", "--all", candidate, "-o", stripped)
            os.replace(stripped, candidate)
        if candidate.stat().st_size > args.max_bytes:
            raise ValueError(f"Component is {candidate.stat().st_size} bytes, exceeds output cap {args.max_bytes}")
        wasm_tool("validate", candidate)
        if candidate.read_bytes()[:8] != COMPONENT_HEADER:
            raise ValueError("compiler did not emit a WebAssembly Component")
        if args.engine == "quickjs":
            # The validator invokes the same guarded initializer as an ordinary export.
            # It supplies trapping authored-import stubs and receives no preopens.
            initialized = subprocess.run([str(wasmtime_path()), "run", "-C", "cache=y", "-O", "opt-level=0", "-C", "parallel-compilation=n", "--invoke",
                                          "wisp-validate()", str(validation)], text=True, capture_output=True,
                                         timeout=args.timeout, env={**os.environ, "RAYON_NUM_THREADS": "1"})
            if initialized.returncode:
                raise ValueError("guest QuickJS initialization failed:\n" + (initialized.stderr or initialized.stdout).strip())
        actual_wit = wasm_tool("component", "wit", candidate)
        metadata = {
            "schemaVersion": 2, "toolchain": "javascript",
            "source": {"path": str(source), "sha256": next(item["sha256"] for item in bundled["inputs"]
                                                            if item["path"] == str(source))},
            "wit": {"path": str(wit), "world": args.world,
                    "files": captured_wit},
            "javascriptSha256": hashlib.sha256(js.encode()).hexdigest(),
            "host": {"python": sys.version.split()[0], "wasmtime": str(wasmtime_path())},
            "dependencies": integrity,
            "recipe": [{"path": str(path.relative_to(ROOT)), "sha256": digest(path)}
                       for path in [ROOT / "compile.py", ROOT / "toolchain.py", ROOT / "typescript.py"]],
            "bundleInputs": bundled["inputs"],
            "typecheck": typecheck,
            "engine": {"name": args.engine, "receipt": engine_receipt},
            "output": {"path": str(output), "bytes": candidate.stat().st_size, "sha256": digest(candidate),
                       "unstrippedBytes": unstripped_bytes, "maxBytes": args.max_bytes},
            "stages": {
                "typecheck": typecheck["compiler"] + " in WASIp1" if args.typecheck else None,
                "typescript": "esbuild@0.28.2 in WASIp1" if source.suffix.lower() in (".ts", ".mts", ".tsx") else None,
                "bundling": bundled["compiler"],
                "javascript": "QuickJS in Wasm, initialized once on the first exported call",
                "bindings": "wit-dylib and componentize-qjs code generation in WASIp1",
                "linking": "wit-component Component linker in WASIp1",
                "inspection": "upstream wasm-tools in WASIp1",
            },
            "options": {"typecheck": args.typecheck, "sync": True, "bundled": True, "stubWasi": args.stub_wasi,
                        "optSize": args.opt_size, "stripped": not args.keep_custom_sections,
                        "runtimeInitHidden": True,
                        "wasiStubBehavior": "zero insecure seed and wall/monotonic time; other calls trap"
                                            if args.stub_wasi else None},
            "sourceCompilerWasmHosted": True, "fullyWasmHostedBuild": args.engine == "quickjs", "nodeRequired": False,
            "nativeSnapshotHostApproved": args.engine == "starlingmonkey",
            "initialization": "guarded guest initialization on first export call per Component instance; compile-time validation invokes the same initializer; no Wizer snapshot",
            "unbundledModuleClosureRecorded": True,
            "limitations": (["TypeScript type checking disabled"] if not args.typecheck else []) +
                            ["Synchronous WIT interfaces", "No Node.js builtins or DOM"],
        }
        if args.engine == "starlingmonkey":
            metadata["stages"].update({
                "javascript": "StarlingMonkey executes bundled JavaScript inside Wasm",
                "bindings": "ComponentizeJS canonical ABI splicer in WASIp1",
                "snapshot": "approved native Wizer captures Wasm initialization state",
                "linking": "wasm-tools Component assembly in WASIp1",
            })
            metadata["initialization"] = "Wasm initialization captured by approved native Wizer; fresh snapshot per build"
            metadata["recipe"] += [{"path": name, "sha256": digest(ROOT / name)}
                                   for name in ("starlingmonkey.py", "starlingmonkey_bootstrap.py")]
            metadata["options"]["wasiStubBehavior"] = (
                "ComponentizeJS stubs disabled WASI features after snapshot initialization" if args.stub_wasi else None)
            metadata["limitations"] = (["TypeScript type checking disabled"] if not args.typecheck else []) + [
                "Synchronous WIT; Promise exports may settle through microtasks without I/O",
                "No Node.js builtins or DOM; HTTP and async WIT are not enabled",
                "Top-level initialization must be safe to snapshot and reuse",
            ]
        staged = [candidate, work / "world.wit", work / "receipt.json"]
        staged[1].write_text(actual_wit)
        staged[2].write_text(json.dumps(metadata, indent=2) + "\n")
        if args.emit_js:
            staged.append(work / "source.mjs")
        publish(staged, targets)
    print(f"{output} ({metadata['output']['bytes']} bytes)")
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'compile: {error}', file=sys.stderr)
        sys.exit(1)
