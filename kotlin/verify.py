#!/usr/bin/env python3
"""Verify Kotlin source compilation, Components, heap growth and compiler errors in Wasm."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT.parent))
import wasm_tools
from teavm.compile import digest
from kotlin.compile import verified_dist


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "verification.json")
    args = parser.parse_args()
    _, manifest = verified_dist()
    host = wasm_tools.runtime()
    environment = {**os.environ, "PATH": "/nonexistent", "WISP_WASMTIME": str(host)}
    checks = []
    with tempfile.TemporaryDirectory(prefix="kotlin-verify-") as directory:
        work = Path(directory)

        def command(arguments, success=True):
            started = time.monotonic()
            result = subprocess.run(list(map(str, arguments)), env=environment,
                                    capture_output=True, text=True, timeout=1800)
            if (result.returncode == 0) != success:
                raise RuntimeError(result.stdout + result.stderr)
            return result, round(time.monotonic() - started, 3)

        def compile_case(name, source, wit, world, output):
            _, seconds = command([sys.executable, ROOT / "compile.py", source, "--main", name,
                                  "--component", "--wit", wit, "--world", world, "-o", output])
            receipt = json.loads(Path(str(output) + ".build.json").read_text())
            if not receipt["fullyWasmHostedBuild"] or receipt["nodeRequired"]:
                raise RuntimeError("Kotlin receipt does not confirm Wasm execution")
            print(f"{name}: compilation passed", flush=True)
            checks.append({"name": name, "compileSeconds": seconds,
                           "bytes": output.stat().st_size, "sha256": digest(output)})
            return output

        def invoke(module, expression, expected):
            result, seconds = command([host, "run", "--invoke", expression, module])
            if result.stdout.strip().splitlines() != expected:
                raise RuntimeError(f"{expression}: {result.stdout}\n{result.stderr}")
            checks.append({"name": expression, "stdout": expected, "seconds": seconds})

        # Place sources beside output to catch staging recursing into itself.
        project = work / "project"
        project.mkdir()
        shutil.copyfile(ROOT / "examples/Math.kt", project / "Math.kt")
        math = compile_case("MathKt", project / "Math.kt", ROOT / "examples/math.wit", "math", project / "math.wasm")
        invoke(math, "add(20,22)", ["kotlin-startup", "42"])
        invoke(math, "add(4294967295,1)", ["kotlin-startup", "0"])
        invoke(math, "widen(9007199254740993)", ["kotlin-startup", "9007199254740994"])
        invoke(math, "scale(-2.0)", ["kotlin-startup", "-3"])
        invoke(math, "overflow(2147483647)", ["kotlin-startup", "0"])
        growth = compile_case("GrowthKt", ROOT / "examples/Growth.kt", ROOT / "examples/growth.wit", "growth", work / "growth.wasm")
        invoke(growth, "answer()", ["kotlin-growth-startup", "42"])
        features = compile_case("FeaturesKt", ROOT / "examples/Features.kt", ROOT / "examples/features.wit", "features", work / "features.wasm")
        invoke(features, "answer()", ["kotlin-features-startup", "42"])
        protected = {p: digest(p) for p in [math, Path(str(math) + ".build.json"), Path(str(math) + ".wit")]}
        invalid = work / "Broken.kt"
        invalid.write_text("fun answer(): Int = unknown\n")
        command([sys.executable, ROOT / "compile.py", invalid, "--main", "BrokenKt", "-o", work / "broken.wasm"], success=False)
        if any(work.glob("broken.wasm*")):
            raise RuntimeError("invalid Kotlin source left published output")
        checks.append({"name": "invalid source leaves no output"})
        command([sys.executable, ROOT / "compile.py", project / "Math.kt", "--main", "MathKt", "-o", math], success=False)
        if any(digest(p) != checksum for p, checksum in protected.items()):
            raise RuntimeError("existing output changed after a rejected overwrite")
        checks.append({"name": "existing output preserved"})
        verified_dist()
    report = {"schema": 1, "version": manifest["version"], "passed": len(checks),
              "nodeOnPath": False, "nativeCompilerOnPath": False, "checks": checks}
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Kotlin: {len(checks)} checks passed")


if __name__ == "__main__":
    main()
