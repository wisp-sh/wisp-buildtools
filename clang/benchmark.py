#!/usr/bin/env python3
"""Measure one compiler component with per-run host peak RSS."""
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def compile_once(wasmtime, component, work, marker, cold):
    with tempfile.NamedTemporaryFile() as rss_file:
        command = ["/usr/bin/time", "-f", "%M", "-o", rss_file.name,
                   wasmtime, "run"]
        if cold:
            command += ["-C", "cache=n"]
        command += ["--dir", f"{work}::/work", str(component),
                    "/work/input.cpp", "-o", "/work/output.wasm"]
        start = time.monotonic_ns()
        process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, bufsize=1)
        markers = {}
        stderr = []
        for line in process.stderr:
            stderr.append(line)
            if line.startswith(marker + ": "):
                markers[line.strip().split(": ", 1)[1]] = (
                    time.monotonic_ns() - start) / 1e6
        stdout = process.stdout.read()
        status = process.wait()
        elapsed = (time.monotonic_ns() - start) / 1e6
        if status:
            raise RuntimeError(f"{command}: {status}\n{''.join(stderr)}\n{stdout}")
        return {
            "startup_ms": markers["ready"],
            "codegen_ms": markers["codegen-complete"] - markers["ready"],
            "link_ms": markers["link-complete"] - markers["codegen-complete"],
            "compile_link_ms": markers["link-complete"] - markers["ready"],
            "total_ms": elapsed,
            "peak_rss_kib": int(Path(rss_file.name).read_text().strip()),
        }


def main():
    if len(sys.argv) != 5:
        raise SystemExit("usage: benchmark.py COMPONENT RAW NAME OUTPUT_JSON")
    root = Path(__file__).resolve().parent
    component, raw, name, output_json = map(Path, sys.argv[1:])
    work = root / "work" / "benchmark" / str(name)
    work.mkdir(parents=True, exist_ok=True)
    (work / "input.cpp").write_bytes((root / "examples/add.cpp").read_bytes())
    wasmtime = str(root / ".toolchains/wasmtime-v50.0.0-x86_64-linux/wasmtime")
    marker = "clang-p3" if str(name) == "original" else "clang-wasm"
    compile_once(wasmtime, component, work, marker, False)  # Populate Wasmtime cache.
    warm_runs = [compile_once(wasmtime, component, work, marker, False)
                 for _ in range(3)]
    warm = {key: statistics.median(run[key] for run in warm_runs)
            for key in warm_runs[0]}
    gzip_bytes = len(subprocess.run(["gzip", "-9", "-c", str(component)],
                                    capture_output=True, check=True).stdout)
    report = {
        "name": str(name),
        "raw_bytes": raw.stat().st_size,
        "stripped_bytes": component.stat().st_size,
        "gzip9_bytes": gzip_bytes,
        "warm": warm,
        "warm_runs": warm_runs,
        "cold": compile_once(wasmtime, component, work, marker, True),
    }
    result = subprocess.run([wasmtime, "run", "--invoke", "add",
                             str(work / "output.wasm"), "20", "22"],
                            capture_output=True, text=True, check=True)
    if result.stdout.strip() != "42":
        raise RuntimeError(result.stdout)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
