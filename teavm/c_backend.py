"""Compile TeaVM's generated C and runtime to WASI inside the shared LLVM guest."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

from llvm.shared import preload_args, root as llvm_root, manifest as llvm_manifest
from wasm_tools import runtime



def verify_provider(dist: Path) -> dict:
    from .compile import digest
    manifest=json.loads((dist/'manifest.json').read_text())
    provider=llvm_manifest()
    actual=digest(llvm_root()/'manifest.json')
    expected=manifest['cBackend']['llvmProviderSha256']
    if actual != expected:
        pin=json.loads((Path(__file__).parent/'llvm-compatibility.json').read_text())
        identities={m['name']:m['sha256'] for m in provider['coreModules']}
        if (expected!=pin['originalProviderManifestSha256']
                or digest(dist/'bin/c-backend.wasm')!=pin['consumerSha256']
                or provider['llvmRevision']!=pin['llvmRevision']
                or provider['profile']!=pin['profile']
                or identities!=pin['coreModuleSha256']):
            raise ValueError('TeaVM C driver differs from the installed shared LLVM provider')
    return {'manifestSha256':actual,'bootstrapManifestSha256':expected,
            'llvmRevision':provider['llvmRevision'],'coreModules':provider['coreModules'],
            'compatibility':'exact shared module bytes; consumer metadata may differ'}

def build(work: Path, dist: Path, *, component: bool, timeout: float) -> dict:
    generated = work / "output"
    exports = (generated / "exports.txt").read_text().splitlines()
    if len(set(exports)) != len(exports) or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in exports):
        raise ValueError("invalid or duplicate TeaVM scalar exports")
    if any(name in {"_initialize", "_start", "cabi_realloc_adapter", "wisp_java_main", "wisp_component_initialize"} for name in exports):
        raise ValueError("TeaVM export conflicts with a reserved runtime entry point")
    manifest = json.loads((dist / "manifest.json").read_text())
    headers = Path(os.environ.get("WISP_CLANG_RESOURCE_DIR", Path(__file__).resolve().parents[1] / "clang/include"))
    from .compile import digest
    provider_record = verify_provider(dist)
    for name, expected in manifest["clangResources"].items():
        if digest(headers / name) != expected:
            raise ValueError(f"Clang resource header differs from TeaVM's bootstrap: {name}")
    common = ["-triple", "wasm32-unknown-wasi", "-emit-obj", "-x", "c", "-std=gnu11", "-O1", "-w", "-fwrapv", "-relaxed-aliasing",
              "-fgnuc-version=4.2.1", "-nostdsysteminc", "-nobuiltininc", "-internal-isystem", "/clang",
              "-isystem", "/toolchain/sysroot/include", "-target-feature", "+exception-handling",
              "-target-feature", "+multivalue", "-target-feature", "+reference-types", "-exception-model=wasm",
              "-mllvm", "-wasm-enable-sjlj", "-mllvm", "-wasm-use-legacy-eh=false",
              "-D_WASI_EMULATED_MMAN", "-D_WASI_EMULATED_SIGNAL", "-D_WASI_EMULATED_PROCESS_CLOCKS"]
    if component:
        common.append("-DWISP_REACTOR")
    units = [[*common, "/project/output/all.c", "-o", "/project/generated.o"],
             [*common, "/toolchain/wasi-entry.c", "-o", "/project/entry.o"]]
    startup = "crt1-reactor.o" if component else "crt1-command.o"
    entrypoint = "_initialize" if component else "_start"
    link = ["--no-entry", "--strip-all", "--export-memory", f"--export={entrypoint}",
            *[f"--export={name}" for name in exports], "-z", "stack-size=1048576",
            f"/toolchain/sysroot/lib/{startup}", "/project/generated.o", "/project/entry.o",
            "-L/toolchain/sysroot/lib", "-lc", "-lm", "-lsetjmp", "-lwasi-emulated-mman",
            "-lwasi-emulated-signal", "-lwasi-emulated-process-clocks", "-lwasi-emulated-getpid",
            "/toolchain/sysroot/lib/libclang_rt.builtins.a", "-o", "/project/output/app.wasm"]
    if component:
        link.append("--export=cabi_realloc_adapter")
    (work / "c-build.json").write_text(json.dumps({"compile": units, "link": link}, indent=2) + "\n")
    command = [str(runtime()), "run", "-S", "p3=y", *preload_args(), "--dir", f"{work}::/project",
               "--dir", f"{dist}::/toolchain", "--dir", f"{headers}::/clang",
               str(dist / "bin/c-backend.wasm"), "build", "/project/c-build.json"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "WASI C backend failed")
    return {"execution": "Wasm-hosted Clang/LLD", "command": command, "exports": exports,
            "backendSha256": digest(dist / "bin/c-backend.wasm"), "llvmProvider": provider_record, "fullyWasmHostedBuild": True}
