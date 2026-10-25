#!/usr/bin/env python3
"""Bootstrap a CPython Component that compiles user Python inside Wasm."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import sys
import tempfile
import urllib.request
import zipfile

from scripts.common import LOCK, MAX_COMPONENT_BYTES, ROOT, digest, invoke, run, tool, verified_package
from scripts.wasm import COMPONENT_HEADER, CORE_HEADER, public_component, section, sections, source_component

OPTIMIZER_FLAGS = ["-Oz", "--all-features", "--disable-compact-imports", "--strip-debug", "--strip-producers"]


def optimizer_cache_key(payload: bytes, optimizer: Path) -> str:
    configuration = {"input_sha256": hashlib.sha256(payload).hexdigest(),
                     "optimizer_sha256": digest(optimizer),
                     "flags": OPTIMIZER_FLAGS}
    return hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()


def install_componentize_py(cache=None, *, offline=False) -> Path:
    specification = LOCK["componentize_py"]
    archive = (ROOT / "dist/provision" if offline else ROOT / ".downloads") / specification["filename"]
    if not offline:
        archive.parent.mkdir(exist_ok=True)
    if not archive.exists():
        if offline:
            raise ValueError("installed Python package lacks the pinned provisioning wheel; reinstall the toolchain")
        with tempfile.NamedTemporaryFile(dir=archive.parent, delete=False) as output:
            temporary = Path(output.name)
            try:
                with urllib.request.urlopen(specification["url"], timeout=60) as response:
                    shutil.copyfileobj(response, output)
                output.close()
                if digest(temporary) != specification["sha256"]:
                    raise ValueError("downloaded componentize-py wheel has an unexpected SHA-256")
                temporary.replace(archive)
            finally:
                temporary.unlink(missing_ok=True)
    if digest(archive) != specification["sha256"]:
        raise ValueError(f"SHA-256 mismatch: {archive}")
    target = (cache or ROOT / ".toolchains") / "componentize-py"
    # Extract the verified wheel again, rather than trusting a cached native .so.
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            if not (target / entry.filename).resolve().is_relative_to(target.resolve()):
                raise ValueError("wheel entry escapes extraction directory")
        source.extractall(target)
    return target


def optimizer_environment(binary: Path) -> dict:
    environment = dict(os.environ)
    environment["BINARYEN_CORES"] = os.environ.get("BINARYEN_CORES", "1")
    environment["LD_LIBRARY_PATH"] = str(ROOT / "dist/provision")
    return environment


def optimize_runtime(data: bytes, work: Path, optimizer: Path, cache=None) -> bytes:
    cache = (cache or ROOT / ".toolchains") / "optimized-cores"
    cache.mkdir(parents=True, exist_ok=True)
    environment = optimizer_environment(optimizer)

    def rewrite(component):
        result = component[:8]
        for kind, payload in sections(component):
            if kind == 0:
                continue
            if kind == 4:
                payload = rewrite(payload)
            elif kind == 1 and payload.startswith(CORE_HEADER):
                key = optimizer_cache_key(payload, optimizer)
                optimized = cache / f"{key}.wasm"
                if not optimized.exists():
                    original = work / f"{key}.wasm"
                    original.write_bytes(payload)
                    temporary = work / f"{key}.optimized.wasm"
                    run([optimizer, original, *OPTIMIZER_FLAGS, "-o", temporary],
                        env=environment, timeout=600)
                    # Optimize once. Cache only validated Wasm, with a separate hash.
                    run([tool("wasm-tools"), "validate", temporary])
                    shutil.copyfile(temporary, optimized)
                    optimized.with_suffix(".sha256").write_text(digest(optimized))
                if digest(optimized) != optimized.with_suffix(".sha256").read_text():
                    raise ValueError(f"optimizer cache SHA-256 mismatch: {optimized}")
                payload = optimized.read_bytes()
            result += section(kind, payload)
        return result

    return rewrite(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cargo", default="cargo", help="direct Cargo executable for package bootstrap")
    parser.add_argument("--force", action="store_true", help="rebuild even when the package is valid")
    args = parser.parse_args()
    run([sys.executable, ROOT.parent / "wasm-tools/bootstrap.py"])
    dist = ROOT / "dist"
    if not args.force and verified_package(dist):
        print(f"Python toolchain is ready: {dist}")
        return
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        raise ValueError("this bootstrap currently pins Linux x86_64 native tools; emitted Wasm is portable")
    tools = {name: tool(name) for name in LOCK["tools"]}
    for name, path in tools.items():
        if digest(path) != LOCK["tools"][name]["sha256"]:
            raise ValueError(f"{name} differs from toolchain.lock.json: {path}")
    installed = install_componentize_py()
    with tempfile.TemporaryDirectory(prefix=".python-bootstrap-", dir=ROOT) as directory:
        work = Path(directory)
        runtime = work / "runtime.component.wasm"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(installed)
        environment.pop("VIRTUAL_ENV", None)
        run([sys.executable, "-P", "-c", "import componentize_py; componentize_py.script()",
             "-d", ROOT / "wit", "-w", "python-runtime", "componentize",
             "-p", ROOT / "runtime", "app", "-o", runtime], env=environment)
        runtime.write_bytes(optimize_runtime(runtime.read_bytes(), work, tools["wasm-opt"]))
        run([tools["wasm-tools"], "validate", runtime])
        provider = work / "source.component.wasm"
        provider.write_bytes(source_component((ROOT / "examples/add/main.py").read_bytes()))
        staging = work / "dist"
        staging.mkdir()
        # Native Cargo is bootstrap-only. User builds execute this Wasm module.
        cargo_environment = dict(os.environ)
        remaps = [f"--remap-path-prefix={Path.home().resolve()}=/build-user",
                  f"--remap-path-prefix={ROOT.parent.resolve()}=/wisp-buildtools"]
        # Encoded flags preserve paths containing spaces and override RUSTFLAGS.
        encoded = cargo_environment.get("CARGO_ENCODED_RUSTFLAGS")
        flags = encoded.split("\x1f") if encoded else shlex.split(cargo_environment.get("RUSTFLAGS", ""))
        cargo_environment["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join([*flags, *remaps])
        run([args.cargo, "build", "--manifest-path", ROOT / "packager/Cargo.toml",
             "--locked", "--release", "--target", "wasm32-wasip1", "-j", "1"], timeout=600, env=cargo_environment)
        run([args.cargo, "build", "--manifest-path", ROOT / "contract-packager/Cargo.toml",
             "--locked", "--offline", "--release", "--target", "wasm32-wasip1", "-j", "1"],
            env=dict(cargo_environment, CARGO_TARGET_DIR=str(work / "contract-target")))
        shutil.copyfile(work / "contract-target/wasm32-wasip1/release/wisp-python-public-exports.wasm",
                        staging / "public-exports.wasm")
        provision = staging / "provision"
        provision.mkdir()
        shutil.copyfile(ROOT / ".downloads" / LOCK["componentize_py"]["filename"],
                        provision / LOCK["componentize_py"]["filename"])
        for name in ("wasm-tools", "wasm-opt", "wac"):
            shutil.copy2(tools[name], provision / name)
        # Binaryen's shared library is shipped beside the pinned executable.
        binaryen_lib = tools["wasm-opt"].parent.parent / "lib/libbinaryen.so"
        if binaryen_lib.is_file():
            shutil.copy2(binaryen_lib, provision / "libbinaryen.so")
        shutil.copyfile(ROOT / "packager/target/wasm32-wasip1/release/wisp-python-packager.wasm",
                        staging / "python-packager.wasm")
        template = staging / "template.component.wasm"
        run([tools["wac"], "plug", runtime, "--plug", provider, "-o", template])
        shutil.copyfile(template, staging / "inference.component.wasm")
        template.write_bytes(public_component(template.read_bytes()))
        run([tools["wasm-tools"], "validate", template])
        if template.read_bytes()[:8] != COMPONENT_HEADER:
            raise ValueError("bootstrap did not produce a Component")
        if template.stat().st_size > MAX_COMPONENT_BYTES:
            raise ValueError(f"Python Component exceeds Wisp's 64 MiB limit: {template.stat().st_size} bytes")
        if invoke(template, "validate()") != "ok" or invoke(template, "add(20, 22)") != "42":
            raise ValueError("bootstrapped Python Component failed its guest smoke check")
        runtime_info = invoke(template, "runtime-info()")
        shutil.copy2(ROOT / "wit/runtime.wit", staging / "runtime.wit")
        shutil.copytree(ROOT / "runtime", staging / "runtime", ignore=shutil.ignore_patterns("__pycache__"))
        licenses = staging / "licenses"
        licenses.mkdir()
        if digest(ROOT / "LICENSE.python") != LOCK["python_license"]["sha256"]:
            raise ValueError("CPython license differs from toolchain.lock.json")
        shutil.copy2(ROOT / "LICENSE.python", licenses / "LICENSE.python")
        for path in installed.rglob("LICENSE*"):
            if path.is_file():
                shutil.copy2(path, licenses / path.name)
        files = {str(p.relative_to(staging)): digest(p) for p in sorted(staging.rglob("*")) if p.is_file()}
        manifest = {"schema": 1, "language": "python", "bootstrap": LOCK,
                    "inputs": {name: digest(ROOT / name) for name in
                               ("bootstrap.py", "scripts/wasm.py", "runtime/app.py", "runtime/inference.py", "wit/runtime.wit",
                                "packager/Cargo.toml", "packager/Cargo.lock", "packager/src/main.rs",
                                "contract-packager/Cargo.toml", "contract-packager/Cargo.lock", "contract-packager/src/main.rs")},
                    "compiler_host": "wasm32, guest CPython builtins.compile",
                    "fullyWasmHostedBuild": True,
                    "packager_host": "wasm32-wasip1",
                    "packager_bootstrap_rust": run(["rustc", "--version"], capture=True).stdout.strip(),
                    "output": "WebAssembly Component", "runtime_info": runtime_info,
                    "component_bytes": template.stat().st_size,
                    "native_bootstrap_tools": {name: {"path": path.name, "sha256": digest(path)}
                                               for name, path in tools.items()},
                    "sha256": files}
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        previous = work / "previous"
        if dist.exists():
            dist.rename(previous)
        try:
            staging.rename(dist)
        except BaseException:
            if previous.exists():
                previous.rename(dist)
            raise
    print(f"Python toolchain ready: {dist}, {template.name} is {manifest['component_bytes']} bytes")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from error
