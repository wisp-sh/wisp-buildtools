#!/usr/bin/env python3
"""Wrap a Grain Core Wasm module with WIT using the shared Wasm tools package."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
if os.environ.get("WISP_WASM_TOOLS_TOOLCHAIN"):
    sys.path.insert(0, str(Path(os.environ["WISP_WASM_TOOLS_TOOLCHAIN"]).resolve().parent))
import wasm_tools

ADAPTER_PAGE = 65536
GRAIN_MEMORY_BASE = 3 * ADAPTER_PAGE


def reserve_adapter_memory(text: str) -> str:
    """Give the adapter two pages below Grain's compiler-reserved heap base."""
    memories = re.findall(r"^\s*\(memory[^\n]*$", text, re.MULTILINE)
    if len(memories) != 1 or re.search(r'\(import[^\n]*\(memory', text):
        raise ValueError("Grain Component requires one owned memory")
    memory = re.fullmatch(r"\s*\(memory(?:\s+\$[^\s()]+)?\s+\(;0;\)\s+(\d+)(?:\s+\d+)?\)\s*", memories[0])
    if memory is None or int(memory[1]) < 3:
        raise ValueError("Grain adapter arena requires at least three unshared wasm32 memory pages")
    if re.search(r'\(export\s+"cabi_realloc_adapter"\s', text):
        raise ValueError("Grain Component reserves the cabi_realloc_adapter export")
    data = re.findall(r"^\s*\(data[^\n]*$", text, re.MULTILINE)
    for segment in data:
        offset = re.search(r"\(i32.const\s+(-?\d+)\)", segment.split('"', 1)[0])
        if offset is None or int(offset[1]) < GRAIN_MEMORY_BASE:
            raise ValueError("Grain data overlaps the adapter arena; compile with --memory-base=196608")
    heap = re.search(r"\(global\s+\$runtimeHeapStart_0\b[^\n]*i32.const\s+(-?\d+)", text)
    if heap is not None and int(heap[1]) < GRAIN_MEMORY_BASE:
        raise ValueError("Grain heap overlaps the adapter arena; compile with --memory-base=196608")
    if not data and heap is None:
        raise ValueError("cannot verify Grain's reserved memory prefix")
    allocator = r'''
  (global $wispAdapterNext (mut i32) (i32.const 65536))
  (func $wispAdapterRealloc (export "cabi_realloc_adapter")
    (param $old i32) (param $oldLen i32) (param $align i32) (param $size i32)
    (result i32) (local $ptr i32)
    local.get $old
    local.get $oldLen
    i32.or
    local.get $size
    i32.const 65536
    i32.ne
    i32.or
    local.get $align
    i32.const 65536
    i32.gt_u
    i32.or
    local.get $align
    i32.eqz
    i32.or
    if
      unreachable
    end
    global.get $wispAdapterNext
    local.tee $ptr
    i32.const 65536
    i32.add
    i32.const 196608
    i32.gt_u
    if
      unreachable
    end
    local.get $ptr
    i32.const 65536
    i32.add
    global.set $wispAdapterNext
    local.get $ptr)
'''
    end = text.rfind(")")
    if end < 0:
        raise ValueError("invalid printed Grain module")
    return text[:end] + allocator + text[end:]


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
          initialize=False, reserve_adapter=False, timeout=120) -> dict:
    source = source.resolve(strict=True)
    wit = wit.resolve(strict=True)
    output = output.absolute()
    targets = [output, Path(str(output) + ".wit"), Path(str(output) + ".build.json")]
    if any(path.exists() or path.is_symlink() for path in targets):
        raise ValueError("output already exists; choose a new output path")
    if source.read_bytes()[:8] != b"\0asm\x01\0\0\0":
        raise ValueError("input must be a Grain Core Wasm module")
    adapter = wasm_tools.preview1_adapter(adapter)
    output.parent.mkdir(parents=True, exist_ok=True)
    published = []
    with tempfile.TemporaryDirectory(prefix=".grain-component-", dir=output.parent) as directory:
        work = Path(directory)
        shutil.copyfile(source, work / "input.wasm")
        if initialize or reserve_adapter:
            # The reactor adapter must be initialized before Grain's startup can
            # call WASI. Component assembly invokes `_initialize` in that order.
            text = tool("print", work / "input.wasm", timeout=timeout)
            if initialize:
                if re.search(r"^\s*\(start\s", text, re.MULTILINE):
                    raise ValueError("Component input must use Grain's default _start export, not --use-start-section")
                if re.search(r'\(export\s+"_initialize"\s', text):
                    raise ValueError("Grain input already exports _initialize")
                text, count = re.subn(r'(\(export\s+)"_start"(\s+\(func\s)',
                                     r'\1"_initialize"\2', text)
                if count != 1:
                    raise ValueError("Grain Component input must export exactly one _start function")
            if reserve_adapter:
                text = reserve_adapter_memory(text)
            (work / "reactor.wat").write_text(text)
            tool("parse", work / "reactor.wat", "-o", work / "reactor.wasm", timeout=timeout)
            prepared = work / "reactor.wasm"
        else:
            prepared = work / "input.wasm"
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
            "toolchain": "grain",
            "stage": "component-assembly",
            "core": {"path": str(source), "sha256": digest(work / "input.wasm")},
            "wit": {"path": str(wit), "world": world, "files": captured},
            "adapter": adapter_record,
            "initialization": "_start exported as _initialize and invoked after WASI adapter setup"
                              if initialize else "input module initialization is preserved",
            "preparedCoreSha256": digest(prepared),
            "adapterMemory": {"allocation": "bounded compiler-reserved prefix", "start": ADAPTER_PAGE,
                              "end": GRAIN_MEMORY_BASE, "heapBaseMinimum": GRAIN_MEMORY_BASE}
                             if reserve_adapter else None,
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
    parser.add_argument("source", type=Path, help="linked Grain Core Wasm")
    parser.add_argument("--wit", type=Path, required=True)
    parser.add_argument("--world")
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--preview1-adapter", type=Path,
                        help="reactor adapter; defaults to WISP_PREVIEW1_ADAPTER or the shared Wasm tools package")
    parser.add_argument("--initialize", action="store_true",
                        help="adapt Grain's default _start export to reactor initialization")
    parser.add_argument("--reserve-adapter", action="store_true",
                        help="supply adapter storage below Grain's --memory-base=196608 heap")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    metadata = build(args.source, args.wit, args.output, world=args.world,
                     adapter=args.preview1_adapter, initialize=args.initialize,
                     reserve_adapter=args.reserve_adapter, timeout=args.timeout)
    print(f"{args.output} ({metadata['output']['bytes']} bytes)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"grain component: {error}", file=sys.stderr)
        sys.exit(1)
