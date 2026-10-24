"""Build a small Clang/LLD consumer against the existing shared LLVM binaries."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from llvm.shared import manifest as provider_manifest, root as provider
from llvm.scripts.shared import code_digest, compile_driver, digest, rename_imports

ROOT = Path(__file__).resolve().parent


def build(sdk: Path, destination: Path, work: Path, lock: dict) -> dict:
    directory = provider()
    manifest = provider_manifest()
    llvm = ROOT.parent / "llvm/artifacts/llvm-24-d297af3-wasip3-o1"
    metadata = json.loads((llvm / "share/llvm/sdk.json").read_text())
    if metadata["llvm_revision"] != manifest["llvmRevision"] or metadata["host_target"] != "wasm32-wasip3":
        raise ValueError("TeaVM driver needs the installed provider's LLVM SDK headers")
    tool = Path(os.environ.get("WISP_BOOTSTRAP_WASM_TOOLS", ROOT.parent / "clang/.toolchains/wasm-tools-1.262.0-x86_64-linux/wasm-tools"))
    if digest(tool) != lock["bootstrapWasmTools"]["sha256"]:
        raise ValueError("TeaVM driver bootstrap needs the pinned native wasm-tools")
    work.mkdir(exist_ok=True)
    obj, driver, linked = [work / name for name in ["driver.o", "driver.so", "linked.wasm"]]
    compile_driver(sdk, llvm / "include", ROOT / "src/c-backend.cpp", obj)
    library = directory / "lib/llvm24.so.wasm"
    runtime = sdk / "share/wasi-sysroot/lib/wasm32-wasip3"
    subprocess.run([sdk / "bin/clang++", "--target=wasm32-wasip3", "-nostartfiles", "-shared",
                    runtime / "crt1-command.o", obj, "-Wl,-Bdynamic", library,
                    "-Wl,--skip-wit-component,--no-entry,--export=__main_argc_argv", "-lrt", "-ldl", "-o", driver], check=True)
    # The provider's unbundled runtime modules omit component type metadata.
    # Restore metadata from the pinned SDK, checking that their code is identical.
    runtimes = {}
    for filename, original in {"libc.so.wasm": "libc.so", "libcxx.so.wasm": "noeh/libc++.so",
                               "libcxxabi.so.wasm": "noeh/libc++abi.so", "libdl.so.wasm": "libdl.so"}.items():
        target = work / filename
        subprocess.run([tool, "strip", "-d", "^name$", runtime / original, "-o", target], check=True)
        if filename != "libdl.so.wasm" and code_digest(target) != code_digest(directory / "lib" / filename):
            raise ValueError(f"WASI SDK runtime differs from LLVM provider: {filename}")
        runtimes[filename] = target
    subprocess.run([tool, "component", "link", driver, library,
                    f"libc++.so={runtimes['libcxx.so.wasm']}",
                    f"libc++abi.so={runtimes['libcxxabi.so.wasm']}",
                    f"libc.so={runtimes['libc.so.wasm']}", f"libdl.so={runtimes['libdl.so.wasm']}",
                    "--stack-size", "16777216", "-o", linked], check=True)
    modules = work / "modules"
    subprocess.run([tool, "component", "unbundle", linked, "--threshold", "524288",
                    "--module-dir", modules, "-o", destination], check=True)
    known = {code_digest(directory / item["path"]): item["name"] for item in manifest["coreModules"]}
    names = {}
    for module in modules.glob("*.wasm"):
        identity = known.get(code_digest(module))
        if identity is None:
            raise ValueError(f"TeaVM driver changed an LLVM provider module: {module.name}")
        names[module.stem] = identity
    if set(names.values()) != set(known.values()):
        raise ValueError("TeaVM driver is missing a shared LLVM module")
    rename_imports(destination, names)
    subprocess.run([tool, "validate", destination], check=True)
    return {"sourceSha256": digest(ROOT / "src/c-backend.cpp"),
            "llvmProviderSha256": digest(directory / "manifest.json"),
            "llvmRebuilt": False, "sha256": digest(destination)}
