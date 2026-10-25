#!/usr/bin/env python3
"""Bootstrap the pinned portable TeaVM backend with a native JDK21 or newer."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
import importlib.util

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".cache"
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT.parent))


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(item, target, offline=False):
    if 'source' in item:
        source = ROOT / item['source']
        if (source.is_symlink() or not source.resolve().is_relative_to(ROOT)
                or not source.is_file() or source.stat().st_size != item['bytes']
                or digest(source) != item['sha256']):
            raise ValueError(f'vendored bootstrap input checksum mismatch: {item["source"]}')
        return source
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and digest(target) == item["sha256"]:
        return target
    if offline:
        raise ValueError(f"missing or changed bootstrap input: {target.name}")
    with urllib.request.urlopen(item["url"], timeout=120) as response:
        data = response.read()
    if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise ValueError(f"bootstrap input checksum mismatch: {target.name}")
    temporary = target.with_name(target.name + ".downloading")
    temporary.write_bytes(data)
    temporary.replace(target)
    return target


def run(*args):
    subprocess.run(list(map(str, args)), check=True)


def write_sdk(archive, target):
    """Convert upstream's signature archive into a reproducible ordinary JAR."""
    data = gzip.decompress(archive.read_bytes())
    offset, files = 0, {}
    while offset < len(data):
        size = struct.unpack_from(">H", data, offset)[0]
        offset += 2
        name = data[offset:offset + size].decode("utf-8")
        offset += size
        length = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        if offset + length > len(data):
            raise ValueError("truncated SDK signature archive")
        if name.endswith(".class"):
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError("invalid SDK signature path")
            files[name] = data[offset:offset + length]
        offset += length
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for name, content in sorted(files.items()):
            entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(entry, content)


