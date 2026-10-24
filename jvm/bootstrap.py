#!/usr/bin/env python3
"""Build the pinned Bovine interpreter as WASI and install JDK class data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

from runtime import ROOT, digest, integrity

CACHE = ROOT / ".cache"


def fetch(item, target, offline):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and digest(target) == item["sha256"]:
        return target
    if offline:
        raise ValueError(f"missing or changed bootstrap input: {target}")
    with tempfile.NamedTemporaryFile(prefix=".download-", dir=target.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            with urllib.request.urlopen(item["url"], timeout=120) as response:
                shutil.copyfileobj(response, output)
            output.close()
            size = item.get("bytes", item.get("size"))
            if digest(temporary) != item["sha256"] or (size and temporary.stat().st_size != size):
                raise ValueError(f"bootstrap input checksum mismatch: {target.name}")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return target


def run(arguments, **kwargs):
    subprocess.run(list(map(str, arguments)), check=True, **kwargs)


def extract(archive, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".extract-", dir=destination.parent) as directory:
        work = Path(directory)
        with tarfile.open(archive) as source:
            source.extractall(work, filter="data")
        children = list(work.iterdir())
        if len(children) != 1 or not children[0].is_dir():
            raise ValueError(f"expected one source directory: {archive}")
        if destination.exists():
            shutil.rmtree(destination)
        children[0].rename(destination)
    return destination


def sdk(lock):
    root = Path(os.environ.get("WISP_JVM_WASI_SDK", ROOT.parent / "clang/.toolchains/wasi-sdk-34.0-x86_64-linux")).resolve()
    for name, expected in [("clang", lock["clangSha256"]), ("wasm-ld", lock["wasmLdSha256"])]:
        path = root / "bin" / name
        if not path.is_file() or digest(path) != expected:
            raise ValueError(f"set WISP_JVM_WASI_SDK to the pinned WASI SDK {lock['version']} ({name})")
    return root


def build_guest(source, zlib, sdk_root, output):
    work = ROOT / ".build"
    work.mkdir(exist_ok=True)
    port = work / "source"
    if port.exists():
        shutil.rmtree(port)
    port.mkdir()
    for name in ["vm", "natives", "vendor", "codegen"]:
        shutil.copytree(source / name, port / name)
    for patch in sorted((ROOT / "patches").glob("*.patch")):
        run(["patch", "-p1", "--batch", "--forward", "--input", patch], cwd=port)
    (work / "config.h").write_text("""#define HAVE_GETENV 1
