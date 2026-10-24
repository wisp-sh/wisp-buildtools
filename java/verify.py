#!/usr/bin/env python3
"""Verify Java source compilation, Components, heap growth and compiler errors in Wasm."""
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
from java.compile import verified_dist


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "verification.json")
    args = parser.parse_args()
    _, manifest = verified_dist()
    host = wasm_tools.runtime()
    environment = {**os.environ, "PATH": "/nonexistent", "WISP_WASMTIME": str(host)}
    checks = []
    with tempfile.TemporaryDirectory(prefix="java-verify-") as directory:
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
                raise RuntimeError("Java receipt does not confirm Wasm execution")
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
        shutil.copyfile(ROOT / "examples/Math.java", project / "Math.java")
        math = compile_case("Math", project / "Math.java", ROOT / "examples/math.wit", "math", project / "math.wasm")
        invoke(math, "add(20,22)", ["java-startup", "42"])
        invoke(math, "add(4294967295,1)", ["java-startup", "0"])
        invoke(math, "widen(9007199254740993)", ["java-startup", "9007199254740994"])
        invoke(math, "scale(-2.0)", ["java-startup", "-3"])
        invoke(math, "overflow(2147483647)", ["java-startup", "0"])
        core = work / "growth-core.wasm"
        _, seconds = command([sys.executable, ROOT / "compile.py", ROOT / "examples/Growth.java",
                              "--main", "Growth", "-o", core])
        core_receipt = json.loads(Path(str(core) + ".build.json").read_text())
        if not core_receipt["fullyWasmHostedBuild"]:
            raise RuntimeError("Core Java compilation did not run in Wasm")
        checks.append({"name": "Growth Core", "compileSeconds": seconds, "sha256": digest(core)})
        # TeaVM's old heap-growth bug depends on argv allocation, including the
        # program name. Keep identical module bytes across this path matrix.
        for length in range(1, 17):
            program = work / ("p" * length + ".wasm")
            shutil.copyfile(core, program)
            result, _ = command([host, "run", program])
            if result.stdout.strip().splitlines() != ["java-growth-startup"]:
                raise RuntimeError(result.stdout + result.stderr)
        checks.append({"name": "8MiB heap growth across Core program-name lengths", "cases": 16})
        growth = work / "growth-component.wasm"
        command([sys.executable, ROOT.parent / "teavm/component.py", core,
                 "--wit", ROOT / "examples/growth.wit", "--world", "growth", "-o", growth])
        invoke(growth, "answer()", ["java-growth-startup", "42"])
        features = compile_case("Features", ROOT / "examples/Features.java", ROOT / "examples/features.wit", "features", work / "features.wasm")
        invoke(features, "answer()", ["java-record-startup", "42"])
        protected = {p: digest(p) for p in [math, Path(str(math) + ".build.json"), Path(str(math) + ".wit")]}
        invalid = work / "Broken.java"
        invalid.write_text("public class Broken { static int answer() { return unknown; } }\n")
        command([sys.executable, ROOT / "compile.py", invalid, "--main", "Broken", "-o", work / "broken.wasm"], success=False)
        if any(work.glob("broken.wasm*")):
            raise RuntimeError("invalid Java source left published output")
        checks.append({"name": "invalid source leaves no output"})
        command([sys.executable, ROOT / "compile.py", project / "Math.java", "--main", "Math", "-o", math], success=False)
        if any(digest(p) != checksum for p, checksum in protected.items()):
            raise RuntimeError("existing output changed after a rejected overwrite")
        checks.append({"name": "existing output preserved"})
        verified_dist()
    report = {"schema": 1, "version": manifest["version"], "passed": len(checks),
              "nodeOnPath": False, "nativeCompilerOnPath": False, "checks": checks}
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Java: {len(checks)} checks passed")


if __name__ == "__main__":
    main()
