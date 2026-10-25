#!/usr/bin/env python3
"""Package a Python program and validate it with Wasm-hosted CPython."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

from scripts.limits import MAX_SOURCE_BYTES
from scripts.common import MAX_COMPONENT_BYTES, ROOT, digest, guest_tools, invoke, run, verified_package


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "wisp/python")
    parser.add_argument("--entry", default="add", help="Python callable to expose through the fixed add export")
    parser.add_argument("--wit", type=Path, help="authored WIT contract with a provisioned runtime template")
    parser.add_argument("--inferred", action="store_true", help="--wit was inferred from top-level Python functions")
    parser.add_argument("--world", help="world selected from --wit")
    parser.add_argument("--dependency-root", type=Path, help="directory containing pyproject.toml/requirements/lock, independent of a src layout")
    parser.add_argument("--package-root", type=Path, help="root for local Python imports, requires --wit")
    parser.add_argument("--python-path", type=Path, action="append", default=[],
                        help="additional directory of vendored pure Python modules, repeatable; requires --wit")
    parser.add_argument("--timeout", type=float, default=30,
                        help="seconds allowed for guest source initialization, default 30")
    args = parser.parse_args()
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    if not args.entry.isidentifier():
        raise ValueError("--entry must be a Python identifier")
    source = args.source.resolve(strict=True)
    output = args.output.absolute()
    if source == output.resolve():
        raise ValueError("output cannot overwrite Python source")
    with source.open("rb") as stream:
        source_bytes = stream.read(MAX_SOURCE_BYTES + 1)
    if len(source_bytes) > MAX_SOURCE_BYTES:
        raise ValueError("Python source exceeds the 16 MiB source/data limit")
    source_bytes.decode("utf-8", errors="strict")
    if args.wit:
        from compile_contract import compile_contract
        compile_contract(args, source, output)
        return
    if args.inferred:
        raise ValueError("--inferred requires --wit")
    if args.package_root or args.python_path or args.dependency_root:
        raise ValueError("--package-root, --python-path and --dependency-root require --wit")
    if args.world:
        raise ValueError("--world requires --wit")
    dist = ROOT / "dist"
    if not verified_package(dist):
        raise ValueError("Python toolchain is missing or changed; run python3 bootstrap.py")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".python-compile-", dir=output.parent) as directory:
        candidate = Path(directory) / "candidate.component.wasm"
        run(guest_tools.guest_command(dist / "python-packager.wasm",
            dist / "template.component.wasm", source, args.entry, candidate))
        if candidate.stat().st_size > MAX_COMPONENT_BYTES:
            raise ValueError(f"Python Component exceeds Wisp's 64 MiB limit: {candidate.stat().st_size} bytes")
        run(guest_tools.command("validate", candidate))
        result = invoke(candidate, "validate()", timeout=args.timeout)
        if result != "ok":
            raise ValueError(f"guest CPython rejected the program: {result}")
        details = {"output": str(output), "format": "component", "bytes": candidate.stat().st_size,
                   "sha256": digest(candidate), "source_sha256": digest(source),
                   "source_compiler": "guest CPython compile/exec", "interface": "wisp:python/api@0.1.0"}
        details["fullyWasmHostedBuild"] = True
        details["build_stages"] = ["WASI source-provider packaging", "WASI wasm-tools validation",
                                    "guest CPython compile/exec"]
        details["python_entry"] = args.entry
        os.replace(candidate, output)
    print(json.dumps(details, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from error
