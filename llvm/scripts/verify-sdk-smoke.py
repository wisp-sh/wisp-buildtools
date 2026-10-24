#!/usr/bin/env python3
"""Compile against a relocated SDK, run LLVM+LLD in Wasm, execute its output."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def run(arguments, **kwargs):
    result = subprocess.run([str(arg) for arg in arguments], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    if result.returncode:
        raise RuntimeError("command failed: " + " ".join(str(arg) for arg in arguments) + "\n" + result.stdout + result.stderr)
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--wasi-sdk", type=Path, required=True)
    parser.add_argument("--wasmtime", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    sdk, wasi_sdk, wasmtime, out = [path.resolve() for path in
                                     [args.sdk, args.wasi_sdk, args.wasmtime, args.out]]
    if out.exists():
        parser.error("--out must not exist")
    out.mkdir(parents=True)
    # Deliberately relocate the SDK; no compiler input references its source build.
    relocated = out / "relocated sdk"
    shutil.copytree(sdk, relocated)
    verification = json.loads(run([ROOT / "scripts/sdk.py", "verify", relocated]))
    config = relocated / "bin/llvm-config"
    expected_prefix = run([config, "--prefix"]).strip()
    assert expected_prefix == str(relocated), expected_prefix
    assert run([config, "--host-target"]).strip() == "wasm32-wasip3"
    assert "-lLLVMCore" in run([config, "--libs", "core"])
    rejected = subprocess.run([str(config), "--libs", "x86"], capture_output=True)
    assert rejected.returncode != 0, "SDK advertised unavailable x86 backend"
    build = out / "build"
    configure = run(["cmake", "-S", ROOT / "examples/sdk-smoke", "-B", build,
                     "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_CXX_FLAGS_RELEASE=-O1 -DNDEBUG",
                     "-DCMAKE_TOOLCHAIN_FILE=" + str(wasi_sdk / "share/cmake/wasi-sdk-p3.cmake"),
                     "-DCMAKE_FIND_ROOT_PATH_MODE_PACKAGE=BOTH",
                     "-DWispLLVM_DIR=" + str(relocated / "lib/cmake/WispLLVM")])
    compile_log = run(["cmake", "--build", build, "--parallel", "2"])
    compiler = build / "sdk-smoke"
    command = [wasmtime, "run", "-S", "p3", "--dir", str(out) + "::/work", compiler,
               "/work/add.o", "/work/add.wasm"]
    compiler_log = run(command)
    value = run([wasmtime, "run", "--invoke", "add", out / "add.wasm", "20", "22"]).strip()
    assert value == "42", value
    report = {"sdk": verification, "relocated_prefix": str(relocated),
              "compiler": str(compiler), "application": str(out / "add.wasm"),
              "result": int(value), "native_llvm_execution": False,
              "rust_compatibility": "not-verified"}
    (out / "configure.log").write_text(configure)
    (out / "compile.log").write_text(compile_log)
    (out / "compiler.log").write_text(compiler_log)
    (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
