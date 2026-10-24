#!/usr/bin/env python3
"""Build the Grain compiler guest from pinned QuickJS and upstream Grain assets."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def integrity():
    lock = json.loads((ROOT / "toolchain.lock.json").read_text())
    manifest = json.loads((ROOT / "dist/manifest.json").read_text())
    if manifest.get("bootstrap") != lock or not manifest.get("fullyWasmHostedBuild"):
        raise ValueError("Grain distribution does not match the pinned bootstrap")
    inputs = manifest.get("bootstrapInputs", {})
    for name, path in [("recipe", ROOT / "bootstrap.py"), ("runner", ROOT / "src/quickjs-wasi.c"),
                       ("runtime", ROOT / "src/node-shim.js"), ("extractor", ROOT / "src/extract-compiler.py"),
                       ("licenses", ROOT / "src/licenses.py")]:
        if inputs.get(name) != digest(path):
            raise ValueError(f"Grain bootstrap source changed: {path.relative_to(ROOT)}")
    files = manifest.get("sha256", {})
    required = {"grainc.wasm", "grainc.js", "runtime.js", "stdlib/pervasives.gr", "upstream.json"}
    if not required.issubset(files):
        raise ValueError("Grain distribution is incomplete")
    for name, expected in files.items():
        path = ROOT / "dist" / name
        if not path.resolve().is_relative_to(ROOT / "dist") or path.is_symlink() or digest(path) != expected:
            raise ValueError(f"Grain artifact is missing or changed: {name}")
    if digest(ROOT / "dist/grainc.js") != lock["compiler"]["javascript"]["sha256"]:
        raise ValueError("Grain JavaScript compiler is not the pinned upstream artifact")
    if (ROOT / "dist/grainc.wasm").read_bytes()[:8] != b"\0asm\x01\0\0\0":
        raise ValueError("Grain command guest is not Core Wasm")
    return manifest


def rebuild(args):
    lock = json.loads((ROOT / "toolchain.lock.json").read_text())
    specification = importlib.util.spec_from_file_location("grain_extract", ROOT / "src/extract-compiler.py")
    extract = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(extract)
    if (extract.RELEASE != lock["compiler"]["release"] or extract.COMPILER != lock["compiler"]["javascript"]
            or extract.SOURCE != lock["source"]["archive"] or extract.STDLIB != lock["stdlib"]):
        raise ValueError("extractor input pins differ from toolchain.lock.json")
    sdk = Path(args.wasi_sdk).resolve() if args.wasi_sdk else ROOT.parent / "clang/.toolchains/wasi-sdk-34.0-x86_64-linux"
    compiler, linker = sdk / "bin/clang", sdk / "bin/wasm-ld"
    if not compiler.is_file() or not linker.is_file():
        raise ValueError("bootstrap needs WASI SDK 34.0; set WISP_GRAIN_WASI_SDK or --wasi-sdk")
    if digest(compiler) != lock["bootstrapWasiSdk"]["clangSha256"] or digest(linker) != lock["bootstrapWasiSdk"]["wasmLdSha256"]:
        raise ValueError("bootstrap requires the pinned Linux x86_64 WASI SDK 34.0 executables")
    archive = ROOT / ".cache" / f"quickjs-{lock['quickjs']['archiveVersion']}.tar.xz"
    archive.parent.mkdir(exist_ok=True)
    if not archive.is_file():
        if args.offline:
            raise ValueError("offline bootstrap needs the cached QuickJS archive")
        with urllib.request.urlopen(lock["quickjs"]["url"], timeout=90) as response:
            data = response.read(lock["quickjs"]["size"] + 1)
        if hashlib.sha256(data).hexdigest() != lock["quickjs"]["sha256"]:
            raise ValueError("QuickJS archive SHA-256 mismatch")
        archive.write_bytes(data)
    if digest(archive) != lock["quickjs"]["sha256"]:
        raise ValueError("cached QuickJS archive SHA-256 mismatch")
    if args.offline:
        for name, pin in [(f"grain-linux-x64-{extract.VERSION}", extract.RELEASE),
                          (f"grain-{extract.COMMIT}.tar.gz", extract.SOURCE),
                          (f"stdlib-{extract.VERSION}.tgz", extract.STDLIB)]:
            path = ROOT / ".cache" / name
            if not path.is_file() or digest(path) != pin["sha256"]:
                raise ValueError(f"offline bootstrap needs the verified cached {name}")
    with tempfile.TemporaryDirectory(prefix=".bootstrap-", dir=ROOT) as directory:
        work = Path(directory)
        dist = work / "dist"
        extract.main(dist)
        # The compiler reads Grain source, not npm scripts or documentation.
        for path in sorted((dist / "stdlib").rglob("*"), reverse=True):
            if path.is_file() and path.suffix != ".gr" and path.name != "LICENSE":
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        with tarfile.open(archive) as bundle:
            bundle.extractall(work / "source", filter="data")
        source = work / "source" / f"quickjs-{lock['quickjs']['version']}"
        quickjs = source / "quickjs.c"
        text = quickjs.read_text()
        atomics = "#if !defined(__EMSCRIPTEN__)\n#define CONFIG_ATOMICS"
        allocation = "#elif defined(__EMSCRIPTEN__)\n    return 0;"
        if text.count(atomics) != 1 or text.count(allocation) != 1:
            raise ValueError("QuickJS WASI patch no longer matches its pinned source")
        quickjs.write_text(text.replace(atomics, "#if !defined(__EMSCRIPTEN__) && !defined(__wasi__)\n#define CONFIG_ATOMICS")
                          .replace(allocation, "#elif defined(__EMSCRIPTEN__) || defined(__wasi__)\n    return 0;"))
        dtoa = source / "dtoa.c"
        if dtoa.read_text().count("#include <setjmp.h>\n") != 1:
            raise ValueError("QuickJS dtoa include patch no longer matches")
        # dtoa does not call setjmp. The unused include requires the EH proposal in wasi-libc.
        dtoa.write_text(dtoa.read_text().replace("#include <setjmp.h>\n", ""))
        flags = ["--target=wasm32-wasip1", "-O2", "-D_GNU_SOURCE", f'-DCONFIG_VERSION="{lock["quickjs"]["version"]}"',
                 f"-ffile-prefix-map={ROOT}=/wisp/grain", f"-ffile-prefix-map={work}=/wisp/grain/bootstrap", "-I", source]
        sources = [ROOT / "src/quickjs-wasi.c", *[source / f"{name}.c" for name in ["quickjs", "dtoa", "libregexp", "libunicode", "cutils"]]]
        objects = []
        for index, path in enumerate(sources):
            obj = work / f"unit-{index}.o"
            subprocess.run([str(value) for value in [compiler, *flags, "-c", path, "-o", obj]], check=True)
            objects.append(obj)
        subprocess.run([str(value) for value in [compiler, "--target=wasm32-wasip1", *objects, "-lm",
                        "-Wl,-z,stack-size=16777216", "-Wl,--strip-all", "-o", dist / "grainc.wasm"]], check=True)
        shutil.copyfile(ROOT / "src/node-shim.js", dist / "runtime.js")
        shutil.copyfile(source / "LICENSE", dist / "licenses/QuickJS-MIT.txt")
        specification = importlib.util.spec_from_file_location("grain_licenses", ROOT / "src/licenses.py")
        licenses = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(licenses)
        licenses.main(dist / "licenses", offline=args.offline)
        manifest = {
            "schema": 1, "version": lock["version"], "bootstrap": lock,
            "compilerExecution": "Grain js_of_ocaml frontend and Binaryen wasm2js backend inside QuickJS Wasm",
            "compilerHost": "wasm32-wasip1", "fullyWasmHostedBuild": True, "nodeRequired": False,
            "nativeReleaseExecuted": False,
            "bootstrapInputs": {"clang": digest(compiler), "wasmLd": digest(linker),
                                "recipe": digest(ROOT / "bootstrap.py"), "runner": digest(ROOT / "src/quickjs-wasi.c"),
                                "runtime": digest(ROOT / "src/node-shim.js"),
                                "extractor": digest(ROOT / "src/extract-compiler.py"), "licenses": digest(ROOT / "src/licenses.py")},
            "hostRequirements": {"wasmtime": {"provider": "wisp", "testedVersion": "49.0.1", "environment": "WISP_WASMTIME"}},
            "runtimeCache": {"optional": True, "description": "--cache-dir stores QuickJS bytecode and compiled Grain library objects outside the package"},
            "sha256": {path.relative_to(dist).as_posix(): digest(path) for path in sorted(dist.rglob("*")) if path.is_file()},
        }
        (dist / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        old = ROOT / ".dist-previous"
        if old.exists():
            shutil.rmtree(old)
        if (ROOT / "dist").exists():
            (ROOT / "dist").replace(old)
        dist.replace(ROOT / "dist")
        shutil.rmtree(old, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--wasi-sdk", default=os.environ.get("WISP_GRAIN_WASI_SDK"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.check and args.rebuild:
        parser.error("--check and --rebuild cannot be combined")
    try:
        if args.rebuild:
            rebuild(args)
        else:
            try:
                integrity()
            except (OSError, ValueError):
                if args.check:
                    raise ValueError("Grain distribution is missing or changed; rerun bootstrap")
                rebuild(args)
        manifest = integrity()
        print(json.dumps({"ready": True, "version": manifest["version"], "fullyWasmHostedBuild": True})
              if args.json else f"Grain {manifest['version']} Wasm compiler is ready")
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as error:
        raise SystemExit(f"Grain bootstrap: {error}") from error


if __name__ == "__main__":
    main()
