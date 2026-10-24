#!/usr/bin/env python3
"""Check guest compilation, typed calls, Python semantics and failure atomicity."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from scripts.common import MAX_COMPONENT_BYTES, ROOT, digest, guest_tools, invoke, run, tool, verified_package


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=ROOT / "verification.json")
    args = parser.parse_args()
    if not verified_package(ROOT / "dist"):
        raise ValueError("run python3 bootstrap.py before verification")
    checks = []
    environment = {**os.environ, "PATH": "/nonexistent", "WISP_WASMTIME": str(tool("wasmtime"))}

    def passed(description):
        checks.append(description)
        print(f"PASS {description}")

    def compile_source(source, output):
        return run([sys.executable, ROOT / "compile.py", source, "-o", output], capture=True, env=environment)

    with tempfile.TemporaryDirectory(prefix="python-verify-") as directory:
        work = Path(directory)
        add = work / "add.component.wasm"
        compile_source(ROOT / "examples/add/main.py", add)
        run(guest_tools.command("validate", add))
        if add.stat().st_size > MAX_COMPONENT_BYTES:
            raise ValueError("Component exceeds Wisp's 16 MiB limit")
        passed("valid Component fits Wisp's 16 MiB limit")
        for a, b in [(20, 22), (-123, 65), (0, 0), (2147483640, 7), (-2147483648, 0)]:
            actual = invoke(add, f"add({a}, {b})")
            if actual != str(a + b):
                raise ValueError(f"add({a}, {b}): {actual}")
        passed("typed add returns correct results for varying and boundary inputs")
        runtime_info = invoke(add, "runtime-info()")
        if "CPython 3.14" not in runtime_info or "platform=wasi" not in runtime_info:
            raise ValueError(f"expected guest CPython: {runtime_info}")
        passed("Python parser and compiler run in guest CPython on WASI")
        repeated = work / "repeat.wasm"
        compile_source(ROOT / "examples/add/main.py", repeated)
        if digest(repeated) != digest(add):
            raise ValueError("repeated source compilation changed the Component")
        passed("repeated compilation from the same template is deterministic")
        stdlib = work / "stdlib.wasm"
        compile_source(ROOT / "examples/stdlib/main.py", stdlib)
        for a, b in [(20, 22), (-31, 45), (1000, 2)]:
            if invoke(stdlib, f"add({a}, {b})") != str(a + b):
                raise ValueError("Python standard library example returned the wrong result")
        passed("guest stdlib, classes, comprehensions and lambdas execute real Python")
        algorithm = work / "algorithm.py"
        algorithm.write_text("""import sys
assert sys.platform == 'wasi'
def add(a: int, b: int) -> int:
    def triangular(n):
        return sum(index for index in range(abs(n) + 1))
    return triangular(a) - triangular(b)
""")
        changed = work / "algorithm.wasm"
        compile_source(algorithm, changed)
        if invoke(changed, "add(5, 3)") != "9" or invoke(changed, "add(4, 1)") != "9":
            raise ValueError("changed Python function body was not executed")
        passed("changing function bodies changes behavior using guest-only Python compilation")
        source = work / "named.py"
        source.write_text("def multiply(a: int, b: int) -> int:\n    return a * b\n")
        named = work / "named.wasm"
        run([sys.executable, ROOT / "compile.py", source, "--entry", "multiply", "-o", named], capture=True, env=environment)
        if invoke(named, "add(6, 7)") != "42":
            raise ValueError("--entry did not select the named Python callable")
        passed("--entry selects a Python callable behind the fixed typed export")
        negative_cases = {
            "invalid syntax": ("def add(a: int, b: int) -> int:\n    return a +\n", "SyntaxError"),
            "missing export": ("def subtract(a: int, b: int) -> int:\n    return a - b\n", "TypeError"),
            "invalid annotations": ("def add(a, b):\n    return a + b\n", "TypeError"),
            "invalid signature": ("def add(a: int, b: int, c: int) -> int:\n    return a+b+c\n", "TypeError"),
            "top level exception": ("raise RuntimeError('initialization failed')\n", "RuntimeError"),
        }
        for label, (source_text, diagnostic) in negative_cases.items():
            source = work / "bad.py"
            source.write_text(source_text)
            nonexistent = work / "not-written.wasm"
            existing = work / "existing.wasm"
            existing.write_bytes(b"existing output must survive")
            for target in (nonexistent, existing):
                result = subprocess.run([sys.executable, str(ROOT / "compile.py"), str(source), "-o", str(target)],
                                        text=True, capture_output=True, timeout=60, env=environment)
                if result.returncode == 0 or diagnostic not in result.stderr:
                    raise ValueError(f"{label} was not rejected by guest CPython: {result.stderr}")
            if nonexistent.exists() or existing.read_bytes() != b"existing output must survive":
                raise ValueError(f"{label} damaged an output")
        passed("guest rejects syntax, export, signature and initialization errors atomically")
        for body, description in [("return str(a + b)", "wrong return type"),
                                  ("return 2147483648", "out of range s32")]:
            source = work / "result.py"
            source.write_text(f"def add(a: int, b: int) -> int:\n    {body}\n")
            component = work / "result.wasm"
            compile_source(source, component)
            result = subprocess.run([str(tool("wasmtime")), "run", "--invoke", "add(1, 2)", str(component)],
                                    text=True, capture_output=True, timeout=30)
            if result.returncode == 0:
                raise ValueError(f"{description} was accepted by the typed adapter")
        passed("typed adapter traps on wrong Python return types and s32 overflow")
        report = {"schema": 1, "status": "passed", "checks": checks,
                  "runtime_info": runtime_info, "component_bytes": add.stat().st_size,
                  "max_component_bytes": MAX_COMPONENT_BYTES,
                  "component_sha256": digest(add),
                  "source_compilation": "Wasm guest CPython compile and exec",
                  "fullyWasmHostedBuild": True,
                  "native_toolchain_removed_from_path": True,
                  "guest_build_stages": ["WASI source-provider packaging", "WASI wasm-tools validation",
                                         "CPython compile/exec"],
                  "native_orchestration": ["Python file I/O and process orchestration", "Wasmtime execution"]}
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(f"Report: {args.report}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from error