def source_inputs():
    return [ROOT / name for name in ["bootstrap.py", "wasi_runtime.py", "c_backend.py", "driver.py", "compile.py",
            "src/TeaVMMain.java", "src/Smoke.java", "src/RuntimePatch.java", "src/WasiPlugin.java", "src/wasi-entry.c", "src/c-backend.cpp", "toolchain.lock.json", "licenses/ASM-9.10.1.txt"]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--check", action="store_true", help="verify the existing distribution without rebuilding")
    args = parser.parse_args()
    if args.check:
        spec = importlib.util.spec_from_file_location("teavm_integrity", ROOT / "compile.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        _, manifest = helper.verified_dist()
        sources = source_inputs()
        expected = {str(p.relative_to(ROOT)): digest(p) for p in sources}
        if manifest.get("bootstrapInputs") != expected:
            raise ValueError("TeaVM bootstrap sources changed; rebuild the distribution")
        print("TeaVM distribution integrity verified")
        return
    lock = json.loads((ROOT / "toolchain.lock.json").read_text())
    java = os.environ.get("WISP_BOOTSTRAP_JAVA", "java")
    jar_inputs = {item["path"]: fetch(item, CACHE / "maven" / item["path"], args.offline)
                  for item in lock["jars"]}
    generator = lock["signatureGenerator"]
    archive = fetch(generator, CACHE / f"teavm-javac-{generator['revision']}.tar.gz", args.offline)
    notices = {item["name"]: fetch(item, CACHE / (item["name"] + ".txt"), args.offline)
               for item in lock["licenses"]}
    with tempfile.TemporaryDirectory(prefix=".bootstrap-", dir=ROOT) as temporary:
        work = Path(temporary)
        source_parent = work / "sources"
        source_parent.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(source_parent, filter="data")
        source = source_parent / f"teavm-javac-{generator['revision']}"
        dist = work / "dist"
        (dist / "jars").mkdir(parents=True)
        for name, path in jar_inputs.items():
            shutil.copyfile(path, dist / "jars" / name)
        classpath = os.pathsep.join(map(str, jar_inputs.values()))
        patcher = work / "patcher"
        patcher.mkdir()
        run(java, "com.sun.tools.javac.Main", "-source", "17", "-target", "17",
            "-cp", classpath, "-d", patcher, ROOT / "src/RuntimePatch.java")
        patched_core = work / "teavm-core-gc.jar"
        run(java, "-cp", str(patcher) + os.pathsep + classpath,
            "wisp.bootstrap.RuntimePatch", jar_inputs["teavm-core-0.16.0.jar"], patched_core)
        from wasi_runtime import patch_runtime
        runtime_patches = patch_runtime(patched_core, dist / "jars/teavm-core-0.16.0.jar")
        gc_class = "org/teavm/runtime/GC.class"
        with zipfile.ZipFile(jar_inputs["teavm-core-0.16.0.jar"]) as original, zipfile.ZipFile(patched_core) as patched:
            runtime_patches.append({"class": gc_class, "source": "src/RuntimePatch.java",
                "originalSha256": hashlib.sha256(original.read(gc_class)).hexdigest(),
                "patchedSha256": hashlib.sha256(patched.read(gc_class)).hexdigest(),
                "repairs": ["accept an exact eight-byte free-chunk remainder",
                            "include the free-chunk header in heap growth"]})
        launcher = dist / "launcher"
        launcher.mkdir()
        run(java, "com.sun.tools.javac.Main", "-source", "17", "-target", "17",
            "-cp", classpath, "-d", launcher, ROOT / "src/TeaVMMain.java", ROOT / "src/WasiPlugin.java")
        services = launcher / "META-INF/services"
        services.mkdir(parents=True)
        (services / "org.teavm.vm.spi.TeaVMPlugin").write_text("wisp.java.WasiPlugin\n")
        (dist / "fixtures").mkdir()
        run(java, "com.sun.tools.javac.Main", "-source", "17", "-target", "17",
            "-cp", classpath, "-d", dist / "fixtures", ROOT / "src/Smoke.java")
        converters = work / "converters"
        converters.mkdir()
        converter_source = source / "compiler/src/main/java/org/teavm/javac"
        patched = work / "converter-source"
        patched.mkdir()
        original = (converter_source / "StdlibConverter.java").read_text()
        marker = "    @Override\n    public FieldVisitor visitField("
        # Upstream renames class/method signatures but forwards nested-class
        # metadata unchanged. ECJ needs the renamed Lookup membership to emit
        # valid lambda and record bootstrap attributes.
        metadata = """    @Override
    public void visitInnerClass(String name, String outer, String inner, int access) {
        super.visitInnerClass(rename(name), outer != null ? rename(outer) : null, inner, access);
    }
    @Override
    public void visitOuterClass(String owner, String name, String descriptor) {
        super.visitOuterClass(rename(owner), name, descriptor != null ? renameMethodDesc(descriptor) : null);
    }
    @Override
    public void visitNestHost(String host) { super.visitNestHost(rename(host)); }
    @Override
    public void visitNestMember(String member) { super.visitNestMember(rename(member)); }

"""
        if original.count(marker) != 1:
            raise ValueError("upstream signature converter metadata patch no longer matches")
        (patched / "StdlibConverter.java").write_text(original.replace(marker, metadata + marker))
        run(java, "com.sun.tools.javac.Main", "-source", "21", "-target", "21",
            "-cp", jar_inputs["asm-9.10.1.jar"], "-d", converters,
            converter_source / "ArchiveBuilder.java", patched / "StdlibConverter.java")
        emulation = work / "emulation"
        emulation.mkdir()
        run(java, "com.sun.tools.javac.Main", "-source", "21", "-target", "21",
            "-cp", classpath, "-d", emulation,
            *sorted((source / "compiler/src/classlibEmu/java").rglob("*.java")))
        signatures = work / "signatures.bin"
        run(java, "-cp", str(converters) + os.pathsep + str(jar_inputs["asm-9.10.1.jar"]),
            "org.teavm.javac.StdlibConverter", signatures,
            jar_inputs["teavm-classlib-0.16.0.jar"], jar_inputs["teavm-jso-0.16.0.jar"],
            jar_inputs["teavm-jso-apis-0.16.0.jar"], emulation)
        write_sdk(signatures, dist / "java-sdk.jar")
        license_dir = dist / "licenses"
        license_dir.mkdir()
        for name, path in notices.items():
            shutil.copyfile(path, license_dir / (name + ".txt"))
        for name, path in jar_inputs.items():
            with zipfile.ZipFile(path) as jar:
                for entry in jar.namelist():
                    if entry.upper().startswith("META-INF/") and any(
                            word in entry.upper() for word in ["LICENSE", "NOTICE", "COPYING"]):
                        if not entry.endswith("/"):
                            (license_dir / (name + "-" + Path(entry).name)).write_bytes(jar.read(entry))
        sdk = Path(os.environ.get("WISP_JVM_WASI_SDK", ROOT.parent / "clang/.toolchains/wasi-sdk-34.0-x86_64-linux")).resolve()
        if digest(sdk / "bin/clang") != lock["bootstrapWasiSdk"]["clangSha256"]:
            raise ValueError("TeaVM bootstrap needs the pinned WASI SDK 34 compiler")
        from driver import build as build_driver
        (dist / "bin").mkdir()
        driver_record = build_driver(sdk, dist / "bin/c-backend.wasm", work / "driver-build", lock)
        from llvm.shared import root as llvm_root
        shutil.copytree(llvm_root() / "licenses", license_dir / "shared-llvm")
        shutil.copyfile(ROOT / "src/wasi-entry.c", dist / "wasi-entry.c")
        resources = ROOT.parent / "clang/include"
        sysroot = dist / "sysroot"
        shutil.copytree(sdk / "share/wasi-sysroot/include/wasm32-wasip1", sysroot / "include", ignore=shutil.ignore_patterns("c++"))
        (sysroot / "lib").mkdir()
        for name in ["crt1-command.o", "crt1-reactor.o", "libc.a", "libm.a", "libdl.a", "libsetjmp.a",
                     "libwasi-emulated-mman.a", "libwasi-emulated-signal.a", "libwasi-emulated-process-clocks.a", "libwasi-emulated-getpid.a"]:
            shutil.copyfile(sdk / "share/wasi-sysroot/lib/wasm32-wasip1" / name, sysroot / "lib" / name)
        shutil.copyfile(sdk / "lib/clang/23/lib/wasm32-unknown-wasip1/libclang_rt.builtins.a", sysroot / "lib/libclang_rt.builtins.a")
        inputs = source_inputs()
        bootstrap_java = subprocess.check_output([java, "-version"], stderr=subprocess.STDOUT, text=True).strip()
        manifest = {"schemaVersion": 1, "toolchain": "teavm", "version": "0.16.0",
                    "target": "wasm32-wasip1", "sourceCompilationWasmHosted": True,
                    "runtimeDependencies": ["jvm", "llvm", "clang", "wasm-tools"], "nodeRequired": False,
                    "bootstrapInputs": {str(p.relative_to(ROOT)): digest(p) for p in inputs},
                    "upstream": lock,
                    "bootstrapJava": bootstrap_java,
                    "jarTransformations": runtime_patches,
                    "cBackend": driver_record,
                    "clangResources": {str(p.relative_to(resources)): digest(p) for p in sorted(resources.rglob("*")) if p.is_file()},
                    "sha256": {str(p.relative_to(dist)): digest(p) for p in sorted(dist.rglob("*")) if p.is_file()}}
        (dist / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        previous = ROOT / ".dist-previous"
        if previous.exists():
            shutil.rmtree(previous)
        if (ROOT / "dist").exists():
            (ROOT / "dist").rename(previous)
        dist.rename(ROOT / "dist")
        if previous.exists():
            shutil.rmtree(previous)
    print(f"TeaVM 0.16.0 WASI backend: {ROOT / 'dist'}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"teavm bootstrap: {error}")
