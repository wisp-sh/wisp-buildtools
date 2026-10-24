#!/usr/bin/env python3
"""Verify real Grain compilation, Component initialization and compiler failures."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from compile import ROOT, digest, integrity, runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / ".cache/verification")
    parser.add_argument("--report", type=Path, default=ROOT / "verification.json")
    args = parser.parse_args()
    manifest = integrity()
    host = runtime()
    environment = {**os.environ, "PATH": "/nonexistent", "WISP_WASMTIME": str(host)}
    records = []
    with tempfile.TemporaryDirectory(prefix="grain-verify-") as directory:
        work = Path(directory)

        def command(arguments, expected=0):
            started = time.monotonic()
            result = subprocess.run([str(value) for value in arguments], env=environment,
                                    capture_output=True, text=True, timeout=900)
            if (expected == 0 and result.returncode != 0) or (expected != 0 and result.returncode == 0):
                raise RuntimeError(f"unexpected exit {result.returncode}: {' '.join(map(str, arguments))}\n{result.stdout}\n{result.stderr}")
            return result, round(time.monotonic() - started, 3)

        def compile_case(name, source, wit, world, output=None):
            output = output or work / f"{name}.wasm"
            result, elapsed = command([sys.executable, ROOT / "compile.py", source, "--component", "--wit", wit,
                                       "--world", world, "--cache-dir", args.cache_dir, "-o", output])
            receipt = json.loads(Path(str(output) + ".build.json").read_text())
            if not receipt["fullyWasmHostedBuild"] or receipt["nodeRequired"]:
                raise RuntimeError("compiler receipt does not confirm Wasm execution")
            records.append({"name": name, "compileSeconds": elapsed, "bytes": output.stat().st_size,
                            "sha256": digest(output), "fullyWasmHostedBuild": True})
            return output

        def invoke(module, expression, expected):
            result, elapsed = command([host, "run", "--invoke", expression, module])
            actual = result.stdout.strip().splitlines()
            if actual != expected:
                raise RuntimeError(f"{expression}: expected {expected}, got {actual}\n{result.stderr}")
            records.append({"name": expression, "stdout": actual, "seconds": elapsed})

        # Source and output commonly share a directory. Staging must not copy itself.
        project = work / "same-directory"
        project.mkdir()
        shutil.copyfile(ROOT / "examples/math.gr", project / "math.gr")
        math = compile_case("math", project / "math.gr", ROOT / "examples/math.wit", "math", project / "math.wasm")
        invoke(math, "add(20,22)", ["42"])
        invoke(math, "widen(4294967296)", ["4294967297"])
        invoke(math, "scale(2.0)", ["3"])
        # Top-level WASI I/O must run after the Preview 1 adapter is initialized.
        startup = compile_case("startup", ROOT / "examples/startup.gr", ROOT / "examples/startup.wit", "startup")
        invoke(startup, "add(20,22)", ["grain-startup", "42"])
        imports = compile_case("imports", ROOT / "examples/imports/main.gr", ROOT / "examples/imports/world.wit", "imports")
        invoke(imports, "twice(21)", ["42"])
        invalid = work / "invalid.gr"
        invalid.write_text('module Invalid\nlet broken = 1 + "wrong type"\n')
        rejected = work / "invalid.wasm"
        result, elapsed = command([sys.executable, ROOT / "compile.py", invalid, "--cache-dir", args.cache_dir,
                                   "-o", rejected], expected=1)
        if rejected.exists() or Path(str(rejected) + ".build.json").exists():
            raise RuntimeError("failed source compilation published an output")
        if not result.stderr.strip():
            raise RuntimeError("invalid source did not produce a compiler diagnostic")
        records.append({"name": "invalid-source", "rejected": True, "seconds": elapsed,
                        "diagnostic": result.stderr.strip()})
        protected = work / "protected.wasm"
        protected.write_bytes(b"existing output")
        result, elapsed = command([sys.executable, ROOT / "compile.py", invalid, "-o", protected], expected=1)
        if protected.read_bytes() != b"existing output":
            raise RuntimeError("compiler replaced an existing output")
        records.append({"name": "existing-output", "preserved": True})
    report = {"schema": 1, "version": manifest["version"], "compilerSha256": manifest["sha256"]["grainc.wasm"],
              "nodeOnPath": False, "nativeCompilerOnPath": False, "host": str(host),
              "checks": records, "passed": len(records)}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Grain {manifest['version']}: {len(records)} checks passed with PATH=/nonexistent")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Grain verification: {error}") from error
