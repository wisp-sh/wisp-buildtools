#!/usr/bin/env python3
"""Verify the shared TeaVM backend inside the Wasm JVM, without native Java."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT.parent))
import wasm_tools
from teavm.compile import digest, verified_dist


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "verification.json")
    args = parser.parse_args()
    dist, manifest = verified_dist()
    host = wasm_tools.runtime()
    environment = {**os.environ, "PATH": "/nonexistent", "WISP_WASMTIME": str(host)}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="teavm-verify-") as directory:
        output = Path(directory) / "smoke.wasm"
        result = subprocess.run([sys.executable, ROOT / "compile.py", dist / "fixtures", "--main", "Smoke",
                                 "--component", "--wit", ROOT / "examples/smoke.wit", "--world", "smoke",
                                 "-o", output], env=environment, capture_output=True, text=True, timeout=1800)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        receipt = json.loads(Path(str(output) + ".build.json").read_text())
        if not receipt["fullyWasmHostedBuild"] or receipt["nodeRequired"]:
            raise RuntimeError("backend receipt does not confirm Wasm execution")
        invoked = subprocess.run([host, "run", "--invoke", "answer()", output],
                                 env=environment, capture_output=True, text=True, timeout=120)
        if invoked.returncode or invoked.stdout.strip().splitlines() != ["teavm-startup", "42"]:
            raise RuntimeError(invoked.stdout + invoked.stderr)
        verified_dist()
        report = {"schema": 1, "version": manifest["version"], "passed": 2,
                  "nodeOnPath": False, "nativeCompilerOnPath": False,
                  "checks": [{"name": "Wasm-hosted bytecode to Component", "sha256": digest(output),
                              "bytes": output.stat().st_size}, {"name": "startup and answer", "stdout": ["teavm-startup", "42"]}],
                  "seconds": round(time.monotonic() - started, 3)}
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print("TeaVM: 2 checks passed")


if __name__ == "__main__":
    main()
