#!/usr/bin/env python3
"""Rebuild only the small Clang consumer, reusing verified shared LLVM modules."""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from llvm.shared import manifest as provider_manifest

spec = importlib.util.spec_from_file_location("shared_builder", ROOT / "llvm/scripts/shared.py")
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", type=Path, default=ROOT / "llvm/dist/shared")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    provider = args.provider.resolve()
    entries = provider_manifest(provider)["coreModules"]
    sdk = ROOT / "clang/.toolchains/wasi-sdk-34.0-x86_64-linux"
    tool = ROOT / "clang/.toolchains/wasm-tools-1.262.0-x86_64-linux/wasm-tools"
    library = provider / "lib/llvm24.so.wasm"
    runtime = sdk / "share/wasi-sysroot/lib/wasm32-wasip3"
    output = args.out.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".clang-consumer-", dir=output.parent) as temporary:
        work = Path(temporary)
        obj, driver = (work / name for name in ["clang.o", "clang.so"])
        shared.compile_driver(sdk, ROOT / "llvm/artifacts/llvm-24-d297af3-wasip3-o1/include",
                              ROOT / "clang/src/clang_wasm_main.cpp", obj)
        shared.run([sdk / "bin/clang++", "--target=wasm32-wasip3", "-nostartfiles", "-shared",
                    runtime / "crt1-command.o", obj, "-Wl,-Bdynamic", library,
                    "-Wl,--threads=1,--skip-wit-component,--no-entry,--export=__main_argc_argv",
                    "-lrt", "-ldl", "-o", driver])
        # libdl is small and remains embedded. The other module bytes must stay
        # identical to the provider already installed, including legacy names.
        # Unbundling removes component-type metadata from the provider's libc.
        # Use the pinned SDK originals for linking, then verify every extracted
        # code section against the existing provider before accepting output.
        runtime_files = {"libc.so": "libc.so.wasm", "noeh/libc++.so": "libcxx.so.wasm",
                         "noeh/libc++abi.so": "libcxxabi.so.wasm", "libdl.so": "libdl.so.wasm"}
        for source, filename in runtime_files.items():
            shared.run([tool, "strip", "-d", "^name$", runtime / source, "-o", work / filename])
        link_command = [tool, "component", "link", driver, library,
                    f"libc++.so={work / 'libcxx.so.wasm'}",
                    f"libc++abi.so={work / 'libcxxabi.so.wasm'}",
                    f"libc.so={work / 'libc.so.wasm'}", f"libdl.so={work / 'libdl.so.wasm'}",
                    "--stack-size", "16777216", "-o", "/dev/stdout"]
        extracted, consumer = work / "modules", work / "clang.component.wasm"
        # Pipe the combined component into unbundling. Keeping it on disk beside
        # its extracted modules would double scratch usage to about 200 MiB.
        with subprocess.Popen([str(arg) for arg in link_command], stdout=subprocess.PIPE) as link:
            try:
                subprocess.run([str(tool), "component", "unbundle", "-", "--threshold", "524288",
                                "--module-dir", str(extracted), "-o", str(consumer)],
                               stdin=link.stdout, check=True)
            finally:
                link.stdout.close()
            if link.wait() != 0:
                raise subprocess.CalledProcessError(link.returncode, link_command)
        known = {shared.code_digest(provider / entry["path"]): entry["name"] for entry in entries}
        names = {}
        for module in extracted.glob("*.wasm"):
            identity = known.get(shared.code_digest(module))
            if identity is None:
                raise ValueError(f"relink changed shared LLVM module: {module.name}")
            names[module.stem] = identity
        if set(names.values()) != {entry["name"] for entry in entries}:
            raise ValueError("relinked consumer has an incomplete shared-module inventory")
        shared.rename_imports(consumer, names)
        shared.run([tool, "validate", consumer])
        os.replace(consumer, output)
        print(f"Rebuilt Wasm Clang consumer: {output} ({output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
