#!/usr/bin/env python3
"""Measure the P3 compiler launch and its in-process compile/link stages."""
import json
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path


def run_timed(command):
    started = time.monotonic_ns()
    result = subprocess.run(command, capture_output=True, text=True)
    elapsed = (time.monotonic_ns() - started) / 1e6
    if result.returncode:
        raise RuntimeError(f"{command}: {result.stderr}")
    return elapsed, result


def run_compiler(command):
    started = time.monotonic_ns()
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, bufsize=1)
    markers = {}
    stderr = []
    for line in process.stderr:
        stderr.append(line)
        if line.startswith("clang-p3: "):
            markers[line.strip()] = (time.monotonic_ns() - started) / 1e6
    stdout = process.stdout.read()
    status = process.wait()
    elapsed = (time.monotonic_ns() - started) / 1e6
    if status:
        raise RuntimeError(f"compiler exited {status}\n{''.join(stderr)}\n{stdout}")
    return markers, elapsed


def main():
    if len(sys.argv) != 5:
        raise SystemExit("usage: measure.py WASMTIME COMPILER WORK EMPTY_COMPONENT")
    wasmtime, compiler, work, empty = sys.argv[1:]
    work = Path(work)
    launch_times = [run_timed([wasmtime, "run", empty])[0] for _ in range(3)]

    command = [wasmtime, "run", "--dir", f"{work}::/work", compiler,
               "/work/input.cpp", "-o", "/work/output.wasm"]
    markers, elapsed = run_compiler(command)
    warm_peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss

    cold_command = [wasmtime, "run", "-C", "cache=n", "--dir",
                    f"{work}::/work", compiler, "/work/input.cpp", "-o",
                    "/work/output.wasm"]
    cold_markers, cold_elapsed = run_compiler(cold_command)
    cold_peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss

    ready = markers["clang-p3: ready"]
    codegen = markers["clang-p3: codegen-complete"]
    linked = markers["clang-p3: link-complete"]
    baseline = statistics.median(launch_times)
    output_run_ms, invocation = run_timed(
        [wasmtime, "run", "--invoke", "add", str(work / "output.wasm"), "20", "22"]
    )
    if invocation.stdout.strip() != "42":
        raise RuntimeError(f"unexpected add result: {invocation.stdout!r}")
    report = {
        "artifact_kind": "WASIp3 component",
        "wasmtime_startup_baseline_ms": baseline,
        "compiler_launch_to_ready_ms": ready,
        "compiler_startup_above_baseline_ms_estimate": max(0, ready - baseline),
        "codegen_ms": codegen - ready,
        "link_ms": linked - codegen,
        "compile_and_link_ms": linked - ready,
        "total_wasmtime_run_ms": elapsed,
        "peak_host_rss_kib": warm_peak,
        "cold_compiler_launch_to_ready_ms": cold_markers["clang-p3: ready"],
        "cold_compile_and_link_ms": (cold_markers["clang-p3: link-complete"]
                                      - cold_markers["clang-p3: ready"]),
        "cold_total_wasmtime_run_ms": cold_elapsed,
        "cold_peak_host_rss_kib": cold_peak,
        "output_bytes": (work / "output.wasm").stat().st_size,
        "output_instantiate_and_call_ms": output_run_ms,
    }
    (work / "measurements.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
