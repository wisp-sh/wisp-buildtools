#!/usr/bin/env python3
"""Wrap a TeaVM Core Wasm module with WIT using the shared Wasm tools package."""
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
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT.parent))
if os.environ.get("WISP_WASM_TOOLS_TOOLCHAIN"):
    sys.path.insert(0, str(Path(os.environ["WISP_WASM_TOOLS_TOOLCHAIN"]).resolve().parent))
import wasm_tools

if __package__:
    from .reactor import prepare
else:
    from reactor import prepare


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tool(*arguments, timeout=120):
    result = subprocess.run(wasm_tools.command(*arguments), capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip())
    return result.stdout


def capture_wit(source: Path, destination: Path) -> list[dict]:
    base = source.parent if source.is_file() else source
    entries = [source] if source.is_file() else list(base.glob("*.wit"))
    if (base / "deps").is_dir():
        entries += list((base / "deps").rglob("*"))
    files = []
    for path in sorted(entries):
        if not path.is_file() or path.suffix not in {".wit", ".wasm"}:
            continue
        if not path.resolve().is_relative_to(base):
            raise ValueError(f"WIT input escapes its directory: {path}")
        target = destination / path.relative_to(base)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        files.append({"path": str(path), "sha256": digest(target)})
    if not files:
        raise ValueError("WIT input contains no contract files")
    return files


def build(source: Path, wit: Path, output: Path, *, world=None, adapter=None,
          timeout=120) -> dict:
    source = source.resolve(strict=True)
    wit = wit.resolve(strict=True)
    output = output.absolute()
    targets = [output, Path(str(output) + ".wit"), Path(str(output) + ".build.json")]
    if any(path.exists() or path.is_symlink() for path in targets):
        raise ValueError("output already exists; choose a new output path")
    if source.read_bytes()[:8] != b"\0asm\x01\0\0\0":
        raise ValueError("input must be a TeaVM Core Wasm module")
    adapter = wasm_tools.preview1_adapter(adapter)
    output.parent.mkdir(parents=True, exist_ok=True)
    published = []
    with tempfile.TemporaryDirectory(prefix=".teavm-component-", dir=output.parent) as directory:
        work = Path(directory)
        shutil.copyfile(source, work / "input.wasm")
        text = tool("print", work / "input.wasm", timeout=timeout)
        text, arena = prepare(text)
        (work / "reactor.wat").write_text(text)
        tool("parse", work / "reactor.wat", "-o", work / "reactor.wasm", timeout=timeout)
        prepared = work / "reactor.wasm"
        captured = capture_wit(wit, work / "wit")
        embedded, candidate = work / "embedded.wasm", work / "component.wasm"
        selected_wit = work / "wit" / wit.name if wit.is_file() else work / "wit"
        arguments = ["component", "embed", selected_wit, prepared,
                     "-o", embedded]
        if world:
            arguments += ["--world", world]
        tool(*arguments, timeout=timeout)
        arguments = ["component", "new", embedded, "-o", candidate]
        adapter_record = None
        if adapter is not None:
            shutil.copyfile(adapter, work / "preview1-adapter.wasm")
            arguments += ["--adapt", f"wasi_snapshot_preview1={work / 'preview1-adapter.wasm'}"]
            adapter_record = {"path": str(adapter), "sha256": digest(work / "preview1-adapter.wasm")}
        tool(*arguments, timeout=timeout)
        tool("validate", candidate, timeout=timeout)
        if candidate.read_bytes()[:8] != b"\0asm\x0d\0\x01\0":
            raise ValueError("linker did not emit a WebAssembly Component")
        actual_wit = tool("component", "wit", candidate, timeout=timeout)
        metadata = {
            "schemaVersion": 1,
            "toolchain": "teavm",
            "stage": "component-assembly",
            "core": {"path": str(source), "sha256": digest(work / "input.wasm")},
            "wit": {"path": str(wit), "world": world, "files": captured},
            "adapter": adapter_record,
            "initialization": "TeaVM runtime and main run after WASI adapter initialization",
            "preparedCoreSha256": digest(prepared),
            "adapterMemory": arena,
            "wasmTools": {"path": str(wasm_tools.module()), "sha256": digest(wasm_tools.module())},
            "host": {"wasmtime": str(wasm_tools.runtime())},
            "output": {"path": str(output), "bytes": candidate.stat().st_size,
                       "sha256": digest(candidate)},
            "assemblyWasmHosted": True,
            "nodeRequired": False,
        }
        (work / "world.wit").write_text(actual_wit)
        (work / "receipt.json").write_text(json.dumps(metadata, indent=2) + "\n")
        try:
            # Publish the complete Component last. Existing outputs are never replaced.
            for staged, target in [(work / "world.wit", targets[1]),
                                   (work / "receipt.json", targets[2]),
                                   (candidate, targets[0])]:
                os.link(staged, target)
                published.append(target)
        except OSError:
            for target in published:
                target.unlink(missing_ok=True)
            raise
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="linked TeaVM Core Wasm")
    parser.add_argument("--wit", type=Path, required=True)
    parser.add_argument("--world")
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--preview1-adapter", type=Path,
                        help="reactor adapter; defaults to WISP_PREVIEW1_ADAPTER or the shared Wasm tools package")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    metadata = build(args.source, args.wit, args.output, world=args.world,
                     adapter=args.preview1_adapter, timeout=args.timeout)
    print(f"{args.output} ({metadata['output']['bytes']} bytes)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"teavm component: {error}", file=sys.stderr)
        sys.exit(1)