#define DTRACE_ENABLED 0
#define DCHECKS_ENABLED 0
#define PLATFORM_LITTLE_ENDIAN 1
#define bswap16 __builtin_bswap16
#define bswap32 __builtin_bswap32
#define bswap64 __builtin_bswap64
""")
    natives = [*sorted((port / "natives/share").rglob("*.c")), *sorted((ROOT / "src").glob("java-*.c"))]
    registration = work / "natives_registration.c"
    registration.write_bytes(subprocess.check_output([sys.executable, str(port / "codegen/scan_native_declarations.py"), " ".join(map(str, natives))]))
    sources = [*sorted((port / "vm").glob("*.c")), *sorted((port / "vm/wasm").glob("*.c")),
               port / "vendor/stb_ds.c", *natives, registration, ROOT / "src/jvm-main.c",
               *[zlib / (name + ".c") for name in ["adler32", "crc32", "inflate", "inftrees", "inffast", "zutil"]]]
    flags = ["--target=wasm32-wasip1", "-std=gnu23", "-O2", "-D_GNU_SOURCE",
             "-D_WASI_EMULATED_SIGNAL", "-D_WASI_EMULATED_MMAN", "-fwasm-exceptions", "-mllvm", "-wasm-enable-sjlj",
             "-mllvm", "-wasm-use-legacy-eh=false", "-I", str(work), "-I", str(port / "vm"),
             "-I", str(port / "vendor"), "-I", str(zlib), "-Wno-deprecated-non-prototype",
             "-Wno-incompatible-pointer-types", "-Wno-unknown-pragmas"]
    identity = hashlib.sha256(" ".join(flags).encode())
    for path in sorted([*port.rglob("*.h"), *zlib.glob("*.h"), work / "config.h"]):
        identity.update(str(path.relative_to(ROOT)).encode())
        identity.update(path.read_bytes())
    key = identity.hexdigest()[:16]
    objects = []
    for index, source_file in enumerate(sources):
        obj = work / f"{hashlib.sha256(str(source_file).encode()).hexdigest()[:16]}-{key}.o"
        objects.append(obj)
        if not obj.is_file() or obj.stat().st_mtime < source_file.stat().st_mtime:
            print(f"[{index + 1}/{len(sources)}] {source_file.name}", flush=True)
            # Enable tail calls for dispatch only. The VM's other translation
            # units must keep the target feature set verified by this port.
            tail_calls = ["-mtail-call"] if source_file == port / "vm/interpreter2.c" else []
            run([sdk_root / "bin/clang", *flags, *tail_calls, "-c", source_file, "-o", obj])
    run([sdk_root / "bin/clang", "--target=wasm32-wasip1", "-mtail-call", "-fwasm-exceptions", *objects,
         "-lm", "-lsetjmp", "-lwasi-emulated-signal", "-lwasi-emulated-mman", "-Wl,-z,stack-size=16777216",
         "-Wl,--max-memory=2147483648", "-o", output])


def jdk_data(archive, lock, dist):
    source = extract(archive, CACHE / "temurin23")
    modules = source / "lib/modules"
    if digest(modules) != lock["modulesSha256"]:
        raise ValueError("JDK module image differs from the pinned release")
    target = dist / "jdk"
    target.mkdir()
    for name in ["conf", "legal"]:
        shutil.copytree(source / name, target / name, symlinks=False)
    shutil.copyfile(source / "release", target / "release")
    (target / "lib").mkdir()
    for name in ["modules", "tzdb.dat"]:
        if (source / "lib" / name).is_file():
            shutil.copyfile(source / "lib" / name, target / "lib" / name)
    if (source / "lib/security").is_dir():
        shutil.copytree(source / "lib/security", target / "lib/security", symlinks=False)
    java = os.environ.get("WISP_BOOTSTRAP_JAVA", "java")
    exploded = CACHE / "exploded-jdk23"
    if exploded.exists():
        shutil.rmtree(exploded)
    run([java, "-m", "jdk.jlink/jdk.tools.jimage.Main", "extract", "--dir", exploded, modules])
    # Flatten genuine JDK class/resources for Bovine's bootstrap classpath.
    # A fixed timestamp and sorted first-module ownership make the JAR reproducible.
    entries = {}
    for module in sorted(exploded.iterdir()):
        for path in sorted(module.rglob("*")):
            if path.is_file():
                relative = path.relative_to(module).as_posix()
                if relative != "module-info.class":
                    entries.setdefault(relative, path)
    with zipfile.ZipFile(dist / "boot.jar", "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as jar:
        for name, path in sorted(entries.items()):
            entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            jar.writestr(entry, path.read_bytes())
    shutil.rmtree(exploded)
    shutil.rmtree(source)


def bootstrap_inputs():
    paths = [ROOT / "bootstrap.py", ROOT / "toolchain.lock.json", *sorted((ROOT / "src").rglob("*")),
             *sorted((ROOT / "patches").glob("*.patch"))]
    return {str(path.relative_to(ROOT)): digest(path) for path in paths if path.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    if args.check:
        manifest = integrity()
        if manifest.get("bootstrapInputs") != bootstrap_inputs():
            raise ValueError("JVM bootstrap source inputs changed; rebuild the distribution")
        print(json.dumps({"passed": True, "files": len(manifest["files"])}))
        return
    if not args.rebuild:
        try:
            installed = integrity()
            if installed.get("bootstrapInputs") == bootstrap_inputs():
                print("Shared JVM and JDK data already match their pinned inputs")
                return
        except (OSError, ValueError, KeyError):
            pass
    lock = json.loads((ROOT / "toolchain.lock.json").read_text())
    wasi = sdk(lock["bootstrapWasiSdk"])
    archives = {name: fetch(lock[name], CACHE / (name + ".tar.gz"), args.offline) for name in ["runtime", "zlib", "jdk"]}
    source = extract(archives["runtime"], ROOT / ".sources" / ("b-jvm-" + lock["runtime"]["commit"]))
    zlib = extract(archives["zlib"], ROOT / ".sources" / ("zlib-" + lock["zlib"]["version"]))
    with tempfile.TemporaryDirectory(prefix=".bootstrap-", dir=ROOT) as directory:
        dist = Path(directory) / "dist"
        dist.mkdir()
        build_guest(source, zlib, wasi, dist / "jvm.wasm")
        jdk_data(archives["jdk"], lock["jdk"], dist)
        (dist / "fixtures").mkdir()
        run([os.environ.get("WISP_BOOTSTRAP_JAVA", "java"), "com.sun.tools.javac.Main", "-source", "17", "-target", "17",
             "-d", dist / "fixtures", ROOT / "src/JvmSmoke.java"])
        (dist / "licenses").mkdir()
        shutil.copyfile(source / "LICENSE", dist / "licenses/Bovine-MIT.txt")
        shutil.copyfile(zlib / "LICENSE", dist / "licenses/zlib.txt")
        manifest = {"schemaVersion": 1, "toolchain": "jvm", "version": lock["runtime"]["commit"][:12],
                    "target": "wasm32-wasip1", "kind": "shared-wasm-interpreter", "upstream": lock,
                    "nativeRuntimeBundled": False, "nodeRequired": False,
                    "execution": "Wisp supplies Wasmtime; JVM interpreter executes entirely in Wasm",
                    "bootstrapInputs": bootstrap_inputs(),
                    "files": {str(path.relative_to(dist)): {"bytes": path.stat().st_size, "sha256": digest(path)}
                              for path in sorted(dist.rglob("*")) if path.is_file()}}
        (dist / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        previous = ROOT / ".dist-previous"
        if previous.exists():
            shutil.rmtree(previous)
        if (ROOT / "dist").exists():
            (ROOT / "dist").rename(previous)
        try:
            dist.rename(ROOT / "dist")
        except OSError:
            if previous.exists():
                previous.rename(ROOT / "dist")
            raise
        if previous.exists():
            shutil.rmtree(previous)
    integrity()
    print(ROOT / "dist")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"JVM bootstrap: {error}") from error
