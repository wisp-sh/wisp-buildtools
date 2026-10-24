#!/usr/bin/env python3
"""Compile a freestanding Zig library using compilers hosted inside WebAssembly."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
EXCLUDED = {".git", ".zig-cache", "zig-cache", "zig-out", "node_modules", ".venv", "__pycache__", "target", ".work", ".sources", ".downloads", "dist", "work"}


def host_runtime(configured: str | None = None) -> str:
    runtime = configured or os.environ.get("WISP_WASMTIME") or shutil.which("wasmtime")
    resolved = shutil.which(runtime) if runtime else None
    if not resolved:
        raise RuntimeError("Wisp must provide Wasmtime; set WISP_WASMTIME or pass --wasmtime")
    path = Path(resolved).resolve()
    if path.is_relative_to(ROOT):
        raise RuntimeError("Wasmtime must be supplied by Wisp outside the Zig compiler package")
    return str(path)


def clang_backend(configured: str | None = None, checksum: str | None = None) -> Path:
    path = Path(configured or os.environ.get("WISP_CLANG") or ROOT.parent / "clang/.toolchains/build-p3/bin/clang-wasm.component").resolve()
    if not path.is_file() or path.is_relative_to(ROOT):
        raise RuntimeError("Clang must be installed as a separate toolchain; set WISP_CLANG or pass --clang")
    manifest = json.loads((DIST / "manifest.json").read_text())
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != (checksum or manifest["clang_backend"]["sha256"]):
        raise RuntimeError("external Clang does not match Zig's compiler dependency; rerun bootstrap.py against the installed Clang")
    return path


def clang_resource_directory() -> Path:
    path = Path(os.environ.get("WISP_CLANG_RESOURCE_DIR") or ROOT.parent / "clang/include").resolve()
    if not (path / "stdarg.h").is_file() or path.is_relative_to(ROOT):
        raise RuntimeError("Clang resource headers are missing from its package; set WISP_CLANG_RESOURCE_DIR")
    return path


def llvm_preload_args() -> list[str]:
    helper = Path(os.environ.get("WISP_LLVM_TOOLCHAIN", ROOT.parent / "llvm")) / "shared.py"
    if not helper.is_file():
        raise RuntimeError("the shared LLVM toolchain is missing; install Clang's LLVM dependency")
    specification = importlib.util.spec_from_file_location("wisp_llvm_shared", helper)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.preload_args()


def compile_source(args: argparse.Namespace) -> None:
    source = Path(args.source).resolve(strict=True)
    project = Path(args.project_root).resolve(strict=True) if args.project_root else source.parent
    entry = source.relative_to(project)
    if source.suffix != ".zig":
        raise ValueError("source must be a .zig file")
    output = Path(args.output).resolve()
    for artifact in ("zig.wasm", "manifest.json", "include/zig.h", "lib/std/std.zig"):
        if not (DIST / artifact).is_file():
            raise RuntimeError(f"missing {DIST / artifact}; run bootstrap.py first")
    with tempfile.TemporaryDirectory(prefix="zig-compile-") as temporary:
        job = Path(temporary)
        local_project = job / "project"

        def ignore(directory: str, names: list[str]) -> list[str]:
            ignored = []
            for name in names:
                path = Path(directory) / name
                if name in EXCLUDED or path.resolve() == output or path.suffix in {".wasm", ".component", ".o"}:
                    ignored.append(name)
                elif path.is_symlink():
                    raise ValueError(f"project symlinks are unsupported: {path}")
            return ignored

        shutil.copytree(project, local_project, ignore=ignore)
        if not (local_project / entry).is_file():
            raise ValueError("source was excluded from the project copy; choose a narrower --project-root")
        cache = job / "cache"
        cache.mkdir()
        shutil.copytree(DIST / "lib", job / "lib")
        guest_job = "."
        c_file = job / "module.c"
        wasm_file = job / "module.wasm"
        runtime = host_runtime(args.wasmtime)
        clang = clang_backend(args.clang, args.clang_sha256)
        resource_headers = clang_resource_directory()
        staged_headers = job / "include"
        shutil.copytree(DIST / "include", staged_headers)
        header = staged_headers / "zig.h"
        header.write_text(header.read_text().replace(
            "#if zig_has_builtin(return_address) || defined(zig_gcc) || defined(zig_tinyc)",
            "#if defined(__wasm__)\n#define zig_return_address() ((uintptr_t)0)\n#elif zig_has_builtin(return_address) || defined(zig_gcc) || defined(zig_tinyc)"))
        # Zig's bootstrap compiler expects cwd to be preopen fd 3. Its WASI
        # implementation opens lib paths relative to cwd, so stage the library
        # beside the project instead of writing into the installed distribution.
        frontend = [runtime, "run", "-O", "opt-level=0", "-C", "cache=y", "--dir", f"{job}::.", "--dir", f"{cache}::/cache", str(DIST / "zig.wasm"), "build-obj", "-ofmt=c", "-OReleaseSmall", "--name", "module", f"-femit-bin={guest_job}/module.c", "-target", "wasm32-freestanding", "--zig-lib-dir", "lib", f"-Mroot={guest_job}/project/{entry.as_posix()}"]
        subprocess.run(frontend, check=True)
        if args.memory_runtime:
            with c_file.open("ab") as stream:
                stream.write(b"\n" + (ROOT / "memory.c").read_bytes())
        if args.emit_c:
            requested = Path(args.emit_c).resolve()
            requested.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(c_file, requested)
            shutil.copy2(header, requested.with_suffix(".zig.h"))
        backend = [runtime, "run", "-S", "p3=y", "-O", "opt-level=0", "-C", "cache=y", *llvm_preload_args(), "--dir", f"{job}::/work", "--dir", f"{staged_headers}::/include", "--dir", f"{resource_headers}::/clang-include", str(clang), "/work/module.c", "-o", "/work/module.wasm", args.optimization, "-I", "/include", "-I", "/clang-include"]
        subprocess.run(backend, check=True)
        if wasm_file.read_bytes()[:8] != b"\x00asm\x01\x00\x00\x00":
            raise RuntimeError("compiler did not produce a Core WebAssembly module")
        output.parent.mkdir(parents=True, exist_ok=True)
        # Replace only after both guest stages succeed. Failed builds retain the
        # previous successful artifact and never publish a partial module.
        with tempfile.NamedTemporaryFile(prefix=f".{output.name}.", dir=output.parent, delete=False) as handle:
            pending = Path(handle.name)
            try:
                with wasm_file.open("rb") as module:
                    shutil.copyfileobj(module, handle)
                handle.flush()
                pending.replace(output)
            finally:
                pending.unlink(missing_ok=True)
        if args.emit_c:
            c_output = Path(args.emit_c).resolve()
            c_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(c_file, c_output)
    print(f"Compiled {source} -> {output}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="Root Zig source file with exported functions")
    parser.add_argument("-o", "--output", required=True, help="Output Core Wasm module")
    parser.add_argument("--project-root", help="Directory containing local Zig imports and embedded files, default source directory")
    parser.add_argument("--memory-runtime", action="store_true", help="provide freestanding memcpy, memmove, memset and memcmp")
    parser.add_argument("--emit-c", help="Also save the C intermediate for inspection")
    parser.add_argument("--wasmtime", help="Wisp-provided Wasmtime executable, default WISP_WASMTIME or host PATH")
    parser.add_argument("--clang-sha256", help="Explicit SHA-256 pin for a separately built Clang backend")
    parser.add_argument("--clang", help="Clang from the separately installed toolchain, default WISP_CLANG or sibling clang")
    optimization = parser.add_mutually_exclusive_group()
    optimization.add_argument("-O0", dest="optimization", action="store_const", const="-O0")
    optimization.add_argument("-O1", dest="optimization", action="store_const", const="-O1")
    parser.set_defaults(optimization="-O1")
    args = parser.parse_args()
    try:
        compile_source(args)
    except subprocess.CalledProcessError as exc:
        print(f"zig: guest compiler exited with status {exc.returncode}", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"zig: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
