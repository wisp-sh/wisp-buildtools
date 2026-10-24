#!/usr/bin/env python3
"""Compile ordinary AssemblyScript sources in Wasm and execute the resulting exports."""

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
from src.runtime import wasmtime


def main():
    manifest_path = ROOT / "dist/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for name, info in manifest["artifacts"].items():
        actual = hashlib.sha256((ROOT / "dist" / name).read_bytes()).hexdigest()
        assert actual == info["sha256"], f"Artifact mismatch: {name}"
    report = {
        "toolchain": "assemblyscript",
        "compiler_execution": "Wasm in Wasmtime",
        "checks": [],
    }
    engine = wasmtime.Engine()
    with tempfile.TemporaryDirectory(prefix="wisp-assemblyscript-") as tmp:
        directory = pathlib.Path(tmp)
        # No Node or native compiler can be found by the normal build process.
        env = dict(os.environ, PATH=str(directory / "empty-path"))
        for filename, runtime, optimize, invocations in [
            ("add.ts", "incremental", 0, [("add", [20, 22], 42)]),
            (
                "algorithms.ts",
                "incremental",
                0,
                [
                    ("add", [20, 22], 42),
                    ("series", [9], 45),
                    ("twice", [1.25], 2.5),
                    ("checked", [7], 7),
                ],
            ),
            (
                "algorithms.ts",
                "minimal",
                1,
                [("add", [20, 22], 42), ("series", [9], 45)],
            ),
            ("algorithms.ts", "stub", 0, [("add", [20, 22], 42), ("series", [9], 45)]),
        ]:
            output = directory / f"{filename}-{runtime}-O{optimize}.wasm"
            command = [
                sys.executable,
                str(ROOT / "compile.py"),
                str(ROOT / "examples" / filename),
                "-o",
                str(output),
                "--runtime",
                runtime,
                "-O",
                str(optimize),
            ]
            result = subprocess.run(
                command, env=env, text=True, capture_output=True, timeout=180
            )
            if result.returncode:
                raise RuntimeError(result.stderr)
            module = wasmtime.Module.from_file(engine, str(output))
            assert not module.imports, f"{filename} has unexpected host imports"
            store = wasmtime.Store(engine)
            instance = wasmtime.Instance(store, module, [])
            exports = instance.exports(store)
            for name, args, expected in invocations:
                actual = exports[name](store, *args)
                assert actual == expected, (
                    f"{name}{args}: expected {expected}, got {actual}"
                )
                report["checks"].append(
                    {
                        "source": filename,
                        "runtime": runtime,
                        "optimize": optimize,
                        "function": name,
                        "arguments": args,
                        "result": actual,
                    }
                )
            if filename == "algorithms.ts":
                try:
                    exports["checked"](store, -1)
                except wasmtime.Trap:
                    report["checks"].append(
                        {
                            "source": filename,
                            "runtime": runtime,
                            "assertion_failure_traps": True,
                        }
                    )
                else:
                    raise AssertionError("assert() did not trap")
        output = directory / "abi.wasm"
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "compile.py"),
                str(ROOT / "examples/abi.ts"),
                "-o",
                str(output),
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=180,
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        module = wasmtime.Module.from_file(engine, str(output))
        assert [(item.module, item.name) for item in module.imports] == [
            ("host", "scale")
        ]
        store = wasmtime.Store(engine)
        linker = wasmtime.Linker(engine)
        linker.define_func(
            "host",
            "scale",
            wasmtime.FuncType([wasmtime.ValType.i32()], [wasmtime.ValType.i32()]),
            lambda value: value * 2,
        )
        exports = linker.instantiate(store, module).exports(store)
        for name, args, expected in [
            ("dispatch", [0, 6], 72),
            ("dispatch", [1, 6], 14),
            ("large", [4294967296], 8589934592),
            ("unicode", [], 2),
        ]:
            actual = exports[name](store, *args)
            assert actual == expected, (
                f"{name}{args}: expected {expected}, got {actual}"
            )
            report["checks"].append(
                {
                    "source": "abi.ts",
                    "function": name,
                    "arguments": args,
                    "result": actual,
                }
            )
        for label, text, diagnostic in [
            (
                "type-error",
                "export function add(a: i32): i32 { return missing; }",
                "TS2304",
            ),
            ("syntax-error", "export function add(: i32 {", "ERROR"),
        ]:
            source = directory / f"{label}.ts"
            source.write_text(text)
            output = directory / f"{label}.wasm"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "compile.py"),
                    str(source),
                    "-o",
                    str(output),
                ],
                env=env,
                text=True,
                capture_output=True,
                timeout=180,
            )
            assert result.returncode != 0, f"{label} compiled successfully"
            assert diagnostic in result.stderr, result.stderr
            assert not output.exists(), f"{label} left a Wasm output"
            report["checks"].append(
                {"source": label, "rejected": True, "diagnostic": diagnostic}
            )
            output.write_bytes(b"previous successful build")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "compile.py"),
                    str(source),
                    "-o",
                    str(output),
                ],
                env=env,
                text=True,
                capture_output=True,
                timeout=180,
            )
            assert result.returncode != 0, f"{label} compiled successfully"
            assert output.read_bytes() == b"previous successful build"
            report["checks"].append(
                {"source": label, "previous_output_preserved": True}
            )
    report["passed"] = True
    report["native_compiler_on_build_path"] = False
    report["fullyWasmHostedBuild"] = True
    (ROOT / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
