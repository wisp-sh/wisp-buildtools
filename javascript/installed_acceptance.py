#!/usr/bin/env python3
"""Install a fresh compiler closure and execute both engines from that prefix."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from toolchain_packages import install, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    prefix = output / "installed"
    package = args.packages.resolve() / "javascript.wisp.zip"
    report = {"passed": False, "packageSha256": digest(package), "engines": {}}
    try:
        report["installation"] = install([package], prefix)
        env = {**os.environ, "WISP_WASM_TOOLS_TOOLCHAIN": str(prefix / "wasm-tools"),
               "WISP_WASM_TOOLS": str(prefix / "wasm-tools/dist/wasm-tools.wasm"),
               "WISP_TSRS_TOOLCHAIN": str(prefix / "tsrs"), "WISP_WIZER_TOOLCHAIN": str(prefix / "wizer"),
               "PYTHONDONTWRITEBYTECODE": "1"}
        for engine in ("quickjs", "starlingmonkey"):
            directory = output / engine
            result = subprocess.run([sys.executable, str(ROOT / "javascript/verify.py"),
                "--compiler-root", str(prefix / "javascript"), "--engine", engine,
                "--output-dir", str(directory)], env=env, timeout=3600)
            report["engines"][engine] = json.loads((directory / "verification.json").read_text())
            if result.returncode or not report["engines"][engine]["passed"]:
                raise ValueError("installed engine acceptance failed: " + engine)
        report["passed"] = True
    finally:
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
